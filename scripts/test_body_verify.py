"""Adversarial test for body_verify (Zeke's Iris_fixes #2, 2026-09-30).

Proves the verify-effect mechanism WITHOUT moving real hardware, by driving it
with synthetic frames of a KNOWN shift and fake actuators. The critical case is
#3: an actuator that LIES (returns moved:True) while the image stays static must
be caught as 'no_effect' — a success dict is not motion.

Run: .venv/Scripts/python.exe scripts/test_body_verify.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402
from brain import body_verify as bv  # noqa: E402

PASS, FAIL = [], []


def check(name, got, expected):
    ok = got == expected
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}, expected {expected!r}")


# A textured frame (phase correlation needs texture; random noise is ideal).
rng = np.random.default_rng(42)
frame = (rng.random((240, 320, 3)) * 255).astype("uint8")


def shifted(img, dx):
    """Roll content right by dx px (dx>0 => content moves right)."""
    return np.roll(img, dx, axis=1)


print("== DETECTOR 1: recovers a known +30px shift ==")
s = bv.measure_shift(frame, shifted(frame, 30))
print(f"  dx={s['dx']:.1f} mag={s['magnitude_px']} response={s['response']}")
check("dx sign positive", s["dx"] > 5, True)
check("magnitude ~30", 25 <= s["magnitude_px"] <= 35, True)

print("\n== DETECTOR 2: identical frames -> ~0 shift ==")
s = bv.measure_shift(frame, frame.copy())
check("identical -> tiny magnitude", s["magnitude_px"] < 2.0, True)

print("\n== VERIFY 3 (THE FALSE-GREEN): actuator says moved, image static -> no_effect ==")
r = bv.verify_effect(
    grab_frame=lambda: frame,                 # same frame both times = no motion
    do_move=lambda: {"ok": True, "moved": True},
    commanded_pan_delta=10.0, pan_sign=-1, settle_s=0.0,
)
check("lying-actuator -> no_effect", r["verdict"], "no_effect")

print("\n== VERIFY 4: real move, image shifts correctly -> confirmed ==")
_frames = iter([frame, shifted(frame, 30)])   # +pan, pan_sign=-1 => content right (dx>0)
r = bv.verify_effect(
    grab_frame=lambda: next(_frames),
    do_move=lambda: {"ok": True, "moved": True},
    commanded_pan_delta=10.0, pan_sign=-1, settle_s=0.0,
)
check("real-move -> confirmed", r["verdict"], "confirmed")

print("\n== VERIFY 5: actuator refuses -> refused (honest failure, not false-green) ==")
r = bv.verify_effect(
    grab_frame=lambda: frame,
    do_move=lambda: {"ok": False, "moved": False, "reason": "no PTZ actuator"},
    commanded_pan_delta=10.0, settle_s=0.0,
)
check("refused-actuator -> refused", r["verdict"], "refused")

print("\n== VERIFY 6: commanded ~0 pan, image static -> no_motion_expected ==")
r = bv.verify_effect(
    grab_frame=lambda: frame,
    do_move=lambda: {"ok": True, "moved": True},
    commanded_pan_delta=0.0, settle_s=0.0,
)
check("no-command -> no_motion_expected", r["verdict"], "no_motion_expected")

print("\n== VERIFY 7: commanded +pan but image shifts WRONG way -> moved_wrong_direction ==")
_frames = iter([frame, shifted(frame, -30)])  # content LEFT, opposite of expected
r = bv.verify_effect(
    grab_frame=lambda: next(_frames),
    do_move=lambda: {"ok": True, "moved": True},
    commanded_pan_delta=10.0, pan_sign=-1, settle_s=0.0,
)
check("wrong-direction -> moved_wrong_direction", r["verdict"], "moved_wrong_direction")

print("\n== VERIFY 8: frames unavailable -> unconfirmed (never a false green) ==")
r = bv.verify_effect(
    grab_frame=lambda: None,
    do_move=lambda: {"ok": True, "moved": True},
    commanded_pan_delta=10.0, settle_s=0.0,
)
check("no-frames -> unconfirmed", r["verdict"], "unconfirmed")

print("\n" + "=" * 50)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL VERIFY-EFFECT CHECKS HELD — a success dict alone can never read as motion.")
