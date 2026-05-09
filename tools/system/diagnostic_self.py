# SELF_ASSESSMENT: I introspect my own state and report what's actually wrong in technical terms — subsystem health, recent errors, last successful timestamps. I help Ava answer "what's wrong with you?" with specifics instead of vague feelings.
"""
Self-diagnostic introspection tool — Task 2 of the 2026-05-02 work order.

When Ava is asked "what's wrong with you?" or self-reports being broken, this
tool pulls the current diagnostic state and returns a technical summary:
which subsystem failed, what error code, when it was last working, what's
been tried. Output is formatted plain-language so Ava can speak it
directly without the user seeing JSON.

Where the data comes from (audit results in
docs/research/local_models/ — same audit framework as Task 1):

  - /api/v1/debug/full       subsystem_health, errors_recent, recent_traces,
                              dual_brain_state, voice_loop, ribbon_and_heartbeat
  - state/health_state.json   per-subsystem issues + history (computed by
                              brain/health.py but not currently exposed)
  - debug_state ring buffers  full traceback text from caught exceptions

The tool runs inside Ava's process so it can read globals directly. No HTTP
hop, no race with snapshot endpoints.

Trigger paths:
  - Voice command "what's wrong" / "are you ok" / "what's broken" /
    "diagnostic check" — wired in brain/voice_commands.py
  - LLM emits [TOOL:diagnostic_self] when self-reporting being broken in
    dialogue. Hint added to SYSTEM_PROMPT.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool


# ── Helpers ────────────────────────────────────────────────────────────────


def _read_health_state(g: dict[str, Any]) -> dict[str, Any]:
    """Pull health.py's per-subsystem issues + history. Not currently exposed
    via /api/v1/debug/full — read it directly from disk."""
    base = Path(g.get("BASE_DIR") or ".")
    path = base / "state" / "health_state.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _subsystem_health_summary(g: dict[str, Any]) -> list[dict[str, Any]]:
    """Read live subsystem state from globals + health file. Returns a list
    of {name, status, detail, last_successful_iso} entries."""
    out: list[dict[str, Any]] = []
    health = _read_health_state(g)

    # Camera + InsightFace + TTS + STT come from globals, augmented by health.
    camera_mgr = g.get("camera_manager")
    if camera_mgr is not None:
        cam_state = "available" if getattr(camera_mgr, "is_available", lambda: False)() else "unavailable"
        cam_running = bool(getattr(camera_mgr, "is_running", lambda: False)())
        out.append({
            "name": "camera",
            "status": "ok" if (cam_state == "available" and cam_running) else "degraded",
            "detail": f"available={cam_state} running={cam_running}",
            "last_successful_iso": _last_successful_iso(health, "camera"),
        })

    if g.get("insight_engine") is not None:
        providers = []
        try:
            providers = list(getattr(g["insight_engine"], "active_providers", []) or [])
        except Exception:
            pass
        on_gpu = any("CUDA" in str(p) for p in providers)
        out.append({
            "name": "insightface",
            "status": "ok" if on_gpu else ("degraded" if providers else "unavailable"),
            "detail": f"providers={providers}",
            "last_successful_iso": _last_successful_iso(health, "insightface"),
        })

    tts = g.get("_tts_worker")
    if tts is not None:
        out.append({
            "name": "tts",
            "status": "ok" if not bool(getattr(tts, "_muted", False)) else "muted",
            "detail": f"speaking={bool(getattr(tts,'_speaking',False))} queue_depth={int(getattr(tts,'_queue_depth',0))} last_dropped={bool(g.get('_tts_last_playback_dropped',False))}",
            "last_successful_iso": _last_successful_iso(health, "tts"),
        })

    if g.get("whisper_model") is not None:
        out.append({
            "name": "stt",
            "status": "ok",
            "detail": "WhisperModel loaded",
            "last_successful_iso": _last_successful_iso(health, "stt"),
        })

    if g.get("_kokoro_pipeline") is not None or g.get("_kokoro_loaded"):
        out.append({
            "name": "kokoro_tts",
            "status": "ok",
            "detail": "Kokoro pipeline loaded",
            "last_successful_iso": _last_successful_iso(health, "kokoro_tts"),
        })

    # Ollama reachability
    ok = bool(g.get("_ollama_last_ok") or False)
    out.append({
        "name": "ollama",
        "status": "ok" if ok else "degraded",
        "detail": f"last_check_ts={g.get('_ollama_last_check_ts','unknown')} last_error={str(g.get('_ollama_last_error','') or '')[:120]}",
        "last_successful_iso": _last_successful_iso(health, "ollama"),
    })

    # mem0
    mem = g.get("_ava_memory")
    if mem is not None:
        avail = bool(getattr(mem, "available", False))
        init_err = str(getattr(mem, "init_error", "") or "")[:140]
        out.append({
            "name": "mem0",
            "status": "ok" if avail else "unavailable",
            "detail": f"available={avail} init_error={init_err!r}" if init_err else f"available={avail}",
            "last_successful_iso": _last_successful_iso(health, "mem0"),
        })

    return out


def _last_successful_iso(health: dict[str, Any], subsystem: str) -> str:
    """Pull the last-known-good timestamp from health_state.json if present."""
    try:
        hist = (health.get("history") or []) if isinstance(health, dict) else []
        for entry in reversed(hist):
            if entry.get("subsystem") == subsystem and entry.get("status") in ("ok", "available"):
                return str(entry.get("ts_iso") or entry.get("ts") or "")
    except Exception:
        pass
    return ""


def _recent_errors(limit: int = 5) -> list[dict[str, Any]]:
    """Pull the last N entries from debug_state's structured error ring."""
    try:
        from brain.debug_state import get_errors
        return list(get_errors(limit=limit))
    except Exception:
        return []


