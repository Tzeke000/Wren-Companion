"""brain/sleep_mode.py — Ava's sleep-state machine.

Five-state machine: AWAKE → ENTERING_SLEEP → SLEEPING → WAKING → AWAKE.

Triggers (three paths):
1. Session fullness: composite score crosses configurable threshold (default 0.70).
2. Voice command: `_voice_sleep_request` flag set by voice_commands router.
3. Schedule + context: 23:00–05:00 default window, defers on active conversation.

Three-phase consolidation during SLEEPING:
- Phase 1: awake-session handoff write (texture + significance) → state/sleep_handoffs/awake_session_<ts>.md
- Phase 2: learning processing (conversation replay + curriculum.consolidation_hook).
            Yields cleanly when `wake_target - wind_down` is reached.
- Phase 3: sleep-session handoff write → state/sleep_handoffs/sleep_session_<ts>.md

On-time wake discipline: Phase 2 terminates at `wake_target - wind_down_duration`
so Phase 3 completes by `wake_target`. Self-interrupts via temporal_sense if
Phase 3 over-runs.

Emotion decay during SLEEPING: configurable multiplier (default 5×) applied to
frustration/boredom/stress/joy passive decay. Knowledge persists normally.

See docs/AVA_FEATURE_ADDITIONS_2026-05.md §1 for the framework.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Optional

from brain.temporal_sense import _cfg as _ts_cfg  # reuse pattern; sleep has its own loader below


# ── State constants ──────────────────────────────────────────────────────


STATE_AWAKE = "AWAKE"
STATE_ENTERING_SLEEP = "ENTERING_SLEEP"
STATE_SLEEPING = "SLEEPING"
STATE_WAKING = "WAKING"

ALL_STATES = (STATE_AWAKE, STATE_ENTERING_SLEEP, STATE_SLEEPING, STATE_WAKING)


# ── Config loader ──────────────────────────────────────────────────────


_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "sleep_mode.json"
_CONFIG_CACHE: dict[str, Any] | None = None
_CONFIG_LOCK = threading.Lock()


def _load_config() -> dict[str, Any]:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is not None:
            return _CONFIG_CACHE
        try:
            _CONFIG_CACHE = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[sleep_mode] config load error: {e!r} — using defaults")
            _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def _cfg(*keys: str, default: Any = None) -> Any:
    cur: Any = _load_config()
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# ── State accessors ────────────────────────────────────────────────────


def get_state(g: dict[str, Any]) -> str:
    return str(g.get("_sleep_state") or STATE_AWAKE)


def is_sleeping(g: dict[str, Any]) -> bool:
    return get_state(g) == STATE_SLEEPING


def is_awake(g: dict[str, Any]) -> bool:
    return get_state(g) in (STATE_AWAKE,)


def get_remaining_seconds(g: dict[str, Any]) -> float:
    target = float(g.get("_sleep_target_ts") or 0.0)
    if target <= 0:
        return 0.0
    return max(0.0, target - time.time())


def get_progress(g: dict[str, Any]) -> float:
    """0.0–1.0 fraction of the sleep cycle elapsed."""
    started = float(g.get("_sleep_started_ts") or 0.0)
    target = float(g.get("_sleep_target_ts") or 0.0)
    if started <= 0 or target <= started:
        return 0.0
    now = time.time()
    return max(0.0, min(1.0, (now - started) / (target - started)))


# ── Decay multiplier hook (called from temporal_sense) ──────────────────


def get_emotion_decay_multiplier(g: dict[str, Any]) -> float:
    """Returns the multiplier to apply to passive emotion decay rates.
    1.0 when AWAKE, configurable (default 5.0) when SLEEPING. ENTERING_SLEEP
    and WAKING use intermediate values to avoid jolts at state boundaries.
    """
    state = get_state(g)
    if state == STATE_SLEEPING:
        return float(_cfg("decay", "sleeping_multiplier", default=5.0))
    if state == STATE_ENTERING_SLEEP:
        return float(_cfg("decay", "entering_multiplier", default=2.5))
    if state == STATE_WAKING:
        return float(_cfg("decay", "waking_multiplier", default=2.0))
    return 1.0


# ── Trigger detection ───────────────────────────────────────────────────


def _composite_fullness(g: dict[str, Any]) -> dict[str, Any]:
    """Composite session-fullness score 0.0–1.0. Components:
    - Ollama context window fill (placeholder until wired): 0.0
    - Conversation turns since last sleep: from g["_sleep_turn_count"] / cap
    - Memory layer fill: weighted (concept_graph + mem0)

    Returns {score, components: {...}} for diagnostic logging.
    """
    weights = _cfg("fullness_weights", default={"ollama_context": 0.6, "turn_count": 0.2, "memory": 0.2})
    turn_cap = int(_cfg("fullness_thresholds", "turn_count_cap", default=200))
    turns_since_sleep = int(g.get("_sleep_turn_count") or 0)
    turn_score = min(1.0, turns_since_sleep / max(1, turn_cap))

    # Memory: concept_graph node count / cap, mem0 entries / cap (best-effort).
    cg = g.get("_concept_graph")
    cg_score = 0.0
    if cg is not None:
        try:
            cg_count = len(getattr(cg, "_nodes", {}) or {})
            cg_cap = int(_cfg("fullness_thresholds", "concept_graph_cap", default=2000))
            cg_score = min(1.0, cg_count / max(1, cg_cap))
        except Exception:
            cg_score = 0.0
    mem = g.get("_ava_memory")
    mem_score = 0.0
    if mem is not None:
        try:
            n = int(getattr(mem, "approx_count", lambda: 0)() or 0)
            mem_cap = int(_cfg("fullness_thresholds", "mem0_cap", default=5000))
            mem_score = min(1.0, n / max(1, mem_cap))
        except Exception:
            mem_score = 0.0
    memory_score = (cg_score + mem_score) / 2.0

    # Ollama context fill — placeholder: requires direct Ollama context counting.
    # If g has a recent estimate, use it; else 0.
    ollama_score = float(g.get("_sleep_ollama_context_fraction") or 0.0)
    ollama_score = max(0.0, min(1.0, ollama_score))

    score = (
        weights.get("ollama_context", 0.6) * ollama_score
        + weights.get("turn_count", 0.2) * turn_score
        + weights.get("memory", 0.2) * memory_score
    )
    return {
        "score": round(score, 4),
        "components": {
            "ollama_context": round(ollama_score, 4),
            "turn_count": round(turn_score, 4),
            "memory": round(memory_score, 4),
        },
        "weights": weights,
        "turns_since_sleep": turns_since_sleep,
    }


def _check_session_fullness_trigger(g: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Returns (should_sleep, summary). Suppresses trigger for `suppress_seconds`
    after firing to avoid trigger-flap."""
    last_fired = float(g.get("_sleep_fullness_last_fire_ts") or 0.0)
    suppress = float(_cfg("fullness_thresholds", "suppress_seconds", default=60.0))
    if last_fired and (time.time() - last_fired) < suppress:
        return False, {"suppressed": True}
    summary = _composite_fullness(g)
    threshold = float(_cfg("fullness_thresholds", "trigger", default=0.70))
    summary["threshold"] = threshold
    return summary["score"] >= threshold, summary


