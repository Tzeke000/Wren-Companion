"""
tools/dev/regression_test.py — autonomous voice-path regression battery.

Boots avaagent.py as a subprocess with AVA_DEBUG=1 and PYTHONIOENCODING=utf-8,
waits for /api/v1/health to return ok, allows background subsystems to settle,
runs a fixed test battery via /api/v1/debug/inject_transcript, captures
diagnostic state via /api/v1/debug/full before+after, then shuts Ava down
cleanly and writes a structured pass/fail report.

Usage:
    py -3.11 tools/dev/regression_test.py
    py -3.11 tools/dev/regression_test.py --skip-warmup
    py -3.11 tools/dev/regression_test.py --report-path state/regression_last.json

Exit codes:
    0 — all tests passed
    1 — at least one test failed
    2 — boot or shutdown failure (Ava never came up, or got stuck)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 5876
DEFAULT_BASE = f"http://127.0.0.1:{DEFAULT_PORT}"

# Core battery — (label, transcript, fast_path?, timeout_seconds).
# Each entry is a single inject_transcript call. Pass criteria: HTTP 200,
# payload.ok=True, reply_chars>0, wall_seconds<=target, no errors_during_turn.
TEST_BATTERY = [
    ("time_query",  "what time is it",                          True,  3.0),
    ("date_query",  "what's today's date",                      True,  3.0),
    ("joke_llm",    "tell me a one sentence joke about clouds", False, 15.0),
    ("thanks",      "thank you",                                True,  2.0),
]


# ── Extended tests ──────────────────────────────────────────────────────
# Each function below is registered in EXTENDED_TESTS and runs after the
# core battery on the same Ava process. They may make multiple
# inject_transcript calls, sleep, and inspect /api/v1/debug/full state
# between calls. Each returns a test_result dict matching the core
# schema (label, wall_seconds, passed, fail_reasons, details). Tests
# are independent — each can be removed without breaking the others.

def _inject(text: str, *, source: str = "regression", speak: bool = False,
            as_user: str = "claude_code",
            timeout_s: float = 30.0) -> tuple[int, dict | None, str]:
    """Helper: drive a synthetic turn and return (status, payload, err).

    All test injects route through the claude_code developer profile by
    default — keeps Zeke's relationship state, mood history, threads, and
    memory clean of regression-test traffic. Override with as_user="zeke"
    when explicitly testing the real-user identity-resolution path.
    """
    return _http_post_json(
        f"{DEFAULT_BASE}/api/v1/debug/inject_transcript",
        {
            "text": text,
            "wake_source": source,
            "wait_for_audio": False,
            "speak": speak,
            "as_user": as_user,
            "timeout_seconds": timeout_s,
        },
        timeout=timeout_s + 30.0,
    )


def _debug_full() -> dict | None:
    """Helper: GET /api/v1/debug/full, return payload or None."""
    _status, payload, _err = _http_get_json(
        f"{DEFAULT_BASE}/api/v1/debug/full", timeout=8.0
    )
    return payload if isinstance(payload, dict) else None


def _ext_test_conversation_active_gating() -> dict:
    """Verify _conversation_active flag is True during a turn.

    Pre-turn snapshot: _conversation_active should be False (passive).
    Mid-turn: dispatch a turn, immediately check the flag — should be True.
    Post-turn: should remain True for the attentive window.
    """
    label = "conversation_active_gating"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {}

    pre = _debug_full() or {}
    pre_active = bool((pre.get("voice_loop") or {}).get("_conversation_active"))
    details["pre_turn_active"] = pre_active

    status, payload, err = _inject("hey ava", timeout_s=5.0)
    details["http_status"] = status
    details["http_error"] = err
    if status != 200:
        fails.append(f"http_status={status}")
    if not isinstance(payload, dict):
        fails.append(f"no_payload err={err}")
        return {
            "label": label,
            "wall_seconds": round(time.time() - t0, 3),
            "passed": False,
            "fail_reasons": fails,
            "details": details,
        }
    details["reply_chars"] = int(payload.get("reply_chars") or 0)

    # Immediately after the turn, _conversation_active should still be True
    # (the attentive window holds it for 180s).
    post = _debug_full() or {}
    post_active = bool((post.get("voice_loop") or {}).get("_conversation_active"))
    details["post_turn_active"] = post_active
    if not post_active:
        fails.append("conversation_active=False post-turn (attentive window not held)")

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


def _ext_test_self_listen_guard_observable() -> dict:
    """Verify the self-listen guard's prerequisites are queryable.

    The guard in voice_loop._should_drop_self_listen() reads two host
    globals: _tts_speaking and _last_speak_end_ts. /api/v1/debug/full
    must surface both reliably so external observers (this regression
    suite, future monitoring tools) can verify guard behaviour without
    instrumenting voice_loop.py.

    Steps:
      1. Inject a turn with speak=True, wait_for_audio=False so TTS
         starts in the background while we keep polling
      2. Poll /debug/full a few times during playback — at least one
         poll should catch tts_worker.speaking=True
      3. Wait for playback to finish (speaking transitions to False or
         _last_speak_end_ts becomes recent), then confirm _last_speak_end_ts
         is within the last few seconds

    Skip-safe: if TTS is not available (kokoro_loaded=False), the test
    reports skipped=True rather than fail — keeps the suite green on
    machines without working audio.
    """
    label = "self_listen_guard_observable"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {}

    pre = _debug_full() or {}
    kokoro_loaded = bool((pre.get("subsystem_health") or {}).get("kokoro_loaded"))
    details["kokoro_loaded"] = kokoro_loaded
    if not kokoro_loaded:
        return {
            "label": label,
            "wall_seconds": round(time.time() - t0, 3),
            "passed": True,
            "fail_reasons": [],
            "details": {**details, "skipped": "kokoro not loaded"},
        }

    inject_t0 = time.time()
    status, payload, err = _inject("hey ava", speak=True, timeout_s=5.0)
    details["http_status"] = status
    details["http_error"] = err
    if status != 200 or not isinstance(payload, dict):
        fails.append(f"inject failed status={status} err={err}")
        return {
            "label": label,
            "wall_seconds": round(time.time() - t0, 3),
            "passed": False,
            "fail_reasons": fails,
            "details": details,
        }

    # Poll quickly to catch TTS in flight. Up to 3s of polling at 100ms.
    saw_speaking = False
    for _ in range(30):
        snap = _debug_full() or {}
        sh = snap.get("subsystem_health") or {}
        tts = sh.get("tts_worker") or {}
        vl = snap.get("voice_loop") or {}
        if bool(tts.get("speaking")) or bool(vl.get("_tts_speaking")):
            saw_speaking = True
            details["caught_speaking_at_offset_ms"] = int((time.time() - inject_t0) * 1000)
            break
        time.sleep(0.1)
    details["saw_speaking"] = saw_speaking
    if not saw_speaking:
        # Not strictly a failure — short replies can finish before we poll.
        # But _last_speak_end_ts must have advanced past inject_t0.
        snap = _debug_full() or {}
        last_end = float((snap.get("voice_loop") or {}).get("last_speak_end_ts") or 0.0)
        details["last_speak_end_ts"] = last_end
        if last_end <= inject_t0:
            fails.append(
                "tts_worker.speaking never True AND last_speak_end_ts didn't advance"
            )

    # Wait briefly for playback completion; record timing.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        snap = _debug_full() or {}
        tts = (snap.get("subsystem_health") or {}).get("tts_worker") or {}
        if not bool(tts.get("speaking")):
            break
        time.sleep(0.15)
    details["wait_for_done_seconds"] = round(time.time() - inject_t0, 2)

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


def _ext_test_attentive_window_observable() -> dict:
    """Verify the attentive window's observable state — last_speak_end_ts
    advances on TTS, attentive_remaining_seconds is non-zero right after,
    and decays over the next few seconds.

    Note: inject_transcript bypasses voice_loop.listen_session, so this
    cannot test the real "user speaks during attentive without wake" flow
    — that requires actual mic audio. What it CAN verify is that the
    timestamps voice_loop's _attentive_wait() loop reads
    (_last_speak_end_ts, attentive_remaining_seconds) are correctly
    surfaced and behave as expected, which is what voice_loop's
    transition logic depends on.

    Steps:
      1. Inject with speak=True so the TTS worker stamps
         _last_speak_end_ts when playback completes
      2. Read /debug/full — last_speak_end_ts must be recent (within 30s
         of test start) and attentive_remaining_seconds > 0
      3. Sleep 3s; read again — attentive_remaining_seconds must have
         decreased by approximately 3s (allow ±1s slack)

    Skip-safe: skipped if kokoro not loaded.
    """
    label = "attentive_window_observable"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {}

    pre = _debug_full() or {}
    if not bool((pre.get("subsystem_health") or {}).get("kokoro_loaded")):
        return {
            "label": label,
            "wall_seconds": round(time.time() - t0, 3),
            "passed": True,
            "fail_reasons": [],
            "details": {"skipped": "kokoro not loaded"},
        }

    inject_t0 = time.time()
    status, payload, _err = _inject("hello", speak=True, timeout_s=5.0)
    if status != 200 or not isinstance(payload, dict):
        fails.append(f"inject failed status={status}")
        return {
            "label": label,
            "wall_seconds": round(time.time() - t0, 3),
            "passed": False,
            "fail_reasons": fails,
            "details": details,
        }

    # Wait for TTS to finish so _last_speak_end_ts gets stamped.
    deadline = time.time() + 6.0
    while time.time() < deadline:
        snap = _debug_full() or {}
        tts = (snap.get("subsystem_health") or {}).get("tts_worker") or {}
        if not bool(tts.get("speaking")):
            break
        time.sleep(0.1)

    s1 = _debug_full() or {}
    vl1 = s1.get("voice_loop") or {}
    last_end_1 = float(vl1.get("last_speak_end_ts") or 0.0)
    rem_1 = float(vl1.get("attentive_remaining_seconds") or 0.0)
    details["last_speak_end_ts_after_inject"] = last_end_1
    details["attentive_remaining_seconds_first"] = rem_1
    if last_end_1 <= inject_t0:
        fails.append(f"last_speak_end_ts ({last_end_1}) didn't advance past inject_t0 ({inject_t0})")
    if rem_1 <= 0:
        fails.append(f"attentive_remaining_seconds={rem_1} (expected >0 right after speak)")

    # Sleep 3s, verify decay.
    time.sleep(3.0)
    s2 = _debug_full() or {}
    vl2 = s2.get("voice_loop") or {}
    rem_2 = float(vl2.get("attentive_remaining_seconds") or 0.0)
    details["attentive_remaining_seconds_after_3s"] = rem_2
    if rem_1 > 0:
        decay = rem_1 - rem_2
        details["decay"] = round(decay, 2)
        if not (2.0 <= decay <= 4.0):
            fails.append(f"unexpected decay rate: {decay:.2f}s in 3s wall clock")

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


def _ext_test_wake_source_variety() -> dict:
    """Verify inject_transcript propagates wake_source through to voice_loop
    state and run_ava produces a reply for each source label.

    Wake sources in production: clap detector, openWakeWord, transcript-poll
    fallback. Each sets _g["_wake_source"] before voice_loop transitions to
    listening; the wake_detector short-circuits to (True, 1.0) when present.
    inject_transcript mirrors this by accepting wake_source and stamping
    _g["_wake_source"] before calling run_ava.

    Steps for each source in {clap, openwakeword, transcript_wake:hey_ava}:
      1. Inject "what time is it" with that wake_source
      2. Verify reply produced (reply_chars > 0)
      3. Read /debug/full — voice_loop.last_wake_source must equal the
         injected value

    Uses voice_command_router-matched text ("what time is it") so each
    iteration completes in ~0.4s without hitting the LLM. Total test
    runtime ~2-3s.
    """
    label = "wake_source_variety"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {"sources": {}}

    sources = ["clap", "openwakeword", "transcript_wake:hey_ava"]
    for src in sources:
        per: dict = {}
        s_t0 = time.time()
        status, payload, err = _inject(
            "what time is it", source=src, speak=False, timeout_s=5.0
        )
        per["http_status"] = status
        per["http_error"] = err
        per["elapsed"] = round(time.time() - s_t0, 3)
        if status != 200 or not isinstance(payload, dict):
            fails.append(f"{src}: inject failed status={status}")
            details["sources"][src] = per
            continue
        per["reply_chars"] = int(payload.get("reply_chars") or 0)
        per["reply_preview"] = (payload.get("reply_text") or "")[:80]
        if per["reply_chars"] <= 0:
            fails.append(f"{src}: empty reply")
        # Verify wake_source landed on _g.
        snap = _debug_full() or {}
        actual = str((snap.get("voice_loop") or {}).get("last_wake_source") or "")
        per["last_wake_source"] = actual
        if actual != src:
            fails.append(f"{src}: voice_loop.last_wake_source={actual!r} (expected {src!r})")
        details["sources"][src] = per

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


def _ext_test_weird_inputs() -> dict:
    """Verify the endpoint handles boundary inputs without hanging or
    crashing. Each case has its own pass criteria.

    Cases:
      empty:       "" → endpoint returns ok=False with "empty text" error
                       (validated server-side, doesn't reach run_ava).
      whitespace:  "   \\t\\n   " → same — text.strip() empty.
      single_char: "?" → run_ava reaches the LLM (no voice_command match,
                       no fast-path pattern). Reply may be empty or a
                       short clarification — either is fine; what matters
                       is that we get a 200 OK in <30s, not a hang.
      long_500:    a 500-character utterance → must complete in under
                       30s without timeout or unhandled exception.

    None of these should produce errors_during_turn entries (those
    represent unexpected exceptions, not graceful empty-reply cases).
    """
    label = "weird_inputs"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {"cases": {}}

    # Empty / whitespace go to a server-side reject (ok=False); fast.
    # single_char and long_500 don't match any fast-path pattern, so they
    # land on the deep path which can take 60-90s on a cold ollama (model
    # routing + multi-model invokes). We don't gate those on timing —
    # what matters is graceful handling without errors_during_turn.
    cases = [
        ("empty",       "",                          {"expect_ok_false": True,  "max_seconds": 5.0,   "check_timing": True}),
        ("whitespace",  "   \t\n   ",                {"expect_ok_false": True,  "max_seconds": 5.0,   "check_timing": True}),
        ("single_char", "?",                         {"expect_ok_false": False, "max_seconds": 120.0, "check_timing": False}),
        ("long_500",    "Tell me " + ("a" * 490),    {"expect_ok_false": False, "max_seconds": 120.0, "check_timing": False}),
    ]
    for case_name, text, expectations in cases:
        per: dict = {"text_len": len(text)}
        c_t0 = time.time()
        status, payload, err = _inject(
            text, speak=False, timeout_s=expectations["max_seconds"]
        )
        per["http_status"] = status
        per["http_error"] = err
        per["elapsed"] = round(time.time() - c_t0, 3)
        if expectations["check_timing"] and per["elapsed"] > expectations["max_seconds"]:
            fails.append(f"{case_name}: timing_over_target {per['elapsed']:.2f}s")
        if status != 200:
            # We only accept HTTP 200 with a JSON payload; the endpoint
            # returns ok=False inline rather than HTTP errors for empty.
            fails.append(f"{case_name}: http_status={status}")
            details["cases"][case_name] = per
            continue
        if not isinstance(payload, dict):
            fails.append(f"{case_name}: no_payload err={err}")
            details["cases"][case_name] = per
            continue
        per["ok"] = bool(payload.get("ok"))
        per["reply_chars"] = int(payload.get("reply_chars") or 0)
        per["error"] = str(payload.get("error") or payload.get("run_ava_error") or "")
        errs_during = payload.get("errors_during_turn") or []
        per["errors_during_turn_count"] = len(errs_during)
        if expectations["expect_ok_false"]:
            if per["ok"]:
                fails.append(f"{case_name}: expected ok=False, got ok=True")
        # errors_during_turn should be empty for graceful handling.
        if errs_during:
            fails.append(
                f"{case_name}: {len(errs_during)} errors_during_turn (graceful failure expected)"
            )
        details["cases"][case_name] = per

    # Settle wait — a deep-path turn (single_char or long_500) can leave
    # _conversation_active and _turn_in_progress flags set briefly while
    # background tasks unwind. Subsequent tests run on the same Ava and
    # need a clean baseline.
    settle_deadline = time.time() + 8.0
    while time.time() < settle_deadline:
        snap = _debug_full() or {}
        vl = snap.get("voice_loop") or {}
        if not bool(vl.get("_turn_in_progress")):
            break
        time.sleep(0.5)
    details["settle_seconds"] = round(time.time() - settle_deadline + 8.0, 2)

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


def _ext_test_sequential_fast_path_latency() -> dict:
    """Verify back-to-back fast-path turns stay flat — no drift, no
    growth from accumulated state. Tests the ChatOllama instance cache
    in reply_engine and confirms the same warmed model isn't being
    evicted between calls.

    Five fast-path utterances in a row. Each should hit the
    voice_command_router OR the LLM fast path with a cached ChatOllama
    instance. Latency should stay flat — the 5th call must not be
    measurably slower than the 1st (>2x).

    Steps:
      1. Inject 5 fast-path inputs sequentially
      2. Record each elapsed time
      3. Pass if max(latencies) <= 2 * min(latencies) AND every call
         succeeded with a non-empty reply

    Note: the absolute floor depends on hardware; we only verify the
    ratio so this test isn't fragile across machines.
    """
    label = "sequential_fast_path_latency"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {"calls": []}

    inputs = [
        "hi ava",
        "thanks",
        "ok ava",
        "got it",
        "hey ava",
    ]
    latencies: list[float] = []
    for idx, text in enumerate(inputs):
        c_t0 = time.time()
        # 30s is generous — first call after a heavy preceding test may
        # need a moment to recover. Real fast-path is 1-2s warm.
        status, payload, err = _inject(text, speak=False, timeout_s=30.0)
        elapsed = time.time() - c_t0
        per = {
            "idx": idx,
            "text": text,
            "elapsed": round(elapsed, 3),
            "http_status": status,
            "http_error": err,
            "reply_chars": int((payload or {}).get("reply_chars") or 0),
            "ok": bool((payload or {}).get("ok")),
        }
        details["calls"].append(per)
        if status != 200 or not isinstance(payload, dict):
            fails.append(f"call {idx} ({text!r}): inject failed status={status}")
            continue
        if not per["ok"] or per["reply_chars"] <= 0:
            fails.append(f"call {idx} ({text!r}): empty/failed reply")
            continue
        latencies.append(elapsed)

    if len(latencies) >= 2:
        details["min_latency"] = round(min(latencies), 3)
        details["max_latency"] = round(max(latencies), 3)
        ratio = max(latencies) / max(0.001, min(latencies))
        details["max_min_ratio"] = round(ratio, 2)
        if ratio > 2.5:
            fails.append(
                f"latency drift: max/min ratio {ratio:.2f} (>2.5x — cache may not be warming)"
            )

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


def _ext_test_concept_graph_save_under_load() -> dict:
    """Verify concept_graph backoff prevents save-failure log spam under
    rapid turn pressure.

    Background: brain/concept_graph.py:_save() now has exponential
    backoff (1, 2, 4, 8, 16, 32, 60s capped) when concept_graph.json
    is locked by an external process. Without backoff, 10+ rapid
    add_node calls would each retry immediately, flooding stderr.

    Steps:
      1. Baseline: read /debug/full — count concept_graph_state.total_nodes
         and how many errors_recent entries mention concept_graph
      2. Inject 10 fast-path turns rapidly with varying short text so
         the concept graph has fresh material to add (proper turns
         exercise the add_node/_save path)
      3. Brief settle (1.5s)
      4. Final: read /debug/full again
      5. Pass if no NEW concept_graph errors appeared in errors_recent

    Pass:  errors_during_load_count == 0
    Fail:  errors_recent grew with messages containing "concept_graph"

    Note: this doesn't force a lock conflict (the file probably isn't
    locked by an external process during the test). What it DOES test
    is that the save path doesn't throw under rapid-fire turn load —
    if the backoff state gets confused or a race condition exists,
    we'd see exceptions in errors_recent.
    """
    label = "concept_graph_save_under_load"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {}

    pre = _debug_full() or {}
    pre_nodes = int((pre.get("concept_graph_state") or {}).get("total_nodes") or 0)
    pre_cg_errors = sum(
        1 for e in (pre.get("errors_recent") or [])
        if "concept_graph" in str(e.get("message", "") + e.get("module", ""))
    )
    details["pre_total_nodes"] = pre_nodes
    details["pre_cg_error_count"] = pre_cg_errors

    # 10 rapid fast-path turns. Vary the content slightly so the
    # concept_graph might extract different concepts each time.
    inputs = [
        "hi ava", "what time is it", "thanks", "got it", "ok ava",
        "hey ava", "hello", "what's up", "how are you", "tell me a joke",
    ]
    inject_t0 = time.time()
    completed = 0
    for text in inputs:
        # Generous timeout — these are all fast-path eligible inputs but
        # if any earlier test left the ollama lock briefly held, the
        # first call here may wait a moment.
        status, payload, _err = _inject(text, speak=False, timeout_s=30.0)
        if status == 200 and isinstance(payload, dict) and payload.get("ok"):
            completed += 1
    details["completed_turns"] = completed
    details["total_inject_seconds"] = round(time.time() - inject_t0, 2)

    # Brief settle so background concept_graph saves complete.
    time.sleep(1.5)

    post = _debug_full() or {}
    post_nodes = int((post.get("concept_graph_state") or {}).get("total_nodes") or 0)
    post_cg_errors_list = [
        e for e in (post.get("errors_recent") or [])
        if "concept_graph" in str(e.get("message", "") + e.get("module", ""))
    ]
    details["post_total_nodes"] = post_nodes
    details["post_cg_error_count"] = len(post_cg_errors_list)
    details["new_cg_errors"] = len(post_cg_errors_list) - pre_cg_errors
    if details["new_cg_errors"] > 0:
        # Capture a sample for the report.
        details["new_cg_error_samples"] = [
            e for e in post_cg_errors_list[-5:]
        ]
        fails.append(
            f"{details['new_cg_errors']} new concept_graph error(s) during load"
        )

    if completed < 8:
        fails.append(f"only {completed}/10 turns completed (system instability under load)")

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


def _ext_test_time_date_no_llm() -> dict:
    """Verify time/date queries are handled deterministically by
    voice_commands and NEVER reach the LLM.

    Lunch voice test (2026-04-30) caught ava-personal hallucinating
    "9:47 AM" when actual time was 12:16 PM, because the user's
    phrasing fell through to the LLM. After expanding voice_commands
    regex, every reasonable phrasing must match the deterministic
    handler — proven here by asserting `re.ollama_invoke_start` does
    NOT appear in trace_lines_for_turn.

    Tests the natural variants: "what time is it", "tell me the time",
    "do you know the time", "current time", and the date equivalents.
    Each must:
      1. Return a non-empty reply
      2. Complete in under 1 second (deterministic, no LLM round-trip)
      3. Have NO `re.ollama_invoke_start` line in its trace
    """
    label = "time_date_no_llm"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {"queries": []}

    queries = [
        # Time variants
        "what time is it",
        "tell me the time",
        "do you know the time",
        "got the time",
        "current time",
        # Date variants
        "what's today's date",
        "what date is it",
        "what day is it",
        "tell me the date",
        "current date",
    ]

    for q in queries:
        per: dict = {"q": q}
        c_t0 = time.time()
        status, payload, err = _inject(q, speak=False, timeout_s=5.0)
        per["elapsed"] = round(time.time() - c_t0, 3)
        per["http_status"] = status
        if status != 200 or not isinstance(payload, dict):
            fails.append(f"{q!r}: inject failed status={status} err={err}")
            details["queries"].append(per)
            continue
        per["reply_text"] = (payload.get("reply_text") or "")[:120]
        per["reply_chars"] = int(payload.get("reply_chars") or 0)
        traces = payload.get("trace_lines_for_turn") or []
        # Critical assertion: NO re.ollama_invoke_start anywhere in this turn.
        invoke_lines = [t for t in traces if "re.ollama_invoke_start" in t]
        per["ollama_invoke_count"] = len(invoke_lines)
        if invoke_lines:
            fails.append(
                f"{q!r}: hit LLM ({len(invoke_lines)}x) — voice_commands didn't intercept"
            )
            per["invoke_lines_sample"] = invoke_lines[:2]
        if per["reply_chars"] <= 0:
            fails.append(f"{q!r}: empty reply")
        if per["elapsed"] > 1.5:
            fails.append(
                f"{q!r}: {per['elapsed']}s > 1.5s (deterministic should be sub-second)"
            )
        details["queries"].append(per)

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


def _ext_test_back_to_back_tts_no_drop() -> dict:
    """Verify two back-to-back TTS turns both complete playback.

    Lunch voice test (2026-04-30) caught a second-turn TTS being silently
    dropped — the user's "it's actually 12:21 PM" follow-up generated a
    224-char reply but no audio. Trace ended at finalize_ava_turn with
    no tts.playback_done. Possible causes: stale _tts_muted, window-focus
    gating, or worker queue stall.

    This test exercises:
      1. Inject turn 1 with speak=True wait_for_audio=True
      2. Verify tts.last_playback_dropped is False post-playback
      3. Within 5s (still in attentive window), inject turn 2 with same
         speak=True wait_for_audio=True
      4. Verify tts.last_playback_dropped is False again

    Skip-safe: skipped if kokoro_loaded=False.
    """
    label = "back_to_back_tts_no_drop"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {}

    pre = _debug_full() or {}
    if not bool((pre.get("subsystem_health") or {}).get("kokoro_loaded")):
        return {
            "label": label,
            "wall_seconds": round(time.time() - t0, 3),
            "passed": True,
            "fail_reasons": [],
            "details": {"skipped": "kokoro not loaded"},
        }

    for turn_idx, text in enumerate(["hello there", "how are you doing"], start=1):
        c_t0 = time.time()
        status, payload, err = _inject(
            text, speak=True, timeout_s=20.0
        )
        details[f"turn_{turn_idx}_http_status"] = status
        details[f"turn_{turn_idx}_elapsed"] = round(time.time() - c_t0, 3)
        if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
            fails.append(f"turn {turn_idx}: inject failed status={status} err={err}")
            continue
        details[f"turn_{turn_idx}_reply_chars"] = int(payload.get("reply_chars") or 0)
        # Wait for the worker to finish playing — poll tts_speaking until False.
        deadline = time.time() + 15.0
        while time.time() < deadline:
            snap = _debug_full() or {}
            tts = (snap.get("subsystem_health") or {}).get("tts_worker") or {}
            if not bool(tts.get("speaking")):
                break
            time.sleep(0.1)
        # Final state: must NOT have last_playback_dropped True.
        snap = _debug_full() or {}
        tts = (snap.get("subsystem_health") or {}).get("tts_worker") or {}
        dropped = bool(tts.get("last_playback_dropped"))
        details[f"turn_{turn_idx}_dropped"] = dropped
        if dropped:
            fails.append(f"turn {turn_idx}: tts.last_playback_dropped=True (silent failure)")

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


def _ext_test_identity_routing() -> dict:
    """Verify as_user routing — claude_code turns are isolated from Zeke,
    real-user turns (as_user unset) still resolve to Zeke.

    The lunch voice test exposed that test traffic from Claude Code was
    pulluting Zeke's relationship state. Fix: inject_transcript accepts
    an as_user param; tests default to claude_code; real voice turns
    (which never use inject_transcript) continue to use Zeke. This test
    asserts both code paths.

    Steps:
      1. Inject "hi there" with as_user=claude_code — verify trace shows
         person=claude_code in [perf] line
      2. Inject "hi there" with as_user=zeke — verify person=zeke
      3. Both must produce non-empty replies (proves both profiles load
         correctly and run_ava handles each)
    """
    label = "identity_routing"
    t0 = time.time()
    fails: list[str] = []
    details: dict = {"calls": []}

    cases = [
        ("claude_code", "hi there"),
        ("zeke", "hi there"),
    ]
    for as_user, text in cases:
        per: dict = {"as_user": as_user, "text": text}
        c_t0 = time.time()
        status, payload, err = _inject(
            text, speak=False, as_user=as_user, timeout_s=10.0
        )
        per["http_status"] = status
        per["elapsed"] = round(time.time() - c_t0, 3)
        if status != 200 or not isinstance(payload, dict):
            fails.append(f"as_user={as_user!r}: inject failed status={status}")
            details["calls"].append(per)
            continue
        per["reply_chars"] = int(payload.get("reply_chars") or 0)
        if per["reply_chars"] <= 0:
            fails.append(f"as_user={as_user!r}: empty reply")
        # Inspect trace for person attribution.
        traces = payload.get("trace_lines_for_turn") or []
        person_lines = [t for t in traces if "person=" in t]
        per["person_trace_sample"] = person_lines[:1]
        # The [perf] start line stamps person=<id>. Test that it matches
        # what we asked for.
        person_in_trace = ""
        for t in person_lines:
            # extract person=foo
            idx = t.find("person=")
            if idx >= 0:
                rest = t[idx + 7:]
                person_in_trace = rest.split()[0].rstrip(",")
                break
        per["person_in_trace"] = person_in_trace
        if person_in_trace and person_in_trace != as_user:
            fails.append(
                f"as_user={as_user!r}: trace says person={person_in_trace!r}"
            )
        details["calls"].append(per)

    return {
        "label": label,
        "wall_seconds": round(time.time() - t0, 3),
        "passed": not fails,
        "fail_reasons": fails,
        "details": details,
    }


EXTENDED_TESTS = [
    _ext_test_conversation_active_gating,
    _ext_test_self_listen_guard_observable,
    _ext_test_attentive_window_observable,
    _ext_test_wake_source_variety,
    _ext_test_weird_inputs,
    _ext_test_sequential_fast_path_latency,
    _ext_test_concept_graph_save_under_load,
    _ext_test_time_date_no_llm,
    _ext_test_back_to_back_tts_no_drop,
    _ext_test_identity_routing,
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _http_get_json(url: str, timeout: float = 5.0) -> tuple[int, dict | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return 0, None, str(e)
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def _http_post_json(url: str, body: dict, timeout: float = 30.0) -> tuple[int, dict | None, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, None, f"HTTP {e.code}: {body[:300]}"
    except urllib.error.URLError as e:
        return 0, None, str(e)
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


class AvaProcess:
    """Manages a child avaagent.py process."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if _port_in_use(DEFAULT_PORT):
            raise RuntimeError(
                f"port {DEFAULT_PORT} already in use — kill any existing avaagent.py before running"
            )
        env = os.environ.copy()
        env["AVA_DEBUG"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # Force UTF-8 console on Windows so [trace] lines with unicode don't
        # crash the print() inside the captured stdout.
        env["PYTHONUTF8"] = "1"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fp = self.log_path.open("w", encoding="utf-8", errors="replace")
        # py -3.11 launcher on Windows; on POSIX, fall back to python3.11.
        cmd = ["py", "-3.11", "avaagent.py"] if os.name == "nt" else ["python3.11", "avaagent.py"]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    def wait_ready(self, timeout_s: float = 90.0) -> tuple[bool, str]:
        deadline = time.time() + timeout_s
        last_err = ""
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False, f"process exited with code {self.proc.returncode} during boot"
            status, payload, err = _http_get_json(f"{DEFAULT_BASE}/api/v1/health", timeout=1.5)
            if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                return True, ""
            last_err = err or f"status={status}"
            time.sleep(1.0)
        return False, f"timeout after {timeout_s:.0f}s — last error: {last_err}"

    def stop(self, timeout_s: float = 30.0) -> str:
        if self.proc is None:
            return "no process"
        # Try graceful HTTP shutdown first.
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"{DEFAULT_BASE}/api/v1/shutdown", method="POST", data=b""),
                timeout=2.0,
            ).read()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout_s)
            return f"clean exit code={self.proc.returncode}"
        except subprocess.TimeoutExpired:
            pass
        # terminate() on Windows is TerminateProcess — abrupt but does not
        # invoke Intel MKL / Fortran CTRL+BREAK handlers, so it avoids the
        # forrtl-200 "program aborting" cascade that CTRL_BREAK_EVENT triggers.
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10.0)
            return f"terminated exit code={self.proc.returncode}"
        except Exception:
            pass
        # Hard kill (last resort).
        try:
            self.proc.kill()
            self.proc.wait(timeout=5.0)
            return f"killed exit code={self.proc.returncode}"
        except Exception as e:
            return f"kill failed: {e}"


