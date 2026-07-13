# SELF_ASSESSMENT: I am the control surface for per-speaker pause profiles —
# I read/write scratch/speaker_pace.json, which the voice daemon's cmd_listen
# consults for the end-of-turn silence window (ff111ea, Zeke design directive
# 2026-07-12: unknown speaker ~3s default, then learned per person).
"""
speaker_pace — get/set the per-speaker end-silence profiles.

The daemon (voice/wren_voice_core.py::_speaker_end_silence) reads
scratch/speaker_pace.json at every listen-start:

    {"active": "zeke", "profiles": {"zeke": 1.5, "q": 3.0}, "default_unknown": 3.0}

Precedence there: explicit end_silence_seconds arg > this file > END_SILENCE_S (1.1).
File ABSENT = feature dormant (exact pre-ff111ea behavior). Values are clamped
daemon-side to 0.3–8.0s; out-of-range values are ignored (fail-open).

Tools:
  speaker_pace_get  -> current file contents (or {"dormant": true})
  speaker_pace_set  -> params: {active?: str, profile?: {name: str, seconds: float},
                                default_unknown?: float, disable?: bool,
                                smart_extend?: {enabled: bool, max_seconds?: float,
                                                recheck_seconds?: float}}
    - disable=true deletes the file (back to dormant/global default)
    - smart_extend: hold the mic past the fast window when smart-turn says the
      sentence sounds unfinished (daemon _smart_extend_cfg reads this per-listen)
    - any other combination merges into the existing file
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

PACE_FILE = Path(r"D:\Wren-Companion\scratch\speaker_pace.json")


def _read() -> dict[str, Any]:
    try:
        if PACE_FILE.exists():
            return json.loads(PACE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _speaker_pace_get(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    d = _read()
    if not d and not PACE_FILE.exists():
        return {"ok": True, "dormant": True,
                "note": "file absent — daemon uses global END_SILENCE_S (1.1s)"}
    return {"ok": True, "dormant": False, "config": d, "path": str(PACE_FILE)}


def _speaker_pace_set(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    if params.get("disable"):
        try:
            if PACE_FILE.exists():
                PACE_FILE.unlink()
            return {"ok": True, "dormant": True, "note": "pace file removed — back to global default"}
        except Exception as e:
            return {"ok": False, "error": f"could not remove pace file: {e!r}"}

    d = _read()
    changed = []
    active = params.get("active")
    if isinstance(active, str) and active.strip():
        d["active"] = active.strip().lower()
        changed.append(f"active={d['active']}")
    prof = params.get("profile")
    if isinstance(prof, dict) and prof.get("name"):
        try:
            secs = float(prof["seconds"])
            if not 0.3 <= secs <= 8.0:
                return {"ok": False, "error": f"seconds {secs} outside sane range 0.3-8.0"}
            d.setdefault("profiles", {})[str(prof["name"]).strip().lower()] = secs
            changed.append(f"profile {prof['name']}={secs}s")
        except (KeyError, TypeError, ValueError) as e:
            return {"ok": False, "error": f"bad profile param: {e!r}"}
    du = params.get("default_unknown")
    if du is not None:
        try:
            duf = float(du)
            if not 0.3 <= duf <= 8.0:
                return {"ok": False, "error": f"default_unknown {duf} outside 0.3-8.0"}
            d["default_unknown"] = duf
            changed.append(f"default_unknown={duf}s")
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": f"bad default_unknown: {e!r}"}
    se = params.get("smart_extend")
    if isinstance(se, dict):
        # Smart-extend (Zeke spec 2026-07-13): if the turn SOUNDS unfinished at the
        # fast window, the daemon holds the mic up to max_seconds. Daemon-side reader
        # (_smart_extend_cfg) fail-opens on anything insane; we validate here too.
        try:
            cur = d.get("smart_extend") or {}
            cur["enabled"] = bool(se.get("enabled", cur.get("enabled", True)))
            if "max_seconds" in se:
                ms = float(se["max_seconds"])
                if not 1.0 <= ms <= 10.0:
                    return {"ok": False, "error": f"smart_extend.max_seconds {ms} outside 1.0-10.0"}
                cur["max_seconds"] = ms
            if "recheck_seconds" in se:
                rs = float(se["recheck_seconds"])
                if not 0.2 <= rs <= 2.0:
                    return {"ok": False, "error": f"smart_extend.recheck_seconds {rs} outside 0.2-2.0"}
                cur["recheck_seconds"] = rs
            cur.setdefault("max_seconds", 3.0)
            d["smart_extend"] = cur
            changed.append(f"smart_extend={'on' if cur['enabled'] else 'off'} "
                           f"(max {cur['max_seconds']}s)")
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": f"bad smart_extend param: {e!r}"}

    if not changed:
        return {"ok": False, "error": "nothing to set — pass active, profile, default_unknown, smart_extend, or disable"}
    try:
        PACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PACE_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"write failed: {e!r}"}
    # Verify with the real loader (config-patch rule): re-read and parse.
    try:
        back = json.loads(PACE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"post-write parse FAILED — file may be corrupt: {e!r}"}
    return {"ok": True, "changed": changed, "config": back,
            "note": "takes effect at the daemon's NEXT listen-start (read per-listen; no reload needed)"}


register_tool(
    "speaker_pace_get",
    "Read per-speaker end-silence profiles (scratch/speaker_pace.json) the voice daemon consults. dormant=true means feature off.",
    1,
    _speaker_pace_get,
)

register_tool(
    "speaker_pace_set",
    "Set per-speaker end-silence: params {active?: str, profile?: {name, seconds}, default_unknown?: float, smart_extend?: {enabled, max_seconds, recheck_seconds}, disable?: bool}. Zeke~1.5s, Q~3.0s, unknown 3.0s; smart_extend holds the mic when the sentence sounds unfinished. Takes effect next listen.",
    2,
    _speaker_pace_set,
)
