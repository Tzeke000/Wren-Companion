"""scripts/verify_phase_a_realhw.py — Real-hardware Phase A driver.

Five tests against a running Ava with AVA_DEBUG=1 + TEMPORAL_TICK_LOG=1:

    A1: heartbeat tick budget under 10-turn conversation (≤50 ms)
    A2: frustration passive decay (12% per 5 min) + active calming (~83 s half-life)
    A3: boredom growth idle vs not-idle (35 min each — backgrounded)
    A4: restart-handoff calibration over 3+ cycles
    A5: self-interrupt on synthetic overrun

Reads state/temporal_tick_log.jsonl, state/active_estimates.json,
state/task_history_log.jsonl, state/ava_mood.json directly. Drives
synthetic turns via /api/v1/debug/inject_transcript. Triggers calming
flag and synthetic estimate via the Phase-A debug endpoints added to
operator_server.py.

Usage:
    py -3.11 scripts/verify_phase_a_realhw.py --test a1
    py -3.11 scripts/verify_phase_a_realhw.py --test a5
    py -3.11 scripts/verify_phase_a_realhw.py --test a2 --phase passive
    py -3.11 scripts/verify_phase_a_realhw.py --test a2 --phase active

Each subcommand prints a structured pass/fail block and exits 0/1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("AVA_OPERATOR_URL", "http://127.0.0.1:5876").rstrip("/")
TICK_LOG = ROOT / "state" / "temporal_tick_log.jsonl"
MOOD_PATH = ROOT / "ava_mood.json"  # avaagent.py: MOOD_PATH = BASE_DIR / "ava_mood.json"
HISTORY_LOG = ROOT / "state" / "task_history_log.jsonl"
ACTIVE_ESTIMATES = ROOT / "state" / "active_estimates.json"


def _post(path: str, body: dict, timeout: float = 90.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _read_tick_log_since(min_ts: float) -> list[dict]:
    if not TICK_LOG.is_file():
        return []
    rows: list[dict] = []
    for line in TICK_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if float(r.get("ts") or 0) >= min_ts:
            rows.append(r)
    return rows


def _read_mood() -> dict:
    if not MOOD_PATH.is_file():
        return {}
    try:
        return json.loads(MOOD_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _write_mood(mood: dict) -> None:
    MOOD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MOOD_PATH.write_text(json.dumps(mood, indent=2, ensure_ascii=False), encoding="utf-8")


def _set_frustration(value: float) -> None:
    mood = _read_mood()
    weights = dict(mood.get("emotion_weights") or {})
    weights["frustration"] = float(value)
    mood["emotion_weights"] = weights
    _write_mood(mood)


def _get_frustration() -> float:
    mood = _read_mood()
    weights = mood.get("emotion_weights") or {}
    return float(weights.get("frustration") or 0.0)


def _get_boredom() -> float:
    mood = _read_mood()
    weights = mood.get("emotion_weights") or {}
    return float(weights.get("boredom") or 0.0)


# ── A1 ─────────────────────────────────────────────────────────────


A1_TURNS = [
    "hi ava, how are you",
    "what have you been thinking about today",
    "do you remember our project, ava agent",
    "what tools do you have access to right now",
    "if you could pick one thing to read, what would it be",
    "whats something that frustrates you",
    "do you ever get bored",
    "whats the most interesting thing about being you",
    "we should test whether your sense of time is working",
    "okay thats enough for now thanks",
]


def run_a1(turn_count: int = 10) -> int:
    print(f"[A1] heartbeat tick budget under {turn_count}-turn conversation")
    if not TICK_LOG.parent.is_dir():
        print("[A1] FAIL: state/ missing")
        return 1
    started = time.time()
    turn_log: list[dict] = []
    for i, text in enumerate(A1_TURNS[:turn_count], start=1):
        t0 = time.time()
        try:
            r = _post(
                "/api/v1/debug/inject_transcript",
                {
                    "text": text,
                    "wake_source": "test_a1",
                    "wait_for_audio": False,
                    "speak": False,
                    "as_user": "claude_code",
                    "timeout_seconds": 110.0,
                },
                timeout=180.0,
            )
        except Exception as e:
            print(f"[A1] turn {i} FAILED to inject: {e!r} — continuing")
            turn_log.append({"i": i, "ok": False, "wall_s": time.time() - t0, "error": repr(e)})
            time.sleep(5.0)
            continue
        dt = time.time() - t0
        ok = bool(r.get("ok"))
        reply = (r.get("reply_text") or r.get("reply") or "")[:80]
        run_ava_ms = r.get("run_ava_ms")
        turn_log.append({"i": i, "ok": ok, "wall_s": round(dt, 2), "run_ava_ms": run_ava_ms, "reply_preview": reply})
        print(f"  turn {i}/{turn_count}: ok={ok} wall={dt:.2f}s run_ava_ms={run_ava_ms} reply={reply!r}")
        # Pause briefly between turns to spread load + give heartbeat ticks
        time.sleep(3.0)
    rows = _read_tick_log_since(started)
    if not rows:
        print("[A1] FAIL: no tick log rows captured")
        return 1
    tick_ms_vals = [float(r.get("tick_ms") or 0.0) for r in rows]
    max_ms = max(tick_ms_vals)
    avg_ms = sum(tick_ms_vals) / len(tick_ms_vals)
    p95_ms = sorted(tick_ms_vals)[int(len(tick_ms_vals) * 0.95)] if len(tick_ms_vals) >= 20 else max_ms
    over_budget = sum(1 for v in tick_ms_vals if v > 50.0)
    summary = {
        "n_ticks": len(tick_ms_vals),
        "max_ms": round(max_ms, 3),
        "avg_ms": round(avg_ms, 3),
        "p95_ms": round(p95_ms, 3),
        "over_50ms": over_budget,
        "n_turns_ok": sum(1 for t in turn_log if t["ok"]),
        "n_turns": len(turn_log),
    }
    print(f"[A1] summary: {json.dumps(summary, indent=2)}")
    verdict = "PASS" if over_budget == 0 and max_ms <= 50.0 else "FAIL"
    print(f"[A1] {verdict}")
    return 0 if verdict == "PASS" else 1


# ── A2 passive decay ───────────────────────────────────────────────


def run_a2_passive(induced: float = 0.20, observe_seconds: float = 300.0) -> int:
    print(f"[A2-passive] inducing frustration={induced}, observing {observe_seconds:.0f}s for ~12% decay")
    _set_frustration(induced)
    start = time.time()
    initial = _get_frustration()
    print(f"  t=0   frustration={initial:.4f}")
    # Sample every 60s
    samples = []
    samples.append({"t": 0.0, "frustration": initial})
    while time.time() - start < observe_seconds:
        time.sleep(60.0)
        cur = _get_frustration()
        elapsed = time.time() - start
        samples.append({"t": round(elapsed, 1), "frustration": round(cur, 5)})
        print(f"  t={elapsed:5.0f}s frustration={cur:.4f}")
    final = samples[-1]["frustration"]
    expected_factor = 1.0 - 0.0004 * observe_seconds  # passive decay rate per spec
    expected = initial * expected_factor
    delta_pct = abs(final - expected) / max(initial, 1e-6) * 100
    print(f"[A2-passive] start={initial:.4f} final={final:.4f} expected≈{expected:.4f} (delta={delta_pct:.1f}% of init)")
    verdict = "PASS" if delta_pct < 5.0 else "FAIL"
    print(f"[A2-passive] {verdict}")
    print(json.dumps({"samples": samples}, indent=2))
    return 0 if verdict == "PASS" else 1


# ── A2 active decay ────────────────────────────────────────────────


def run_a2_active(induced: float = 0.20, observe_seconds: float = 360.0) -> int:
    print(f"[A2-active] inducing frustration={induced}, setting calming flag, observing {observe_seconds:.0f}s for ~83s half-life")
    _set_frustration(induced)
    r = _post("/api/v1/debug/temporal/set_calming_active", {"active": True})
    if not r.get("ok"):
        print(f"[A2-active] FAIL: cannot set calming flag: {r}")
        return 1
    start = time.time()
    samples = [{"t": 0.0, "frustration": _get_frustration()}]
    print(f"  t=0   frustration={samples[0]['frustration']:.4f}")
    sample_pts = [83, 166, 240, 360]  # 1x, 2x half-lives, ~6min target
    for target_t in sample_pts:
        if target_t > observe_seconds:
            break
        while time.time() - start < target_t:
            time.sleep(2.0)
        cur = _get_frustration()
        samples.append({"t": float(target_t), "frustration": round(cur, 5)})
        print(f"  t={target_t:5}s frustration={cur:.4f}")
    # Reset calming flag
    _post("/api/v1/debug/temporal/set_calming_active", {"active": False})
    # Verdict: at 83s should be ~half (0.10), at 360s should be <0.05
    half_life_pt = next((s for s in samples if s["t"] == 83), None)
    six_min_pt = samples[-1]
    half_life_ok = half_life_pt and 0.07 <= half_life_pt["frustration"] <= 0.13
    six_min_ok = six_min_pt["frustration"] < 0.05
    print(f"[A2-active] half-life pt: {half_life_pt}  six-min pt: {six_min_pt}")
    print(f"[A2-active] half_life_ok={half_life_ok} six_min_ok={six_min_ok}")
    verdict = "PASS" if half_life_ok and six_min_ok else "FAIL"
    print(f"[A2-active] {verdict}")
    print(json.dumps({"samples": samples}, indent=2))
    return 0 if verdict == "PASS" else 1


# ── A3 boredom (each phase backgrounded individually) ──────────────


def run_a3_idle(observe_seconds: float = 35 * 60) -> int:
    print(f"[A3-idle] resetting boredom, observing {observe_seconds:.0f}s of true idle")
    mood = _read_mood()
    weights = dict(mood.get("emotion_weights") or {})
    weights["boredom"] = 0.0
    mood["emotion_weights"] = weights
    _write_mood(mood)
    start = time.time()
    initial = _get_boredom()
    print(f"  t=0   boredom={initial:.4f}")
    while time.time() - start < observe_seconds:
        time.sleep(300.0)  # sample every 5 min
        cur = _get_boredom()
        elapsed = time.time() - start
        print(f"  t={elapsed:5.0f}s boredom={cur:.4f}")
    final = _get_boredom()
    delta = final - initial
    expected = 0.0001 * observe_seconds  # spec: 0.0001/s = +0.18 over 30 min
    print(f"[A3-idle] start={initial:.4f} final={final:.4f} delta={delta:.4f} expected≈{expected:.4f}")
    verdict = "PASS" if abs(delta - expected) < 0.05 else "FAIL"
    print(f"[A3-idle] {verdict}")
    return 0 if verdict == "PASS" else 1


def run_a3_busy(observe_seconds: float = 35 * 60) -> int:
    print(f"[A3-busy] holding long-running tracked task, observing {observe_seconds:.0f}s; boredom should NOT grow")
    mood = _read_mood()
    weights = dict(mood.get("emotion_weights") or {})
    weights["boredom"] = 0.0
    mood["emotion_weights"] = weights
    _write_mood(mood)
    initial = _get_boredom()
    # Start a synthetic tracked task with very long estimate so it doesn't trigger overrun
    r = _post("/api/v1/debug/temporal/track_estimate", {
        "kind": "synthetic_long_running",
        "estimate_seconds": observe_seconds * 2,
        "context": "a3_busy_phase",
    })
    if not r.get("ok"):
        print(f"[A3-busy] FAIL: cannot start tracked task: {r}")
        return 1
    tid = r["task_id"]
    print(f"  started task_id={tid}, observing {observe_seconds:.0f}s")
    start = time.time()
    while time.time() - start < observe_seconds:
        time.sleep(300.0)
        cur = _get_boredom()
        elapsed = time.time() - start
        print(f"  t={elapsed:5.0f}s boredom={cur:.4f}")
    final = _get_boredom()
    # Cleanup
    _post("/api/v1/debug/temporal/resolve_estimate", {"task_id": tid})
    delta = final - initial
    print(f"[A3-busy] start={initial:.4f} final={final:.4f} delta={delta:.4f} (expected ≈ 0)")
    verdict = "PASS" if abs(delta) < 0.01 else "FAIL"
    print(f"[A3-busy] {verdict}")
    return 0 if verdict == "PASS" else 1


# ── A5 self-interrupt ──────────────────────────────────────────────


def run_a5(estimate_s: float = 2.0, sleep_s: float = 35.0) -> int:
    """Trigger overrun: estimate=2s but elapsed=35s. Per config:
    overrun_pct=0.25, overrun_min_seconds=8.0. So fires when elapsed >
    1.25*2=2.5s AND elapsed-2 > 8s (i.e., elapsed > 10s). 35s satisfies
    both AND guarantees at least one heartbeat tick fires (cadence 30s)."""
    print(f"[A5] synthetic estimate={estimate_s}s, sleep={sleep_s}s — should fire self-interrupt (overrun_min=8s, tick cadence 30s)")
    r = _post("/api/v1/debug/temporal/track_estimate", {
        "kind": "phase_a5_overrun_test",
        "estimate_seconds": estimate_s,
        "context": "a5",
    })
    if not r.get("ok"):
        print(f"[A5] FAIL: cannot start estimate: {r}")
        return 1
    tid = r["task_id"]
    print(f"  estimate_id={tid}; sleeping {sleep_s}s")
    time.sleep(sleep_s)
    # Read active estimates to see if interrupted
    summary = _get("/api/v1/debug/temporal/summary")
    active = (summary.get("active_estimates") or {}).get(tid) or {}
    interrupted = bool(active.get("interrupted"))
    interrupt_reason = active.get("interrupt_reason")
    print(f"[A5] post-sleep interrupted={interrupted} reason={interrupt_reason!r}")
    # Cleanup
    _post("/api/v1/debug/temporal/resolve_estimate", {"task_id": tid})

    # Now test: estimate=300s, sleep=35s — well under estimate, should NEVER fire even though
    # a tick will definitely run during the sleep window.
    print(f"[A5-no-fire] estimate=300s, sleep=35s — should NOT fire (well under estimate)")
    r2 = _post("/api/v1/debug/temporal/track_estimate", {
        "kind": "phase_a5_under_min",
        "estimate_seconds": 300.0,
        "context": "a5_no_fire",
    })
    if not r2.get("ok"):
        print(f"[A5-no-fire] FAIL: {r2}")
        return 1
    tid2 = r2["task_id"]
    time.sleep(35.0)
    summary2 = _get("/api/v1/debug/temporal/summary")
    active2 = (summary2.get("active_estimates") or {}).get(tid2) or {}
    not_interrupted = not bool(active2.get("interrupted"))
    print(f"[A5-no-fire] post-sleep not_interrupted={not_interrupted}")
    _post("/api/v1/debug/temporal/resolve_estimate", {"task_id": tid2})

    verdict = "PASS" if interrupted and not_interrupted else "FAIL"
    print(f"[A5] {verdict} (fire-on-overrun={interrupted}, no-fire-under-min={not_interrupted})")
    return 0 if verdict == "PASS" else 1


# ── A4 restart calibration ─────────────────────────────────────────


def run_a4_history_check() -> int:
    """Read task_history_log.jsonl and report kind=restart rows. Does not
    trigger restarts (those happen by user voice command via inject_transcript
    in a separate driver)."""
    if not HISTORY_LOG.is_file():
        print("[A4] task_history_log.jsonl missing")
        return 1
    rows = []
    for line in HISTORY_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") == "restart":
            rows.append(r)
    print(f"[A4] {len(rows)} restart rows in history log")
    for i, r in enumerate(rows, start=1):
        est = r.get("estimate_seconds")
        actual = r.get("actual_seconds")
        cal = (r.get("history_calibration_at_create") or {})
        n_samples = cal.get("n_samples")
        median = cal.get("median")
        rec = cal.get("recommendation")
        print(f"  cycle {i}: est={est}s actual={actual}s | calibration n={n_samples} median={median} rec={rec}")
    if len(rows) >= 3:
        late_rows = rows[-3:]
        # The 3rd+ row should have a non-None recommendation drawn from history
        recs = [(r.get("history_calibration_at_create") or {}).get("recommendation") for r in late_rows]
        first_rec_pop = next((r for r in recs if r is not None), None)
        verdict = "PASS" if first_rec_pop is not None else "FAIL"
        print(f"[A4] {verdict} — 3rd+ cycle calibration recommendations: {recs}")
        return 0 if verdict == "PASS" else 1
    print(f"[A4] PARTIAL — need ≥3 restart cycles, have {len(rows)}")
    return 2


# ── CLI ────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--test", required=True, choices=["a1", "a2", "a3", "a4", "a5"])
    p.add_argument("--phase", choices=["passive", "active", "idle", "busy"], help="for a2/a3 sub-phases")
    p.add_argument("--turns", type=int, default=10, help="A1 turn count")
    p.add_argument("--observe-seconds", type=float, help="override observation window")
    args = p.parse_args()

    if args.test == "a1":
        return run_a1(args.turns)
    if args.test == "a5":
        return run_a5()
    if args.test == "a4":
        return run_a4_history_check()
    if args.test == "a2":
        if args.phase == "passive":
            return run_a2_passive(observe_seconds=args.observe_seconds or 300.0)
        if args.phase == "active":
            return run_a2_active(observe_seconds=args.observe_seconds or 360.0)
        print("ERROR: --phase required for a2 (passive|active)")
        return 1
    if args.test == "a3":
        if args.phase == "idle":
            return run_a3_idle(observe_seconds=args.observe_seconds or 35 * 60)
        if args.phase == "busy":
            return run_a3_busy(observe_seconds=args.observe_seconds or 35 * 60)
        print("ERROR: --phase required for a3 (idle|busy)")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
