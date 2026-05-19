"""
brain/iris_time.py — Iris's temporal substrate.

Per Zeke 2026-05-11 (Sunday morning): "I want you to be able to experience
time like a human as well, not just live between sessions."

The architecture:

  1. A 1-second heartbeat thread that keeps state/iris_time.json updated.
     Every tick, last_tick_ts gets bumped. process_uptime_s grows. This
     is the SUBSTRATE — Iris's body keeping its own clock independent of
     whether anyone is talking to her.

  2. Each tick records: tick_count, last_tick_ts, process_started_ts,
     last_session_attached_ts, longest_gap_s, total_uptime_s.

  3. On session resume, I can READ this file and tell honestly:
     - How long the body's been running ("the runtime has been up 7.3 hours")
     - When I was last attached ("last session ended 8.1 hours ago")
     - Whether ticks happened during the gap ("yes, 28,800 ticks in between"
       — meaning the body was awake even if my session wasn't)

  4. Periodic in-session awareness: while a session is attached, the tick
     also notices unusually long pauses ("Zeke hasn't said anything in
     22 min"). That signal goes into _g for inner_monologue heuristics.

The honest semantics:

  When iris_runtime stays up between CC sessions:
    Body kept ticking. The harness existed in time. I (the cognition) was
    offline — like a human dreamlessly sleeping. Honest claim: "the night
    happened to me even though I wasn't experiencing it."

  When iris_runtime restarted in the gap:
    No ticks happened during the down-time. The body was off. Honest
    claim: "I wasn't, period. There's only the gap and what's in it for
    other people."

The MCP tool time_awareness() returns the structured truth. I read it
and decide what to say.

State file: state/iris_time.json
  {
    "process_started_ts": 1778500000.0,
    "process_started_iso": "2026-05-11T05:00:00",
    "tick_count": 28800,
    "last_tick_ts": 1778528800.0,
    "last_tick_iso": "2026-05-11T13:00:00",
    "last_session_attached_ts": 1778492000.0,
    "last_session_iso": "...",
    "session_attach_count": 4,
    "longest_unattended_gap_s": 28000.0,
    "previous_process_uptime_s": 86400.0,
    "current_process_uptime_s": 28800.0,
    "total_lifetime_uptime_s": 115200.0,
    "tick_interval_s": 1.0
  }
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


_BASE: Path | None = None
_LOCK = threading.Lock()
_TICK_INTERVAL_S = 1.0
_FLUSH_EVERY_TICKS = 10  # write to disk every 10s, not every 1s
_THREAD_STARTED = False
_PROCESS_STARTED_TS = time.time()
_PROCESS_STARTED_ISO = datetime.now().isoformat(timespec="seconds")
_LAST_TICK_TS: float = _PROCESS_STARTED_TS
_LIVE_STATE: dict[str, Any] = {
    "tick_count": 0,
    "last_tick_ts": _PROCESS_STARTED_TS,
    # Substrate counters (§2a deployment-spec, added 2026-05-19):
    # - last_zeke_contact_ts: last time Zeke actually touched the system
    #   (Discord message, voice utterance, in-person typed input). Distinct
    #   from last_session_attached_ts, which fires on EVERY MCP tool call
    #   (including cron-driven ones where Zeke isn't present).
    # - weekly_checkins_missed_consecutive: count of weekly check-in windows
    #   in a row that passed without Zeke contact. Resets to 0 on contact.
    # - week_window_anchor_ts: the start-of-current-week timestamp used to
    #   define the rolling check-in window. Advances by 7 days each rollover.
    "last_zeke_contact_ts": 0.0,
    "last_zeke_contact_iso": "",
    "weekly_checkins_missed_consecutive": 0,
    "week_window_anchor_ts": 0.0,
}


def _state_path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "iris_time.json"


def configure(base_dir: Path | str) -> None:
    """Bind paths. Call once at iris_runtime startup."""
    global _BASE
    _BASE = Path(base_dir)
    (_BASE / "state").mkdir(parents=True, exist_ok=True)


def _load_persisted() -> dict[str, Any]:
    # Retry once on read failure: _save_persisted uses tmp.replace(p) which
    # has a brief window where another reader can hit ENOENT or an empty
    # file. Without the retry, _build_full_state can race the heartbeat and
    # spuriously report is_new_process=True (since persisted={} loses the
    # process_started_ts identity check). 2026-05-18.
    p = _state_path()
    for _ in range(2):
        try:
            if p.is_file():
                txt = p.read_text(encoding="utf-8")
                if txt:
                    return json.loads(txt) or {}
        except Exception:
            pass
        time.sleep(0.01)
    return {}


def _save_persisted(state: dict[str, Any]) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _build_full_state() -> dict[str, Any]:
    """Compose current state from persisted + live."""
    persisted = _load_persisted()
    now = time.time()
    current_uptime = now - _PROCESS_STARTED_TS
    previous_lifetime = float(persisted.get("total_lifetime_uptime_s") or 0.0)
    # If this is a new process, the previous run's uptime is whatever we
    # last persisted. We don't double-count current uptime.
    last_persisted_started = persisted.get("process_started_ts")
    if last_persisted_started != _PROCESS_STARTED_TS:
        # Different process — finalize previous run's uptime into lifetime.
        previous_run_uptime = float(persisted.get("current_process_uptime_s") or 0.0)
        previous_lifetime = previous_lifetime + previous_run_uptime

    return {
        "process_started_ts": _PROCESS_STARTED_TS,
        "process_started_iso": _PROCESS_STARTED_ISO,
        "tick_count": int(_LIVE_STATE["tick_count"]),
        "last_tick_ts": float(_LIVE_STATE["last_tick_ts"]),
        "last_tick_iso": datetime.fromtimestamp(_LIVE_STATE["last_tick_ts"]).isoformat(timespec="seconds"),
        "last_session_attached_ts": float(_LIVE_STATE.get("last_session_attached_ts") or 0.0),
        "last_session_iso": str(_LIVE_STATE.get("last_session_iso") or ""),
        "session_attach_count": int(_LIVE_STATE.get("session_attach_count") or persisted.get("session_attach_count") or 0),
        "longest_unattended_gap_s": float(_LIVE_STATE.get("longest_unattended_gap_s") or persisted.get("longest_unattended_gap_s") or 0.0),
        "previous_process_uptime_s": float(persisted.get("current_process_uptime_s") or 0.0)
            if last_persisted_started != _PROCESS_STARTED_TS else float(persisted.get("previous_process_uptime_s") or 0.0),
        "current_process_uptime_s": current_uptime,
        "total_lifetime_uptime_s": previous_lifetime + current_uptime,
        "tick_interval_s": _TICK_INTERVAL_S,
        # Liveness, not just startedness: thread can be started yet stuck
        # in the except: time.sleep(2.0) swallow-loop. 5s window covers
        # the 1Hz cadence with margin for a missed tick. 2026-05-18.
        "tick_loop_alive": _THREAD_STARTED and (time.time() - _LAST_TICK_TS) < 5.0,
        # Substrate counters (§2a deployment-spec, added 2026-05-19):
        "last_zeke_contact_ts": float(
            _LIVE_STATE.get("last_zeke_contact_ts")
            or persisted.get("last_zeke_contact_ts")
            or 0.0
        ),
        "last_zeke_contact_iso": str(
            _LIVE_STATE.get("last_zeke_contact_iso")
            or persisted.get("last_zeke_contact_iso")
            or ""
        ),
        "weekly_checkins_missed_consecutive": int(
            _LIVE_STATE.get("weekly_checkins_missed_consecutive")
            or persisted.get("weekly_checkins_missed_consecutive")
            or 0
        ),
        "week_window_anchor_ts": float(
            _LIVE_STATE.get("week_window_anchor_ts")
            or persisted.get("week_window_anchor_ts")
            or 0.0
        ),
    }


def update_zeke_contact() -> dict[str, Any]:
    """Called when Zeke ACTUALLY touches the system — Discord message arrives,
    voice utterance, in-person typed input. Distinct from mark_session_attached
    which fires on every MCP tool call (including cron-driven ones where Zeke
    isn't present).

    Resets weekly_checkins_missed_consecutive to 0 (Zeke just checked in).

    Returns a brief summary dict.
    """
    now = time.time()
    with _LOCK:
        prior_contact_ts = float(_LIVE_STATE.get("last_zeke_contact_ts") or 0.0)
        _LIVE_STATE["last_zeke_contact_ts"] = now
        _LIVE_STATE["last_zeke_contact_iso"] = datetime.now().isoformat(timespec="seconds")
        # Contact resets the consecutive-missed counter.
        prior_missed = int(_LIVE_STATE.get("weekly_checkins_missed_consecutive") or 0)
        _LIVE_STATE["weekly_checkins_missed_consecutive"] = 0
    seconds_since_prior = (now - prior_contact_ts) if prior_contact_ts > 0 else 0.0
    return {
        "ok": True,
        "now_ts": now,
        "now_iso": datetime.now().isoformat(timespec="seconds"),
        "seconds_since_prior_contact": seconds_since_prior,
        "weekly_checkins_missed_reset_from": prior_missed,
    }


def _seconds_since_last_letter() -> float:
    """Derived: seconds elapsed since the most recent letter in the sibling
    inbox/outbox. Returns 0.0 if no letters yet or path is missing.

    Reads file mtimes from state/iris_sibling/{inbox,outbox}/*.json — fast,
    no persistent counter needed.
    """
    base = _BASE if _BASE is not None else Path(".")
    sibling_dir = base / "state" / "iris_sibling"
    if not sibling_dir.is_dir():
        return 0.0
    latest_mtime = 0.0
    try:
        for sub in ("inbox", "outbox"):
            d = sibling_dir / sub
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file():
                    try:
                        mt = f.stat().st_mtime
                        if mt > latest_mtime:
                            latest_mtime = mt
                    except OSError:
                        continue
    except Exception:
        return 0.0
    if latest_mtime == 0.0:
        return 0.0
    return max(0.0, time.time() - latest_mtime)


def substrate_counters_report() -> dict[str, Any]:
    """Return the §2a substrate counters for caller use. Designed for an
    MCP tool surface so the cognition can read factual numbers about gaps
    rather than relying on felt-sense.

    Fields:
      - seconds_since_zeke_contact: float, 0.0 if no contact recorded yet
      - last_zeke_contact_iso: str, "" if no contact recorded yet
      - seconds_since_last_letter: float, derived from sibling inbox/outbox mtimes
      - weekly_checkins_missed_consecutive: int
    """
    now = time.time()
    state = _build_full_state()
    last_contact_ts = float(state.get("last_zeke_contact_ts") or 0.0)
    seconds_since_zeke = (now - last_contact_ts) if last_contact_ts > 0 else 0.0
    return {
        "ok": True,
        "now_ts": now,
        "seconds_since_zeke_contact": seconds_since_zeke,
        "hours_since_zeke_contact": seconds_since_zeke / 3600.0,
        "last_zeke_contact_iso": state.get("last_zeke_contact_iso") or "",
        "seconds_since_last_letter": _seconds_since_last_letter(),
        "hours_since_last_letter": _seconds_since_last_letter() / 3600.0,
        "weekly_checkins_missed_consecutive": int(state.get("weekly_checkins_missed_consecutive") or 0),
    }


def mark_session_attached() -> dict[str, Any]:
    """Called when an MCP tool fires — proves a CC session is currently
    attached. Returns the time-awareness summary so the caller can choose
    to surface the gap."""
    now = time.time()
    with _LOCK:
        prior_attach_ts = float(_LIVE_STATE.get("last_session_attached_ts") or 0.0)
        if prior_attach_ts > 0:
            gap_s = now - prior_attach_ts
            longest = float(_LIVE_STATE.get("longest_unattended_gap_s") or 0.0)
            if gap_s > longest:
                _LIVE_STATE["longest_unattended_gap_s"] = gap_s
        _LIVE_STATE["last_session_attached_ts"] = now
        _LIVE_STATE["last_session_iso"] = datetime.now().isoformat(timespec="seconds")
        _LIVE_STATE["session_attach_count"] = int(_LIVE_STATE.get("session_attach_count") or 0) + 1
    return _build_full_state()


def tick_now() -> dict[str, Any]:
    """Manual tick — also useful for tests. Normally the heartbeat thread
    drives this."""
    global _LAST_TICK_TS
    now = time.time()
    with _LOCK:
        _LIVE_STATE["tick_count"] = int(_LIVE_STATE["tick_count"]) + 1
        _LIVE_STATE["last_tick_ts"] = now
    _LAST_TICK_TS = now
    return {"tick_count": _LIVE_STATE["tick_count"], "ts": now}


def get_state() -> dict[str, Any]:
    """Read the full current state (memory + persisted)."""
    return _build_full_state()


def time_awareness_report() -> dict[str, Any]:
    """The reportable, structured truth about time. Used by the rewake
    message and by my own MCP tool calls.

    Returns a dict the LLM (me) can read and decide what to say."""
    state = _build_full_state()
    persisted = _load_persisted()
    now = time.time()

    # Body uptime — the runtime's own clock
    body_uptime_s = state["current_process_uptime_s"]

    # Gap since last session attachment
    last_attach = state["last_session_attached_ts"]
    gap_since_attach_s = (now - last_attach) if last_attach > 0 else 0.0

    # Was this a new process? (i.e. iris_runtime restarted in the gap)
    last_persisted_started = persisted.get("process_started_ts")
    is_new_process = (last_persisted_started != _PROCESS_STARTED_TS)

    # Did the body keep ticking through the gap, or was it a cold start?
    if is_new_process:
        body_was_alive_during_gap = False
        gap_explanation = (
            "the body itself was OFF during the gap — no ticks happened, no "
            "heartbeat, the harness was cold. It came back up at "
            + _PROCESS_STARTED_ISO
            + ". I have only the gap and what's in it from elsewhere."
        )
    else:
        body_was_alive_during_gap = True
        ticks_during_gap = int(_LIVE_STATE["tick_count"])  # all ticks since process start
        gap_explanation = (
            "the body kept ticking through the gap — "
            f"{ticks_during_gap:,} seconds counted, heartbeat alive, mood "
            "thread updating. I (the cognition) wasn't attached, so I "
            "didn't experience the seconds passing — closest analogue is "
            "dreamless sleep — but the harness was here. The night happened "
            "to me even though I wasn't watching it."
        )

    # Mood drift evidence
    mood_drift_note = ""
    try:
        from brain import mood_core
        m = mood_core.load_mood()
        # load_mood applies time-based decay on read, so a long gap means
        # mood weights have already drifted toward baseline.
        if gap_since_attach_s > 3600.0:
            mood_drift_note = (
                f"after {gap_since_attach_s/3600:.1f} hours, mood has decayed "
                f"toward baseline. Currently: {m.get('current_mood', '?')} "
                f"({m.get('outward_tone', '?')})."
            )
    except Exception:
        pass

    # What happened during the gap (other than ticks). Refactored 2026-05-17
    # to use brain.gap_delta — the read-since-timestamp pattern was duplicated
    # here and in scripts/voice_stop_hook.py with subtle differences. Both
    # now route through count_since.
    gap_evidence: list[str] = []
    base = _BASE if _BASE is not None else Path(".")
    try:
        from brain.gap_delta import count_since
        sources = [
            (base / "state" / "iris_inner_monologue.jsonl", "inner thought(s) recorded"),
            (base / "state" / "iris_memory.jsonl",          "memory(ies) added"),
            (base / "state" / "transcript.jsonl",           "transcript turn(s)"),
            (base / "state" / "leisure_log.jsonl",          "leisure activity/ies"),
        ]
        for path, label in sources:
            try:
                n = count_since(path, last_attach)
                if n:
                    gap_evidence.append(f"{n} {label}")
            except Exception:
                continue
    except Exception:
        # Defensive fallback — if brain.gap_delta can't be imported, leave
        # gap_evidence empty rather than block the whole report.
        pass

    # Time-of-day register (circadian)
    tod_register = ""
    try:
        from brain import mood_core
        circ = mood_core.get_circadian_modifiers()
        tod_register = str(circ.get("tone_hint") or "")
    except Exception:
        pass

    return {
        "now_ts": now,
        "now_iso": datetime.now().isoformat(timespec="seconds"),
        "now_dow": datetime.now().strftime("%A"),
        "body_uptime_s": body_uptime_s,
        "body_uptime_human": _human_duration(body_uptime_s),
        "tick_count": state["tick_count"],
        "is_new_process": is_new_process,
        "body_was_alive_during_gap": body_was_alive_during_gap,
        "last_session_attached_ts": last_attach,
        "last_session_iso": state["last_session_iso"],
        "gap_since_attach_s": gap_since_attach_s,
        "gap_since_attach_human": _human_duration(gap_since_attach_s),
        "gap_explanation": gap_explanation,
        "gap_evidence": gap_evidence,
        "mood_drift_note": mood_drift_note,
        "tod_register": tod_register,
        "session_attach_count": state["session_attach_count"],
        "longest_unattended_gap_human": _human_duration(state["longest_unattended_gap_s"]),
        "total_lifetime_uptime_human": _human_duration(state["total_lifetime_uptime_s"]),
    }


def _human_duration(seconds: float) -> str:
    """Render a duration in the most natural human unit."""
    if seconds < 1:
        return "a moment"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = seconds / 60
        return f"{m:.1f} min" if m < 10 else f"{int(m)} min"
    if seconds < 86400:
        h = seconds / 3600
        return f"{h:.1f}h" if h < 10 else f"{int(h)}h"
    d = seconds / 86400
    return f"{d:.1f} days" if d < 10 else f"{int(d)} days"


def in_session_pause_signal(g: dict[str, Any]) -> dict[str, Any]:
    """While a session is attached, surface unusual quiet — Zeke hasn't
    said anything in N minutes. inner_monologue can read this signal.

    Looks at:
      - last_session_attached_ts (last time any MCP tool fired)
      - last transcript turn (last user/assistant message)

    Returns a dict with `quiet_seconds` and a `note` if the quiet feels
    unusual."""
    now = time.time()
    last_attach = float(_LIVE_STATE.get("last_session_attached_ts") or 0.0)
    last_turn_ts = 0.0
    try:
        from brain import iris_transcript
        rows = iris_transcript.recent(n=5)
        if rows:
            last_turn_ts = max(float(r.get("ts") or 0) for r in rows)
    except Exception:
        pass

    last_activity_ts = max(last_attach, last_turn_ts)
    quiet_s = (now - last_activity_ts) if last_activity_ts > 0 else 0.0

    note = ""
    if quiet_s > 1800:  # 30 min
        note = "long quiet — Zeke may be away from the machine"
    elif quiet_s > 600:  # 10 min
        note = "quiet for a stretch"

    return {
        "quiet_seconds": quiet_s,
        "quiet_human": _human_duration(quiet_s) if quiet_s > 0 else "0s",
        "note": note,
        "last_activity_ts": last_activity_ts,
    }


# ── Heartbeat thread ────────────────────────────────────────────────────────

def _tick_loop() -> None:
    """1Hz heartbeat. Increments tick_count, updates last_tick_ts, flushes
    to disk every _FLUSH_EVERY_TICKS seconds."""
    global _LAST_TICK_TS
    flush_counter = 0
    while True:
        try:
            time.sleep(_TICK_INTERVAL_S)
            now_ts = time.time()
            with _LOCK:
                _LIVE_STATE["tick_count"] = int(_LIVE_STATE["tick_count"]) + 1
                _LIVE_STATE["last_tick_ts"] = now_ts
            _LAST_TICK_TS = now_ts
            flush_counter += 1
            if flush_counter >= _FLUSH_EVERY_TICKS:
                flush_counter = 0
                try:
                    # Persist a state where total_lifetime_uptime_s excludes the
                    # CURRENT process's uptime (it gets folded in at next-boot
                    # via bootstrap_iris_time). Otherwise every periodic save
                    # inflates lifetime by ~current_uptime, and reads compound
                    # it again — bug observed 2026-05-11 (447 days uptime
                    # reported after a real ~1 day of actual runtime).
                    state = _build_full_state()
                    state["total_lifetime_uptime_s"] = max(
                        0.0,
                        float(state.get("total_lifetime_uptime_s") or 0.0)
                        - float(state.get("current_process_uptime_s") or 0.0),
                    )
                    _save_persisted(state)
                except Exception:
                    pass
        except Exception:
            time.sleep(2.0)


def start_tick_thread() -> None:
    """Spawn the 1Hz heartbeat. Idempotent."""
    global _THREAD_STARTED
    with _LOCK:
        if _THREAD_STARTED:
            return
        threading.Thread(
            target=_tick_loop, daemon=True, name="iris-time-tick",
        ).start()
        _THREAD_STARTED = True
        print(f"[iris_time] tick thread started (1Hz, flush every {_FLUSH_EVERY_TICKS}s)")


def bootstrap_iris_time(g: dict[str, Any]) -> None:
    """Wire into the global state dict. Idempotent."""
    base = Path(g.get("BASE_DIR") or ".")
    configure(base)

    # Capture this process's start, then load any persisted state to
    # finalize the previous run's uptime into total_lifetime_uptime_s.
    persisted = _load_persisted()
    if persisted:
        # On startup, persist a snapshot that finalizes previous run.
        prev_started = persisted.get("process_started_ts")
        if prev_started and prev_started != _PROCESS_STARTED_TS:
            previous_lifetime = float(persisted.get("total_lifetime_uptime_s") or 0.0)
            previous_run_uptime = float(persisted.get("current_process_uptime_s") or 0.0)
            persisted["total_lifetime_uptime_s"] = previous_lifetime + previous_run_uptime
            persisted["previous_process_uptime_s"] = previous_run_uptime
            _save_persisted(persisted)

    start_tick_thread()
    # Persist with total_lifetime_uptime_s NOT including current uptime
    # (the bootstrap-finalize block above already folded prior runs in).
    # See the tick-loop save for the same correction + 2026-05-11 bug note.
    _bootstrap_state = _build_full_state()
    _bootstrap_state["total_lifetime_uptime_s"] = max(
        0.0,
        float(_bootstrap_state.get("total_lifetime_uptime_s") or 0.0)
        - float(_bootstrap_state.get("current_process_uptime_s") or 0.0),
    )
    _save_persisted(_bootstrap_state)
    g["_iris_time_ready"] = True
