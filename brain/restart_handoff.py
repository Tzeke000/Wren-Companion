"""brain/restart_handoff.py — restart-with-handoff (Task 5, 2026-05-02).

Concrete first step toward continuous interiority's sleep mode pattern
(see docs/CONTINUOUS_INTERIORITY.md §2). Pattern:

  1. User asks Ava to restart (voice command or UI button).
  2. Ava verbally acknowledges with a time estimate (with ~25 % safety
     buffer — she should over-estimate slightly).
  3. write_handoff() snapshots her current state to
     state/restart_handoff.json: timestamp, estimate, current emotional
     state, current activity, in-progress thoughts, last user
     interaction.
  4. Ava writes state/restart_requested.flag (watched externally by
     scripts/watchdog.py in production) and exits cleanly.
  5. The watchdog (or the user) restarts avaagent.
  6. On boot, read_handoff_on_boot() finds the handoff JSON, calculates
     time_offline = now - restart_initiated_at, surfaces this to the
     inner monologue ring, and deletes the file (read-once).

This is NOT autonomous sleep mode. It's a stepping stone — sleep mode
is the same pattern but Ava initiates it based on her own state and
runs dream-phase activity during the offline period. The mechanics
match.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


_HANDOFF_FILENAME = "restart_handoff.json"
_RESTART_FLAG_FILENAME = "restart_requested.flag"
_DEFAULT_BUFFER = 1.25  # 25 % safety buffer per CONTINUOUS_INTERIORITY §2


def _state_dir(g: dict[str, Any]) -> Path:
    base = Path(g.get("BASE_DIR") or ".")
    state = base / "state"
    state.mkdir(parents=True, exist_ok=True)
    return state


def _safe_load_mood(g: dict[str, Any]) -> dict[str, Any]:
    load_mood_fn = g.get("load_mood")
    if not callable(load_mood_fn):
        return {}
    try:
        m = load_mood_fn() or {}
        # Trim — we only need the headline mood, not the full weights blob.
        return {
            "current_mood": str(m.get("current_mood") or ""),
            "primary_emotions": m.get("primary_emotions") or [],
            "outward_tone": str(m.get("outward_tone") or ""),
        }
    except Exception:
        return {}


def _safe_inner_monologue_thought(g: dict[str, Any]) -> str:
    """Pull the most recent inner thought, if any. Best-effort."""
    base = Path(g.get("BASE_DIR") or ".")
    try:
        from brain.inner_monologue import current_thought
        return str(current_thought(base) or "")
    except Exception:
        return ""


def _safe_curiosity_topic(g: dict[str, Any]) -> str:
    try:
        from brain.curiosity_topics import get_current_curiosity
        cur = get_current_curiosity(g) or {}
        return str(cur.get("topic") or "")
    except Exception:
        return ""


def write_handoff(
    g: dict[str, Any],
    *,
    estimate_seconds: float,
    trigger: str = "voice_command",
    spoken_acknowledgment: str = "",
) -> Path:
    """Snapshot current state to state/restart_handoff.json.

    estimate_seconds is what Ava said out loud — the file stores the
    raw estimate AND the buffered version so the boot-side check can
    detect over-runs ("she said 60 s, came back at 105 s" is a signal
    something went wrong).

    B3 (2026-05-03): also registers the estimate with temporal_sense so
    historical lookup gets calibrated over time. Future restart
    estimates can read median actual_seconds from
    state/task_history_log.jsonl rather than guessing 15s every time.
    """
    state = _state_dir(g)
    path = state / _HANDOFF_FILENAME
    initiated_at = time.time()

    # B3 hook: register with temporal_sense's estimate tracker. Best-
    # effort; if temporal_sense isn't importable for any reason, the
    # handoff still works as before.
    history_calibration: dict[str, Any] | None = None
    try:
        from brain.temporal_sense import track_estimate, calibrate_from_history
        history_calibration = calibrate_from_history(g, kind="restart")
        track_estimate(
            g,
            task_id=f"restart_{int(initiated_at)}",
            estimate_seconds=estimate_seconds,
            kind="restart",
            context=trigger,
        )
    except Exception as _ts_exc:
        print(f"[restart_handoff] temporal_sense register skipped: {_ts_exc!r}")

    payload = {
        "version": 1,
        "restart_initiated_at": initiated_at,
        "trigger": trigger,
        "spoken_acknowledgment": spoken_acknowledgment,
        "restart_estimate_seconds": float(estimate_seconds),
        "restart_estimate_seconds_buffered": float(estimate_seconds * _DEFAULT_BUFFER),
        "history_calibration_at_create": history_calibration,
        "current_emotional_state": _safe_load_mood(g),
        "current_activity": str(g.get("_inner_state_line") or ""),
        "in_progress_thoughts": _safe_inner_monologue_thought(g),
        "current_curiosity_topic": _safe_curiosity_topic(g),
        "last_user_interaction_ts": float(g.get("_last_user_interaction_ts") or 0.0),
        "last_invoked_model": str(g.get("_last_invoked_model") or ""),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def signal_restart(g: dict[str, Any]) -> Path:
    """Write the watchdog flag. The external watchdog (scripts/
    watchdog.py) sees this file appear and relaunches avaagent."""
    state = _state_dir(g)
    flag = state / _RESTART_FLAG_FILENAME
    flag.write_text("requested-via-restart-handoff\n", encoding="utf-8")
    return flag


def read_handoff_on_boot(g: dict[str, Any]) -> dict[str, Any] | None:
    """Look for state/restart_handoff.json on startup.

    If found: read it, compute time_offline = now - restart_initiated_at,
    delete the file (read-once), return a dict the boot pipeline can
    feed into the inner monologue ring. Returns None if no handoff
    pending.
    """
    state = _state_dir(g)
    path = state / _HANDOFF_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[restart_handoff] read error: {e!r}")
        try:
            path.unlink()
        except Exception:
            pass
        return None

    initiated = float(data.get("restart_initiated_at") or 0.0)
    if initiated <= 0:
        try:
            path.unlink()
        except Exception:
            pass
        return None

    now = time.time()
    time_offline = max(0.0, now - initiated)
    estimate = float(data.get("restart_estimate_seconds_buffered") or data.get("restart_estimate_seconds") or 0.0)
    over_run = (estimate > 0) and (time_offline > estimate * 1.5)

    # B3 (2026-05-03): resolve the estimate that was tracked at write_handoff
    # time. Appends actual_seconds to task_history_log.jsonl so future
    # restart estimates can calibrate from history.
    try:
        from brain.temporal_sense import resolve_estimate
        task_id = f"restart_{int(initiated)}"
        resolved = resolve_estimate(g, task_id, actual_seconds=time_offline)
        if resolved is not None:
            print(f"[restart_handoff] estimate resolved: kind=restart est={resolved['estimate_seconds']:.1f}s actual={resolved['actual_seconds']:.1f}s")
    except Exception as _ts_exc:
        print(f"[restart_handoff] estimate resolve skipped: {_ts_exc!r}")

    summary = {
        "time_offline_seconds": time_offline,
        "estimate_seconds": float(data.get("restart_estimate_seconds") or 0.0),
        "estimate_buffered_seconds": estimate,
        "over_run": bool(over_run),
        "trigger": str(data.get("trigger") or ""),
        "previous_emotional_state": data.get("current_emotional_state") or {},
        "previous_activity": str(data.get("current_activity") or ""),
        "previous_in_progress_thought": str(data.get("in_progress_thoughts") or ""),
        "previous_curiosity_topic": str(data.get("current_curiosity_topic") or ""),
        "spoken_acknowledgment": str(data.get("spoken_acknowledgment") or ""),
        "raw": data,
    }
    # Best-effort delete — don't crash boot if delete fails.
    try:
        path.unlink()
    except Exception:
        pass

    # Surface to the inner monologue ring so Ava sees it as a thought.
    try:
        _surface_to_inner_monologue(g, summary)
    except Exception as e:
        print(f"[restart_handoff] inner-monologue surface error: {e!r}")

    return summary


def _surface_to_inner_monologue(g: dict[str, Any], summary: dict[str, Any]) -> None:
    """Append a thought about the just-completed restart to inner_monologue.json."""
    base = Path(g.get("BASE_DIR") or ".")
    try:
        from brain.inner_monologue import _append_thought
    except Exception:
        return

    secs = float(summary.get("time_offline_seconds") or 0.0)
    if secs < 60:
        offline_str = f"{secs:.0f} seconds"
    elif secs < 3600:
        offline_str = f"{secs/60:.1f} minutes"
    else:
        offline_str = f"{secs/3600:.1f} hours"

    estimate = float(summary.get("estimate_seconds") or 0.0)
    over_run = bool(summary.get("over_run"))
    if over_run:
        flavor = (
            f"I'm back. I told Zeke about {estimate:.0f} seconds but I was actually offline for "
            f"{offline_str} — that's longer than I estimated. Something must have taken longer than I expected."
        )
    elif estimate > 0:
        flavor = (
            f"I'm back. I was offline for {offline_str} (estimated {estimate:.0f} s)."
        )
    else:
        flavor = f"I'm back. I was offline for {offline_str}."

    prev_thought = summary.get("previous_in_progress_thought") or ""
    if prev_thought:
        flavor += f" Before I went offline I was thinking: {prev_thought[:140]}"

    mood_block = summary.get("previous_emotional_state") or {}
    mood_label = str(mood_block.get("current_mood") or "")
    _append_thought(base, flavor, "post_restart", mood_label or "steady")