def _recent_traces(limit: int = 8) -> list[str]:
    try:
        from brain.debug_state import get_traces
        return list(get_traces(limit=limit))
    except Exception:
        return []


# ── Public tool handler ───────────────────────────────────────────────────


def diagnostic_self(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Run a self-diagnostic and return both structured data and a
    plain-language summary string Ava can speak directly.

    Optional params:
      verbose: bool — include traces and full error tracebacks
      subsystem: str — narrow the report to a single subsystem
    """
    verbose = bool(params.get("verbose") or False)
    only = str(params.get("subsystem") or "").strip().lower()

    subsystems = _subsystem_health_summary(g)
    if only:
        subsystems = [s for s in subsystems if s["name"] == only]

    errors = _recent_errors(limit=5)
    traces = _recent_traces(limit=8) if verbose else []

    # Build the spoken summary.
    lines: list[str] = []
    degraded = [s for s in subsystems if s["status"] not in ("ok",)]
    if degraded:
        lines.append(f"I have {len(degraded)} subsystem{'s' if len(degraded) > 1 else ''} reporting degraded state right now.")
        for s in degraded:
            ls = s.get("last_successful_iso") or "no recorded last-successful timestamp"
            lines.append(f"- {s['name']}: {s['status']} — {s['detail']}. Last known good: {ls}.")
    else:
        lines.append("All subsystems I check are reporting healthy.")
        for s in subsystems:
            lines.append(f"- {s['name']}: {s['status']} — {s['detail']}.")

    if errors:
        lines.append("")
        lines.append(f"My most recent {len(errors)} captured errors:")
        for e in errors[-5:]:
            ts = str(e.get("ts") or "")[:19]
            mod = str(e.get("module") or "?")
            msg = str(e.get("message") or "")[:160]
            lines.append(f"- [{ts}] {mod}: {msg}")
    else:
        lines.append("")
        lines.append("No structured errors captured in the recent error ring.")

    if traces:
        lines.append("")
        lines.append("Recent traces:")
        for t in traces:
            lines.append(f"  {t}")

    summary_text = "\n".join(lines)

    return {
        "ok": True,
        "summary_text": summary_text,
        "subsystems": subsystems,
        "errors_recent": errors,
        "traces_recent": traces if verbose else None,
        "captured_ts": time.time(),
    }


register_tool(
    "diagnostic_self",
    "Run a self-diagnostic — returns subsystem health, recent errors, last-known-good "
    "timestamps. Use when the user asks 'what's wrong' / 'are you ok' / 'what's broken', "
    "or when about to self-report being broken in dialogue. The summary_text field is "
    "ready to speak.",
    1,
    diagnostic_self,
)
