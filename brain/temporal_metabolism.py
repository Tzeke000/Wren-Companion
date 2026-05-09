"""brain/temporal_metabolism.py — Slow-cycle Memory-as-Metabolism pass (B3, 2026-05-03).

See docs/TEMPORAL_SENSE.md §7 and docs/MEMORY_METABOLISM_AUDIT.md for context.

This is the slow-cycle side of the two-cadence architecture (5-15 min default).
Runs the named TRIAGE → CONTEXTUALIZE → DECAY → CONSOLIDATE → AUDIT pass against
existing memory infrastructure rather than duplicating it:

  TRIAGE       — walk concept graph, flag nodes that crossed bands since last cycle
  CONTEXTUALIZE— cross-reference triaged items with active conversation, mood, goals
  DECAY        — call existing concept_graph.decay_levels(now) + memory.decay_tick(g)
  CONSOLIDATE  — conditional micro-consolidation if triage queue large or time-since-full > N hours
  AUDIT        — append cycle results to state/metabolism_log.jsonl

Performance budget: this can take seconds, can hit the LLM (consolidation step),
can do disk I/O. MUST yield to the voice loop — defers if _conversation_active
or _turn_in_progress is set.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from brain.temporal_sense import _cfg


_LAST_CYCLE_TS: float = 0.0
_LAST_CYCLE_LOCK = threading.Lock()


def _log_path(g: dict[str, Any]) -> Path:
    base = Path(g.get("BASE_DIR") or ".")
    rel = str(_cfg("metabolism", "log_path", default="state/metabolism_log.jsonl"))
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _should_run(g: dict[str, Any], now: float) -> bool:
    """Yield to the voice loop. Defer if Ava is in a turn."""
    if bool(g.get("_conversation_active")) or bool(g.get("_turn_in_progress")):
        return False
    interval = float(_cfg("slow_cycle", "interval_seconds", default=600))
    return (now - _LAST_CYCLE_TS) >= interval


# ── TRIAGE ─────────────────────────────────────────────────────────────────


def _triage(g: dict[str, Any], now: float) -> dict[str, Any]:
    """Identify concept graph nodes that crossed bands since last cycle.

    Returns a dict with:
      - level_dropped: list of node_ids whose level dropped >= triage_level_drop_threshold
      - recently_activated: list of node_ids activated within triage_recent_activation_seconds
      - archive_streak_advanced: list of node_ids whose archive_streak incremented
      - count_total: total triaged
    """
    summary: dict[str, Any] = {
        "level_dropped": [],
        "recently_activated": [],
        "archive_streak_advanced": [],
        "count_total": 0,
    }
    cg = g.get("_concept_graph")
    if cg is None:
        return summary
    drop_threshold = int(_cfg("metabolism", "triage_level_drop_threshold", default=1))
    recent_window = float(_cfg("metabolism", "triage_recent_activation_seconds", default=3600))

    try:
        nodes = list(getattr(cg, "_nodes", {}).values()) if hasattr(cg, "_nodes") else []
    except Exception:
        nodes = []

    cutoff = now - recent_window

    # Compare against last-known levels in g for level-drop detection.
    last_levels: dict[str, int] = g.get("_metabolism_last_levels") or {}
    new_levels: dict[str, int] = {}

    seen: set[str] = set()
    for node in nodes:
        try:
            node_id = str(getattr(node, "id", ""))
            if not node_id:
                continue
            level = int(getattr(node, "level", 0) or 0)
            new_levels[node_id] = level
            last_act = float(getattr(node, "last_activated", 0.0) or 0.0)
            archive_streak = int(getattr(node, "archive_streak", 0) or 0)

            prior_level = last_levels.get(node_id)
            if prior_level is not None and (prior_level - level) >= drop_threshold:
                summary["level_dropped"].append(node_id)
                seen.add(node_id)

            if last_act > cutoff:
                summary["recently_activated"].append(node_id)
                seen.add(node_id)

            # archive_streak advancement detection — needs prior tracking too
            prior_streak = (g.get("_metabolism_last_archive_streaks") or {}).get(node_id, 0)
            if archive_streak > prior_streak:
                summary["archive_streak_advanced"].append(node_id)
                seen.add(node_id)
        except Exception:
            continue

    g["_metabolism_last_levels"] = new_levels
    g["_metabolism_last_archive_streaks"] = {
        node_id: int(getattr(node, "archive_streak", 0) or 0)
        for node in nodes
        for node_id in [str(getattr(node, "id", "") or "")]
        if node_id
    }
    summary["count_total"] = len(seen)
    # Trim long lists for log readability — don't dump 200 ids into the audit
    for k in ("level_dropped", "recently_activated", "archive_streak_advanced"):
        if len(summary[k]) > 20:
            summary[k] = summary[k][:20] + ["...(truncated)"]
    return summary


# ── CONTEXTUALIZE ──────────────────────────────────────────────────────────


def _contextualize(g: dict[str, Any], triage: dict[str, Any]) -> dict[str, Any]:
    """Cross-reference triaged items with active context.

    Light-weight pass: collect the active context once, note matches by category.
    """
    ctx: dict[str, Any] = {}

    # Active conversation topic
    try:
        from brain.inner_monologue import _read_chatlog_topic  # type: ignore
        base = Path(g.get("BASE_DIR") or ".")
        ctx["chatlog_topic"] = _read_chatlog_topic(base)[:120]
    except Exception:
        ctx["chatlog_topic"] = ""

    # Current mood top-3 emotions
    try:
        load_mood = g.get("load_mood")
        if callable(load_mood):
            mood = load_mood() or {}
            primaries = mood.get("primary_emotions") or []
            ctx["mood_top"] = [
                {"name": p.get("name"), "percent": p.get("percent")}
                for p in primaries[:3]
                if isinstance(p, dict)
            ]
    except Exception:
        ctx["mood_top"] = []

    # Open goals (best-effort; goal_system shape varies)
    try:
        load_goal_system = g.get("load_goal_system")
        if callable(load_goal_system):
            gs = load_goal_system() or {}
            active_goal = (gs.get("active_goal") or {}).get("name", "")
            ctx["active_goal"] = str(active_goal)[:80]
    except Exception:
        ctx["active_goal"] = ""

    return {
        "context": ctx,
        "triage_count": int(triage.get("count_total", 0)),
    }


# ── DECAY ──────────────────────────────────────────────────────────────────


def _decay(g: dict[str, Any], now: float) -> dict[str, Any]:
    """Call existing decay implementations. Don't duplicate."""
    summary: dict[str, Any] = {"concept_decay": None, "vector_decay": None}

    if bool(int(__import__("os").environ.get("AVA_DECAY_DISABLED", "0") or "0")):
        summary["skipped"] = "AVA_DECAY_DISABLED"
        return summary

    cg = g.get("_concept_graph")
    if cg is not None:
        try:
            if hasattr(cg, "decay_levels"):
                result = cg.decay_levels(now)
                summary["concept_decay"] = result if isinstance(result, dict) else {"ok": True}
        except Exception as e:
            summary["concept_decay"] = {"error": repr(e)}

    try:
        from brain import memory as _mem  # lazy import; module may not always be importable
        if hasattr(_mem, "decay_tick"):
            res = _mem.decay_tick(g)
            summary["vector_decay"] = res if isinstance(res, dict) else {"ok": True}
    except Exception as e:
        summary["vector_decay"] = {"error": repr(e)[:120]}

    return summary