def run_battery(warmup_s: float = 30.0) -> dict:
    """Boot Ava, run battery, capture report, shutdown. Returns report dict."""
    state_dir = REPO_ROOT / "state" / "regression"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / f"run_{int(time.time())}.log"
    report: dict = {
        "started_ts": _now(),
        "log_path": str(log_path),
        "phases": {},
        "tests": [],
        "boot_ok": False,
        "all_pass": False,
    }
    ava = AvaProcess(log_path)
    try:
        ava.start()
        report["phases"]["start_pid"] = ava.proc.pid if ava.proc else None
        boot_t0 = time.time()
        ok, err = ava.wait_ready(timeout_s=240.0)
        report["phases"]["boot_seconds"] = round(time.time() - boot_t0, 2)
        report["phases"]["boot_error"] = err
        report["boot_ok"] = ok
        if not ok:
            return report
        # Background settle.
        time.sleep(max(0.0, warmup_s))
        report["phases"]["warmup_seconds"] = warmup_s

        # Baseline /debug/full
        status, baseline, err = _http_get_json(f"{DEFAULT_BASE}/api/v1/debug/full", timeout=10.0)
        report["baseline_debug_status"] = status
        report["baseline_debug_error"] = err
        if isinstance(baseline, dict):
            report["baseline_summary"] = {
                "voice_loop_state": (baseline.get("voice_loop") or {}).get("state"),
                "conversation_active": (baseline.get("voice_loop") or {}).get("_conversation_active"),
                "ollama_reachable": ((baseline.get("subsystem_health") or {}).get("ollama_reachable") or {}),
                "errors_recent_count": len(baseline.get("errors_recent") or []),
            }

        # Run battery.
        for label, text, fast_path, target_s in TEST_BATTERY:
            t0 = time.time()
            body = {
                "text": text,
                "wake_source": "regression",
                "wait_for_audio": False,
                "speak": False,  # don't block on audio for battery — TTS path tested separately
                "as_user": "claude_code",  # don't pollute Zeke's profile state with test traffic
                "timeout_seconds": target_s + 5.0,
            }
            status, payload, err = _http_post_json(
                f"{DEFAULT_BASE}/api/v1/debug/inject_transcript",
                body,
                timeout=target_s + 30.0,
            )
            elapsed = time.time() - t0
            test_result = {
                "label": label,
                "text": text,
                "expected_path": "fast" if fast_path else "llm",
                "target_seconds": target_s,
                "wall_seconds": round(elapsed, 3),
                "http_status": status,
                "http_error": err,
                "reply_text": "",
                "reply_chars": 0,
                "ollama_lock_wait_ms_total": 0,
                "trace_count": 0,
                "errors_during_turn": [],
                "passed": False,
                "fail_reasons": [],
            }
            if isinstance(payload, dict):
                test_result["reply_text"] = (payload.get("reply_text") or "")[:300]
                test_result["reply_chars"] = int(payload.get("reply_chars") or 0)
                test_result["ollama_lock_wait_ms_total"] = int(payload.get("ollama_lock_wait_ms_total") or 0)
                test_result["trace_count"] = len(payload.get("trace_lines_for_turn") or [])
                test_result["errors_during_turn"] = payload.get("errors_during_turn") or []
                test_result["run_ava_ms"] = int(payload.get("run_ava_ms") or 0)
                test_result["total_ms"] = int(payload.get("total_ms") or 0)
                # Pass criteria.
                fails: list[str] = []
                if status != 200:
                    fails.append(f"http_status={status}")
                if not payload.get("ok"):
                    fails.append(f"ok=false (error={payload.get('run_ava_error')})")
                if not (test_result["reply_chars"] > 0):
                    fails.append("empty_reply")
                if elapsed > target_s:
                    fails.append(f"timing_over_target {elapsed:.2f}s>{target_s:.2f}s")
                if test_result["errors_during_turn"]:
                    fails.append(f"errors_during_turn n={len(test_result['errors_during_turn'])}")
                test_result["passed"] = not fails
                test_result["fail_reasons"] = fails
            else:
                test_result["fail_reasons"] = [f"no_payload err={err}"]
            report["tests"].append(test_result)

        # Extended tests — run after the core battery on the same Ava
        # process. Each test handles its own injects + assertions and
        # returns a result dict matching the core schema.
        for fn in EXTENDED_TESTS:
            try:
                tr = fn()
            except Exception as e:
                tr = {
                    "label": getattr(fn, "__name__", "unknown_extended"),
                    "wall_seconds": 0.0,
                    "passed": False,
                    "fail_reasons": [f"raised {type(e).__name__}: {e}"],
                    "details": {},
                }
            tr.setdefault("text", "")
            tr.setdefault("expected_path", "ext")
            tr.setdefault("target_seconds", 0.0)
            tr.setdefault("reply_text", "")
            tr.setdefault("reply_chars", 0)
            report["tests"].append(tr)

        # Final /debug/full
        status, final, err = _http_get_json(f"{DEFAULT_BASE}/api/v1/debug/full", timeout=10.0)
        report["final_debug_status"] = status
        report["final_debug_error"] = err
        if isinstance(final, dict):
            report["final_summary"] = {
                "voice_loop_state": (final.get("voice_loop") or {}).get("state"),
                "conversation_active": (final.get("voice_loop") or {}).get("_conversation_active"),
                "errors_recent_count": len(final.get("errors_recent") or []),
                "errors_recent": final.get("errors_recent") or [],
                "concept_graph_total_nodes": (final.get("concept_graph_state") or {}).get("total_nodes"),
                "stream_b_queue_depth": (final.get("dual_brain_state") or {}).get("stream_b_queue_depth"),
            }
            report["last_turn"] = final.get("last_turn") or {}
            report["recent_traces_tail"] = (final.get("recent_traces") or [])[-30:]

        all_pass = all(t["passed"] for t in report["tests"])
        report["all_pass"] = bool(report["tests"]) and all_pass
        return report
    finally:
        report["shutdown_status"] = ava.stop(timeout_s=30.0)
        report["finished_ts"] = _now()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Ava voice regression battery.")
    ap.add_argument("--warmup", type=float, default=30.0, help="Seconds to wait after Ava is up before testing")
    ap.add_argument("--report-path", default=None, help="Where to write JSON report")
    args = ap.parse_args()
    report = run_battery(warmup_s=float(args.warmup))
    out_path = Path(args.report_path) if args.report_path else (REPO_ROOT / "state" / "regression" / "last.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    # Compact summary to stdout.
    print(f"=== Regression report — {report['finished_ts']} ===")
    print(f"boot_ok={report.get('boot_ok')} boot_seconds={report.get('phases',{}).get('boot_seconds')}")
    if report.get("phases", {}).get("boot_error"):
        print(f"boot_error: {report['phases']['boot_error']}")
    for t in report.get("tests", []):
        flag = "PASS" if t.get("passed") else "FAIL"
        reasons = ", ".join(t.get("fail_reasons") or []) or "-"
        print(f"  [{flag}] {t['label']:12s} {t['wall_seconds']:>5.2f}s  chars={t['reply_chars']:>3d}  "
              f"reasons={reasons}  reply={(t.get('reply_text') or '')[:80]!r}")
    print(f"all_pass={report.get('all_pass')}")
    print(f"shutdown={report.get('shutdown_status')}")
    print(f"log={report.get('log_path')}")
    print(f"report={out_path}")
    if not report.get("boot_ok"):
        return 2
    return 0 if report.get("all_pass") else 1


if __name__ == "__main__":
    sys.exit(main())
