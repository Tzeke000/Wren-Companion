"""scripts/verify_temporal_sense.py — Live verification of B3 (2026-05-03).

Exercises brain/temporal_sense.py and brain/temporal_metabolism.py against
a stub `g` dict + temp state files. Avoids the long Ava boot. Tests the
math + threshold logic per the work order's verification list.

Usage:
  py -3.11 scripts/verify_temporal_sense.py

Verifies:
  1. Frustration passive decay (target: 12% per 5 min from 0.20)
  2. Frustration active exponential decay (target: 0.20 -> < 0.05 in 6 min)
  3. Boredom growth (target: +0.18 over 30 min from 0)
  4. Boredom doesn't grow when processing_active=True
  5. Estimate tracking + 25%-and-min-threshold self-interrupt
  6. Self-interrupt does NOT fire on small tasks (10s -> 13s = 30% but only 3s)
  7. Historical logging persists across resolve_estimate calls
  8. calibrate_from_history reads back the rolled-up median
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Make brain/ importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use a temp BASE_DIR so we don't pollute live state/
TMP_DIR = Path(tempfile.mkdtemp(prefix="ava_temporal_verify_"))
print(f"[verify] using temp BASE_DIR: {TMP_DIR}")

# Ensure ts module reads our config (uses repo-relative path)
import brain.temporal_sense as ts  # noqa: E402

# Stub mood store
_mood_state: dict = {
    "emotion_weights": {
        "frustration": 0.20,
        "boredom": 0.0,
        "calmness": 0.20,
        "interest": 0.10,
    }
}

def _load_mood():
    return dict(_mood_state)

def _save_mood(m):
    _mood_state.clear()
    _mood_state.update(m)


def _reset_mood(frustration=0.0, boredom=0.0):
    _mood_state.clear()
    _mood_state.update({
        "emotion_weights": {
            "frustration": frustration,
            "boredom": boredom,
            "calmness": 0.20,
            "interest": 0.10,
        }
    })


def _make_g(processing=False, calming=False, last_interaction_offset=0.0,
            voice_state="passive"):
    """Build a stub `g` dict that the temporal_sense module can read."""
    class _StubVoiceLoop:
        _state = voice_state
    g = {
        "BASE_DIR": str(TMP_DIR),
        "load_mood": _load_mood,
        "save_mood": _save_mood,
        "_turn_in_progress": processing,
        "_calming_activity_active": calming,
        "_last_user_interaction_ts": time.time() - last_interaction_offset,
        "_voice_loop": _StubVoiceLoop(),
        "_process_start_ts": time.time(),
    }
    return g


# ── TEST 1: Frustration passive decay ──────────────────────────────────────


def test_frustration_passive_decay():
    print("\n[TEST 1] Frustration passive decay (target: ~12% per 5 min from 0.20)")
    _reset_mood(frustration=0.20)
    g = _make_g(processing=False, last_interaction_offset=2000)  # idle
    # Simulate 5 minutes of decay
    ts.apply_state_decay_growth(g, dt_seconds=300.0)
    final = _mood_state["emotion_weights"]["frustration"]
    expected_low, expected_high = 0.16, 0.18  # ~12% decay -> 0.176
    pass_check = expected_low <= final <= expected_high
    mark = "PASS" if pass_check else "FAIL"
    print(f"  start=0.20  after 5min={final:.4f}  expected≈0.176±0.015  -> {mark}")
    return pass_check


# ── TEST 2: Frustration active exponential ────────────────────────────────


def test_frustration_active_decay():
    print("\n[TEST 2] Frustration active exponential decay (target: 0.20 -> <0.05 in 6 min)")
    _reset_mood(frustration=0.20)
    g = _make_g(processing=False, calming=True, last_interaction_offset=2000)
    # Simulate 6 minutes of decay
    ts.apply_state_decay_growth(g, dt_seconds=360.0)
    final = _mood_state["emotion_weights"]["frustration"]
    pass_check = final < 0.05
    mark = "PASS" if pass_check else "FAIL"
    print(f"  start=0.20  after 6min calming={final:.4f}  expected<0.05  -> {mark}")
    return pass_check


# ── TEST 3: Boredom growth when idle ───────────────────────────────────────


def test_boredom_growth_idle():
    print("\n[TEST 3] Boredom growth (target: +0.18 over 30 min from idle)")
    _reset_mood(frustration=0.0, boredom=0.0)
    g = _make_g(processing=False, last_interaction_offset=2000)  # 33 min idle
    ts.apply_state_decay_growth(g, dt_seconds=1800.0)
    final = _mood_state["emotion_weights"]["boredom"]
    pass_check = 0.16 <= final <= 0.20
    mark = "PASS" if pass_check else "FAIL"
    print(f"  start=0.0  after 30min idle={final:.4f}  expected≈0.18±0.02  -> {mark}")
    return pass_check


# ── TEST 4: Boredom does NOT grow when processing_active=True ─────────────


def test_boredom_blocked_when_processing():
    print("\n[TEST 4] Boredom does NOT grow when processing_active=True")
    _reset_mood(frustration=0.0, boredom=0.0)
    g = _make_g(processing=True, last_interaction_offset=2000)
    ts.apply_state_decay_growth(g, dt_seconds=1800.0)
    final = _mood_state["emotion_weights"]["boredom"]
    pass_check = final == 0.0
    mark = "PASS" if pass_check else "FAIL"
    print(f"  start=0.0  after 30min with processing_active=True  boredom={final:.4f}  expected=0.0  -> {mark}")
    return pass_check


# ── TEST 5: Estimate tracking + 25%-and-min-threshold self-interrupt ──────


def test_self_interrupt_fires_on_overrun():
    print("\n[TEST 5] Self-interrupt fires on 5-min task at 30% overrun (1.5min over)")
    g = _make_g(processing=False)
    # Start an estimate: 5 minutes
    task_id = ts.track_estimate(g, estimate_seconds=300.0, kind="test_overrun_fires", context="verify")
    # Simulate 6:30 elapsed (30% over = 90s, well above 8s minimum)
    estimates = ts._read_active_estimates(g)
    estimates[task_id]["created_at"] = time.time() - 390.0
    ts._write_active_estimates(g, estimates)
    summary = ts._check_overrun(g, time.time())
    fired = summary["fired_overrun"] >= 1
    estimates_after = ts._read_active_estimates(g)
    interrupted = estimates_after[task_id].get("interrupted")
    mark = "PASS" if (fired and interrupted) else "FAIL"
    print(f"  fired_overrun={summary['fired_overrun']}  interrupted={interrupted}  -> {mark}")
    return fired and interrupted


def test_self_interrupt_no_fire_below_minimum():
    print("\n[TEST 6] Self-interrupt does NOT fire on 10s task at 30% overrun (3s over, below 8s min)")
    g = _make_g(processing=False)
    task_id = ts.track_estimate(g, estimate_seconds=10.0, kind="test_overrun_min", context="verify")
    # Simulate 13s elapsed (30% over = 3s, below 8s min)
    estimates = ts._read_active_estimates(g)
    estimates[task_id]["created_at"] = time.time() - 13.0
    ts._write_active_estimates(g, estimates)
    summary = ts._check_overrun(g, time.time())
    estimates_after = ts._read_active_estimates(g)
    not_fired = summary["fired_overrun"] == 0
    not_interrupted = not estimates_after[task_id].get("interrupted")
    mark = "PASS" if (not_fired and not_interrupted) else "FAIL"
    print(f"  fired_overrun={summary['fired_overrun']}  interrupted={estimates_after[task_id].get('interrupted')}  -> {mark}")
    return not_fired and not_interrupted


# ── TEST 7: Historical logging + calibration ──────────────────────────────


def test_historical_logging_and_calibration():
    print("\n[TEST 7] Historical logging persists; calibrate reads median")
    g = _make_g()
    # Run 5 "research" tasks with known actuals
    actuals = [120.0, 130.0, 140.0, 150.0, 160.0]  # median = 140
    for i, actual in enumerate(actuals):
        tid = ts.track_estimate(g, estimate_seconds=100.0, kind="test_research", context=f"trial_{i}")
        ts.resolve_estimate(g, tid, actual_seconds=actual)
    # Now calibrate
    cal = ts.calibrate_from_history(g, kind="test_research")
    median = cal.get("median")
    rec = cal.get("recommendation")
    n = cal.get("n_samples")
    pass_check = (n == 5 and abs((median or 0) - 140.0) < 0.1 and rec == 140.0)
    mark = "PASS" if pass_check else "FAIL"
    print(f"  n={n}  median={median}  recommendation={rec}  expected median=140  -> {mark}")
    return pass_check


# ── TEST 8: is_idle three-and gate ─────────────────────────────────────────


def test_is_idle_gate():
    print("\n[TEST 8] is_idle() three-and gate")
    # Clean up any leftover active estimates from earlier tests so
    # processing_active doesn't see them as in-flight work.
    g_clean = _make_g(processing=False)
    ts._write_active_estimates(g_clean, {})
    # All three TRUE -> idle=True
    g = _make_g(processing=False, last_interaction_offset=2000, voice_state="passive")
    r1 = ts.is_idle(g)
    # processing_active=True -> idle=False
    g = _make_g(processing=True, last_interaction_offset=2000, voice_state="passive")
    r2 = ts.is_idle(g)
    # elapsed too short -> idle=False
    g = _make_g(processing=False, last_interaction_offset=60, voice_state="passive")
    r3 = ts.is_idle(g)
    # voice_state=listening -> idle=False
    g = _make_g(processing=False, last_interaction_offset=2000, voice_state="listening")
    r4 = ts.is_idle(g)
    pass_check = (r1 is True and r2 is False and r3 is False and r4 is False)
    mark = "PASS" if pass_check else "FAIL"
    print(f"  all_three_true={r1}  processing={r2}  short_elapsed={r3}  voice_listening={r4}  -> {mark}")
    return pass_check


def main() -> int:
    results = []
    try:
        results.append(("frustration_passive_decay", test_frustration_passive_decay()))
        results.append(("frustration_active_decay", test_frustration_active_decay()))
        results.append(("boredom_growth_idle", test_boredom_growth_idle()))
        results.append(("boredom_blocked_when_processing", test_boredom_blocked_when_processing()))
        results.append(("self_interrupt_fires_on_overrun", test_self_interrupt_fires_on_overrun()))
        results.append(("self_interrupt_no_fire_below_minimum", test_self_interrupt_no_fire_below_minimum()))
        results.append(("historical_logging_and_calibration", test_historical_logging_and_calibration()))
        results.append(("is_idle_gate", test_is_idle_gate()))
    finally:
        # Cleanup
        try:
            shutil.rmtree(TMP_DIR)
        except Exception:
            pass

    print()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"=== {passed}/{total} tests passed ===")
    for name, ok in results:
        print(f"  {'OK ' if ok else 'FAIL'}  {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