# ── CONSOLIDATE (conditional micro-consolidation) ──────────────────────────


def _maybe_consolidate(g: dict[str, Any], triage: dict[str, Any], now: float) -> dict[str, Any]:
    """Conditional micro-consolidation. Triggers if triage queue large
    OR enough hours since last full consolidation."""
    queue_threshold = int(_cfg("slow_cycle", "micro_consolidation_queue_threshold", default=20))
    min_hours = float(_cfg("slow_cycle", "micro_consolidation_min_hours_since_full", default=6))

    triage_count = int(triage.get("count_total", 0))
    full_consolidation_path = Path(g.get("BASE_DIR") or ".") / "state" / "consolidation_state.json"
    last_full_ts = 0.0
    try:
        if full_consolidation_path.is_file():
            data = json.loads(full_consolidation_path.read_text(encoding="utf-8"))
            last_full_ts = float(data.get("last_consolidation_ts") or 0.0)
    except Exception:
        pass
    hours_since_full = (now - last_full_ts) / 3600.0 if last_full_ts > 0 else 999.0

    should_micro = (triage_count >= queue_threshold) or (hours_since_full >= min_hours)
    if not should_micro:
        return {"triggered": False, "reason": f"queue={triage_count}<{queue_threshold} AND hours_since_full={hours_since_full:.1f}<{min_hours}"}

    # Don't actually run the LLM-heavy full consolidation here — that's
    # weekly. Micro-consolidation = the cheaper subset (episode review +
    # concept graph prune already happens in DECAY above). For now we
    # log the decision; full micro-consolidation wiring lands in a
    # follow-up if needed.
    return {
        "triggered": True,
        "reason": f"queue={triage_count}>={queue_threshold} OR hours_since_full={hours_since_full:.1f}>={min_hours}",
        "note": "micro-consolidation logged but does not invoke LLM in B3 minimum-viable; weekly full consolidation continues to handle the LLM step",
    }


# ── AUDIT ──────────────────────────────────────────────────────────────────


def _audit(g: dict[str, Any], cycle_summary: dict[str, Any]) -> None:
    """Append the cycle's results to state/metabolism_log.jsonl."""
    p = _log_path(g)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(cycle_summary, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[temporal_metabolism] audit append error: {e!r}")


# ── Public entry point ────────────────────────────────────────────────────


def run_metabolism_cycle(g: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Run one full metabolism cycle. Yields to the voice loop.

    Returns a summary dict; logs full result to metabolism_log.jsonl.
    """
    global _LAST_CYCLE_TS
    if now is None:
        now = time.time()

    if not _should_run(g, now):
        return {"skipped": "deferred_to_voice_loop_or_too_soon"}

    with _LAST_CYCLE_LOCK:
        # Re-check inside lock to avoid races
        if not _should_run(g, now):
            return {"skipped": "raced"}
        _LAST_CYCLE_TS = now

        cycle_id = f"cycle_{int(now)}"
        summary: dict[str, Any] = {
            "ts": now,
            "cycle_id": cycle_id,
        }

        # Run the five stages. Each stage is best-effort; an error in one
        # doesn't stop the others — observability through the audit log
        # is more important than atomic completion.
        try:
            triage = _triage(g, now)
            summary["triage"] = triage
        except Exception as e:
            summary["triage"] = {"error": repr(e)[:160]}
            triage = {"count_total": 0}

        try:
            summary["contextualize"] = _contextualize(g, triage)
        except Exception as e:
            summary["contextualize"] = {"error": repr(e)[:160]}

        try:
            summary["decay"] = _decay(g, now)
        except Exception as e:
            summary["decay"] = {"error": repr(e)[:160]}

        try:
            summary["consolidate"] = _maybe_consolidate(g, triage, now)
        except Exception as e:
            summary["consolidate"] = {"error": repr(e)[:160]}

        # AUDIT step is the log write itself
        _audit(g, summary)

        return summary