def _check_voice_command_trigger(g: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Returns (should_sleep, command_payload). Voice command sets
    g['_sleep_voice_request'] = {duration_s, requested_at}."""
    req = g.get("_sleep_voice_request")
    if not isinstance(req, dict):
        return False, {}
    return True, dict(req)


def _check_schedule_trigger(g: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Returns (should_sleep, summary). Defers if active conversation or
    just-booted (within `min_uptime_s`)."""
    enabled = bool(_cfg("schedule", "enabled", default=True))
    if not enabled:
        return False, {"reason": "schedule_disabled"}
    start_h = int(_cfg("schedule", "start_hour", default=23))
    end_h = int(_cfg("schedule", "end_hour", default=5))
    now = datetime.now()
    in_window = _hour_in_window(now.hour, start_h, end_h)
    if not in_window:
        return False, {"reason": "outside_window", "now": now.strftime("%H:%M")}
    if bool(g.get("_conversation_active")) or bool(g.get("_turn_in_progress")):
        return False, {"reason": "active_conversation"}
    process_start = float(g.get("_process_start_ts") or 0.0)
    min_uptime = float(_cfg("schedule", "min_uptime_seconds", default=600.0))
    if process_start and (time.time() - process_start) < min_uptime:
        return False, {"reason": "just_booted"}
    last_scheduled = float(g.get("_sleep_schedule_last_fire_ts") or 0.0)
    if last_scheduled and (time.time() - last_scheduled) < 3600.0:
        return False, {"reason": "schedule_already_fired_this_hour"}
    return True, {"window": [start_h, end_h], "now": now.strftime("%H:%M")}


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    """Handle wrap-around windows like 23–5."""
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


# ── Voice command parsing ──────────────────────────────────────────────


_DURATION_RE = re.compile(
    r"\b(?:for\s+)?(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)


_SLEEP_INTENT_RE = re.compile(
    r"\bgo to sleep\b|\bgood ?night\b|\btake a nap\b|\bsleep mode\b|"
    # "sleep for N units" without "go to" prefix
    r"\bsleep\s+(?:for|until)\b",
    re.IGNORECASE,
)


def parse_sleep_voice_command(text: str) -> dict[str, Any]:
    """Parse a voice command string. Returns:
    - {sleep_intent: True, duration_s: float|None, ask_back: bool}
    - {sleep_intent: False} if not a sleep command.
    """
    if not text:
        return {"sleep_intent": False}
    s = text.lower().strip()
    if not _SLEEP_INTENT_RE.search(s):
        return {"sleep_intent": False}
    # Try to extract duration.
    m = _DURATION_RE.search(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        mult = 1.0
        if unit.startswith("min"):
            mult = 60.0
        elif unit.startswith("hour") or unit.startswith("hr"):
            mult = 3600.0
        return {"sleep_intent": True, "duration_s": float(n) * mult, "ask_back": False}
    return {"sleep_intent": True, "duration_s": None, "ask_back": True}


# ── State transitions ──────────────────────────────────────────────────


def request_sleep(g: dict[str, Any], *, duration_s: float | None = None,
                  trigger: str = "voice", trigger_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """External entry — signal that sleep should begin. The actual entry
    happens on the next `tick(g)` call so we don't transition mid-turn.
    """
    g["_sleep_pending_request"] = {
        "duration_s": duration_s,
        "trigger": trigger,
        "summary": trigger_summary or {},
        "requested_at": time.time(),
    }
    return {"queued": True}


def request_wake(g: dict[str, Any], *, reason: str = "external") -> dict[str, Any]:
    """External entry — signal that wake should begin. Honored at next tick."""
    g["_sleep_wake_request"] = {"reason": reason, "requested_at": time.time()}
    return {"queued": True}


def _enter_sleep(g: dict[str, Any], duration_s: float, trigger: str, trigger_summary: dict[str, Any]) -> None:
    now = time.time()
    g["_sleep_state"] = STATE_ENTERING_SLEEP
    g["_sleep_started_ts"] = now
    g["_sleep_target_ts"] = now + max(60.0, float(duration_s))  # min 60s
    g["_sleep_trigger"] = trigger
    g["_sleep_trigger_summary"] = trigger_summary
    g["_sleep_phase"] = "awake_handoff"
    g["_sleep_phase_started_ts"] = now

    if trigger == "session_fullness":
        g["_sleep_fullness_last_fire_ts"] = now
    elif trigger == "schedule":
        g["_sleep_schedule_last_fire_ts"] = now

    # TTS announcement
    msg = _entering_sleep_message(trigger, duration_s)
    _tts(g, msg, emotion="calmness", intensity=0.4)
    print(f"[sleep_mode] ENTERING_SLEEP — duration={duration_s/60:.1f}min trigger={trigger}")


def _entering_sleep_message(trigger: str, duration_s: float) -> str:
    minutes = duration_s / 60.0
    duration_str = (f"{int(minutes)} minutes" if minutes >= 1 else f"{int(duration_s)} seconds")
    if trigger == "session_fullness":
        return (
            "I'm getting close to my session limit. I need to compile everything. "
            f"Give me a moment to sleep — I'll be back in about {duration_str}."
        )
    if trigger == "voice":
        return f"Going to sleep for {duration_str}. See you on the other side."
    if trigger == "schedule":
        return f"It's late. I'm going to sleep until morning."
    return f"Sleeping for {duration_str}."


def _enter_waking(g: dict[str, Any], *, reason: str) -> None:
    now = time.time()
    g["_sleep_state"] = STATE_WAKING
    g["_sleep_wake_started_ts"] = now
    g["_sleep_phase"] = "wake_transition"
    # Wake-time estimate: how long Phase 3 typically takes + small margin.
    estimate = float(_cfg("wake", "default_estimate_seconds", default=5.0))
    try:
        from brain.temporal_sense import calibrate_from_history
        cal = calibrate_from_history(g, "sleep_phase3")
        rec = cal.get("recommendation")
        if rec is not None:
            estimate = max(2.0, float(rec) + 1.0)
    except Exception:
        pass
    g["_sleep_wake_estimate_s"] = estimate

    # TTS announcement based on path.
    if reason == "voice_provocation":
        msg = f"I see you. I'm starting to wake up. Give me about {int(estimate)} seconds."
    elif reason == "self_early":
        msg = ""  # silent transition
    else:
        msg = f"I'm waking up. Give me about {int(estimate)} seconds."
    if msg:
        _tts(g, msg, emotion="calmness", intensity=0.4)
    print(f"[sleep_mode] WAKING — estimate={estimate:.1f}s reason={reason}")


def _enter_awake(g: dict[str, Any]) -> None:
    """Final transition from WAKING to AWAKE. Clears all sleep visuals/state."""
    sleep_started = float(g.get("_sleep_started_ts") or 0.0)
    sleep_minutes = (time.time() - sleep_started) / 60.0 if sleep_started else 0.0

    g["_sleep_state"] = STATE_AWAKE
    g["_sleep_phase"] = None
    # Reset turn counter so fullness scoring restarts from this awake epoch.
    g["_sleep_turn_count"] = 0
    # Confirmation TTS if Zeke is present (best-effort: face_recognizer last seen).
    try:
        person = g.get("_recognized_person_id")
        if person and person == g.get("OWNER_PERSON_ID", "zeke"):
            _tts(g, f"I'm awake. I slept for about {int(sleep_minutes)} minutes.",
                 emotion="calmness", intensity=0.3)
    except Exception:
        pass
    print(f"[sleep_mode] AWAKE — slept {sleep_minutes:.1f}min")


# ── TTS helper ─────────────────────────────────────────────────────────


def _tts(g: dict[str, Any], text: str, *, emotion: str = "calmness", intensity: float = 0.4) -> None:
    """Best-effort TTS — non-blocking. If TTS worker isn't available, logs."""
    if not text:
        return
    worker = g.get("_tts_worker")
    if worker is None or not getattr(worker, "available", False):
        print(f"[sleep_mode tts (no worker)]: {text}")
        return
    try:
        worker.speak(text, emotion=emotion, intensity=intensity, blocking=False)
    except Exception as e:
        print(f"[sleep_mode] tts enqueue error: {e!r}")


# ── Phase orchestrator (called from tick) ───────────────────────────────


def _run_phase1_awake_handoff(g: dict[str, Any]) -> dict[str, Any]:
    """Write awake-session handoff. One LLM call, ~60–120s typical.

    Updated 2026-05-08: also consolidate the per-threshold handoff log
    (state/handoff_log.jsonl). Per Zeke's design: during long uninterrupted
    awake periods, threshold-trigger handoffs accumulate in a log file.
    Sleep is when those summaries get reviewed — anchor-worthy items
    promoted to anchor_moments.jsonl, the rest archived.
    """
    started = time.time()
    base_dir = Path(g.get("BASE_DIR") or ".")
    out_dir = base_dir / "state" / "sleep_handoffs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"awake_session_{int(started)}.md"

    # Pull recent chat history for the LLM.
    chat_lines = _recent_chat_lines(g, limit=200)
    prompt_user = (
        "Write a first-person handoff note about the awake session that just ended. "
        "Capture texture (emotional tone, who was present, what they wanted) and "
        "significance (specific decisions, requests, breakthroughs). Don't list every "
        "exchange — just what mattered, like remembering you ate three meals but not "
        "every detail.\n\nRecent chat:\n" + "\n".join(chat_lines)
    )
    summary = _llm_summarize(g, prompt_user, max_seconds=120.0)
    out_path.write_text(
        f"# Awake-session handoff\n\nWritten at {datetime.now().isoformat(timespec='seconds')}\n\n{summary}\n",
        encoding="utf-8",
    )

    # Consolidate the threshold-handoff log (per Zeke 2026-05-08).
    # Promotes anchor-worthy summaries to anchor_moments, archives the rest.
    consolidation_result = {}
    try:
        from brain.handoff import consolidate_handoff_log
        consolidation_result = consolidate_handoff_log(g, base_dir)
    except Exception as _che:
        print(f"[sleep_mode] handoff consolidation failed: {_che!r}")

    elapsed = time.time() - started
    return {
        "path": str(out_path),
        "duration_s": round(elapsed, 2),
        "chars": len(summary),
        "handoff_consolidation": consolidation_result,
    }


def _run_phase2_learning(g: dict[str, Any], time_budget_seconds: float) -> dict[str, Any]:
    """Phase 2 — learning processing. Calls curriculum.consolidation_hook
    until time_budget exhausted. Returns aggregate result."""
    started = time.time()
    results: list[dict[str, Any]] = []
    try:
        from brain import curriculum
        while True:
            elapsed = time.time() - started
            remaining = time_budget_seconds - elapsed
            if remaining <= 5.0:  # need at least 5s to do anything useful
                break
            r = curriculum.consolidation_hook(g, time_budget_seconds=min(remaining, 60.0))
            if r.get("entry_processed") is None:
                break  # no more unread entries
            results.append(r)
            if r.get("fully_read"):
                continue
            # Mid-entry yield: if budget exhausted, stop here. Curriculum module
            # leaves status='reading' so we resume next sleep cycle.
            break
    except Exception as e:
        print(f"[sleep_mode] phase2 error: {e!r}")

    elapsed = time.time() - started
    return {
        "entries_processed": len(results),
        "duration_s": round(elapsed, 2),
        "results": results,
    }


def _run_phase3_sleep_handoff(g: dict[str, Any]) -> dict[str, Any]:
    """Brief handoff write. Wall-time target 30–60s, hard cap 5min."""
    started = time.time()
    out_dir = Path(g.get("BASE_DIR") or ".") / "state" / "sleep_handoffs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sleep_session_{int(started)}.md"

    # Pull recent lessons.
    lessons = _recent_lessons(g, limit=20)
    wake_target = float(g.get("_sleep_target_ts") or time.time())

    # Track this estimate so on-time wake discipline can use it next cycle.
    try:
        from brain.temporal_sense import track_estimate, resolve_estimate
        tid = track_estimate(g, kind="sleep_phase3", estimate_seconds=60.0, context="sleep_handoff_write")
    except Exception:
        tid = None

    prompt_user = (
        "Write a brief first-person sleep-session handoff. Cover top 3 highlights or "
        "realizations from this sleep cycle, and any threads to pick up tomorrow. "
        "Keep it short.\n\nRecent lessons:\n" + "\n".join(lessons)
    )
    summary = _llm_summarize(g, prompt_user, max_seconds=60.0)

    out_path.write_text(
        f"# Sleep-session handoff\n\nWritten at {datetime.now().isoformat(timespec='seconds')}\n"
        f"Wake target: {datetime.fromtimestamp(wake_target).isoformat(timespec='seconds')}\n\n{summary}\n",
        encoding="utf-8",
    )

    elapsed = time.time() - started
    if tid:
        try:
            from brain.temporal_sense import resolve_estimate
            resolve_estimate(g, tid)
        except Exception:
            pass
    return {"path": str(out_path), "duration_s": round(elapsed, 2), "chars": len(summary)}


# ── LLM call helper ────────────────────────────────────────────────────


def _llm_summarize(g: dict[str, Any], prompt: str, max_seconds: float = 120.0) -> str:
    """Call the foreground LLM (or background if available) to summarize.
    Falls back to a deterministic stub if Ollama isn't reachable."""
    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import HumanMessage, SystemMessage
        # Use the foreground model (already warm).
        from brain.dual_brain import DualBrain
        model = DualBrain.FOREGROUND_MODEL_PREFERRED
        llm = ChatOllama(model=model, temperature=0.5, keep_alive=-1)
        sys_prompt = "You are Ava. Write in first person. Be concise. No preamble."
        msgs = [SystemMessage(content=sys_prompt), HumanMessage(content=prompt)]
        resp = llm.invoke(msgs)
        return str(getattr(resp, "content", "") or "").strip()
    except Exception as e:
        print(f"[sleep_mode] LLM summarize fallback ({e!r})")
        return "(LLM unavailable; sleep handoff stub. Awake session ended; sleep cycle will continue.)"


def _recent_chat_lines(g: dict[str, Any], limit: int = 200) -> list[str]:
    p = Path(g.get("BASE_DIR") or ".") / "state" / "chat_history.jsonl"
    if not p.is_file():
        return []
    lines: list[str] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                role = row.get("role") or row.get("speaker") or "?"
                text = row.get("text") or row.get("content") or ""
                if text:
                    lines.append(f"{role}: {str(text)[:300]}")
            except Exception:
                continue
    except Exception:
        pass
    return lines


def _recent_lessons(g: dict[str, Any], limit: int = 20) -> list[str]:
    p = Path(g.get("BASE_DIR") or ".") / "state" / "learning" / "lessons.jsonl"
    if not p.is_file():
        return []
    out: list[str] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                lesson = row.get("lesson") or ""
                source = row.get("source") or ""
                if lesson:
                    out.append(f"- {lesson} (from {source})")
            except Exception:
                continue
    except Exception:
        pass
    return out


# ── Main tick (called from heartbeat) ───────────────────────────────────


_TICK_LOCK = threading.Lock()


def tick(g: dict[str, Any]) -> dict[str, Any]:
    """Called periodically (every heartbeat) to advance the sleep state machine.
    Idempotent — safe to call from multiple paths."""
    with _TICK_LOCK:
        return _tick_impl(g)


def _tick_impl(g: dict[str, Any]) -> dict[str, Any]:
    state = get_state(g)

    # Honor pending requests first.
    pending = g.pop("_sleep_pending_request", None) if state == STATE_AWAKE else None
    wake_req = g.pop("_sleep_wake_request", None) if state in (STATE_SLEEPING, STATE_ENTERING_SLEEP) else None

    if state == STATE_AWAKE:
        if pending:
            duration = float(pending.get("duration_s") or _cfg("default_duration_seconds", default=21600.0))  # 6h default
            _enter_sleep(g, duration_s=duration, trigger=str(pending.get("trigger") or "voice"),
                         trigger_summary=dict(pending.get("summary") or {}))
            return {"transition": "AWAKE → ENTERING_SLEEP", "trigger": pending.get("trigger")}
        # Check triggers (only fire one per tick, first match wins).
        # Voice command takes priority.
        voice_ok, voice_payload = _check_voice_command_trigger(g)
        if voice_ok:
            duration = voice_payload.get("duration_s") or _cfg("default_duration_seconds", default=21600.0)
            g.pop("_sleep_voice_request", None)
            _enter_sleep(g, duration_s=duration, trigger="voice", trigger_summary=voice_payload)
            return {"transition": "AWAKE → ENTERING_SLEEP", "trigger": "voice"}

        full_ok, full_summary = _check_session_fullness_trigger(g)
        if full_ok:
            duration = float(_cfg("session_fullness_duration_seconds", default=1800.0))  # 30min default
            _enter_sleep(g, duration_s=duration, trigger="session_fullness", trigger_summary=full_summary)
            return {"transition": "AWAKE → ENTERING_SLEEP", "trigger": "session_fullness", "fullness": full_summary}

        sched_ok, sched_summary = _check_schedule_trigger(g)
        if sched_ok:
            # Compute duration to schedule end.
            duration = _seconds_until_schedule_end()
            _enter_sleep(g, duration_s=duration, trigger="schedule", trigger_summary=sched_summary)
            return {"transition": "AWAKE → ENTERING_SLEEP", "trigger": "schedule"}

        return {"state": STATE_AWAKE}

    if state == STATE_ENTERING_SLEEP:
        # Run Phase 1 in a background thread so the heartbeat tick doesn't
        # block for the LLM call (which can be 30-120s on this hardware).
        # First tick launches; subsequent ticks check completion.
        if g.get("_sleep_phase1_thread") is None:
            def _do_phase1():
                try:
                    result = _run_phase1_awake_handoff(g)
                    g["_sleep_phase1_result"] = result
                except Exception as _ex:
                    g["_sleep_phase1_result"] = {"error": repr(_ex)}
                finally:
                    g["_sleep_phase1_done"] = True
            t = threading.Thread(target=_do_phase1, daemon=True, name="ava-sleep-phase1")
            t.start()
            g["_sleep_phase1_thread"] = t
            g["_sleep_phase1_done"] = False
            return {"transition": "ENTERING_SLEEP → phase1_started"}
        if not g.get("_sleep_phase1_done"):
            return {"state": STATE_ENTERING_SLEEP, "phase": "awake_handoff_in_progress"}
        # Phase 1 complete — transition to SLEEPING.
        g["_sleep_state"] = STATE_SLEEPING
        g["_sleep_phase"] = "learning"
        g["_sleep_phase_started_ts"] = time.time()
        # Clear thread refs so next sleep cycle starts clean.
        g.pop("_sleep_phase1_thread", None)
        g.pop("_sleep_phase1_done", None)
        return {"transition": "ENTERING_SLEEP → SLEEPING", "phase1": g.get("_sleep_phase1_result")}

    if state == STATE_SLEEPING:
        # Honor wake requests.
        if wake_req:
            _enter_waking(g, reason=str(wake_req.get("reason") or "external"))
            return {"transition": "SLEEPING → WAKING", "reason": "external"}

        # Check wake target (on-time wake discipline).
        wake_target = float(g.get("_sleep_target_ts") or 0.0)
        wind_down = _wind_down_duration(g)
        now = time.time()
        if now >= wake_target:
            _enter_waking(g, reason="timer_expired")
            return {"transition": "SLEEPING → WAKING", "reason": "timer_expired"}

        # If we're inside the wind-down window AND still in learning phase,
        # transition to phase3 sleep handoff.
        if g.get("_sleep_phase") == "learning" and (wake_target - now) <= wind_down:
            print(f"[sleep_mode] wind-down — transitioning to phase3 ({wake_target - now:.0f}s before wake)")
            g["_sleep_phase"] = "sleep_handoff"
            g["_sleep_phase_started_ts"] = now
            return {"phase_transition": "learning → sleep_handoff"}

        # Run phase work.
        if g.get("_sleep_phase") == "learning":
            # One pass of learning per tick (curriculum.consolidation_hook), bounded.
            phase_budget = float(_cfg("phase2", "tick_budget_seconds", default=30.0))
            r = _run_phase2_learning(g, time_budget_seconds=phase_budget)
            return {"state": STATE_SLEEPING, "phase": "learning", "result": r}

        if g.get("_sleep_phase") == "sleep_handoff":
            # Same threading pattern as Phase 1 — don't block the heartbeat tick.
            if g.get("_sleep_phase3_thread") is None:
                def _do_phase3():
                    try:
                        result = _run_phase3_sleep_handoff(g)
                        g["_sleep_phase3_result"] = result
                    except Exception as _ex:
                        g["_sleep_phase3_result"] = {"error": repr(_ex)}
                    finally:
                        g["_sleep_phase3_done"] = True
                t = threading.Thread(target=_do_phase3, daemon=True, name="ava-sleep-phase3")
                t.start()
                g["_sleep_phase3_thread"] = t
                g["_sleep_phase3_done"] = False
                return {"phase3_started": True}
            if not g.get("_sleep_phase3_done"):
                return {"state": STATE_SLEEPING, "phase": "sleep_handoff_in_progress"}
            # Phase 3 complete — transition to WAKING.
            g.pop("_sleep_phase3_thread", None)
            g.pop("_sleep_phase3_done", None)
            _enter_waking(g, reason="phase3_complete")
            return {"transition": "SLEEPING → WAKING", "phase3": g.get("_sleep_phase3_result")}

        return {"state": STATE_SLEEPING, "phase": g.get("_sleep_phase")}

    if state == STATE_WAKING:
        # Check for over-run; self-interrupt if needed.
        wake_started = float(g.get("_sleep_wake_started_ts") or time.time())
        estimate = float(g.get("_sleep_wake_estimate_s") or 5.0)
        elapsed = time.time() - wake_started
        if elapsed > estimate * 1.5:
            # Over-run: self-interrupt notification, then complete transition.
            try:
                from brain.temporal_sense import _enqueue_self_interrupt
                _enqueue_self_interrupt(g, f"I need a little more time, about {int(estimate)} more seconds.")
            except Exception:
                pass
            # Bump the budget once more, then force AWAKE on next tick.
            g["_sleep_wake_estimate_s"] = elapsed + 5.0
            return {"state": STATE_WAKING, "overrun": True}
        if elapsed >= estimate:
            _enter_awake(g)
            return {"transition": "WAKING → AWAKE", "elapsed_s": round(elapsed, 2)}
        return {"state": STATE_WAKING, "elapsed_s": round(elapsed, 2), "estimate_s": estimate}

    return {"state": state}


def _wind_down_duration(g: dict[str, Any]) -> float:
    """How many seconds we need to reserve for Phase 3 + wake transition.
    Uses historical median from temporal_sense if available."""
    default = float(_cfg("wake", "wind_down_seconds_default", default=300.0))
    try:
        from brain.temporal_sense import calibrate_from_history
        cal = calibrate_from_history(g, "sleep_phase3")
        rec = cal.get("recommendation")
        if rec is not None:
            # Add 30s margin for the wake transition itself.
            return max(60.0, float(rec) + 30.0)
    except Exception:
        pass
    return default


def _seconds_until_schedule_end() -> float:
    """Compute seconds until the configured end_hour."""
    end_h = int(_cfg("schedule", "end_hour", default=5))
    now = datetime.now()
    end_dt = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
    if end_dt <= now:
        # Wrap to tomorrow
        from datetime import timedelta
        end_dt = end_dt + timedelta(days=1)
    return (end_dt - now).total_seconds()


# ── Snapshot for UI / debug ─────────────────────────────────────────────


def get_snapshot(g: dict[str, Any]) -> dict[str, Any]:
    """Return a compact summary suitable for /api/v1/debug/full and OrbCanvas."""
    state = get_state(g)
    return {
        "state": state,
        "phase": g.get("_sleep_phase"),
        "started_ts": g.get("_sleep_started_ts"),
        "target_ts": g.get("_sleep_target_ts"),
        "remaining_seconds": get_remaining_seconds(g),
        "progress": get_progress(g),
        "trigger": g.get("_sleep_trigger"),
        "wake_estimate_s": g.get("_sleep_wake_estimate_s"),
        "wake_started_ts": g.get("_sleep_wake_started_ts"),
        "decay_multiplier": get_emotion_decay_multiplier(g),
    }
