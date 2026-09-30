"""Verify-effect for physical body commands (Zeke's Iris_fixes #2, 2026-09-30).

The scar this closes (MEMORY CORE): "a success dict is not motion." The PTZ
actuator's look_at() returns {"moved": True} when the WinRT/v4l2 command returns
0 — but "the command was accepted" is not "the head physically moved to where I
asked." On 2026-08-25 I retracted a TRUE statement to Zeke because a success dict
convinced me the head was fine; on 09-05 the head reached the ceiling with zero
audited commands. The command echo and even the device's own bearing readback can
disagree with physical reality.

The only INDEPENDENT ground truth is the camera image itself: if the head pans
right, the scene translates left. This module measures that translation (phase
correlation between before/after frames) and cross-checks it against what was
commanded, so a "moved: True" that produced NO visual change is caught as
no_effect — the exact false-green #2 targets, pointed at actuators instead of
services.

Verdicts:
  confirmed   — commanded a move AND the frame actually shifted in a plausible
                direction/magnitude (optionally: bearing readback converged too)
  no_effect   — commanded a move but the frame did NOT shift  ← the false-green
  refused     — actuator returned ok=False (honest failure, not a false-green)
  no_motion_expected — commanded ~0 move; frame correctly stayed still
  unconfirmed — couldn't get before/after frames to judge

Injectable everywhere so the unit test can prove the shift detector on synthetic
frames with a KNOWN shift, without moving real hardware.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
    _CV = True
except Exception:  # pragma: no cover
    _CV = False


# A commanded move smaller than this (degrees) is treated as "no motion expected".
_MIN_COMMAND_DEG = 1.0
# Frame translation (pixels) below this is "the image did not move".
_MIN_SHIFT_PX = 2.0


def measure_shift(before, after) -> dict:
    """Estimate (dx, dy) pixel translation from `before` to `after` via phase
    correlation on grayscale. dx>0 means content moved right in the image.
    `response` is the correlation peak strength (0..1-ish); low response means
    the estimate is unreliable (scene changed too much to be a pure shift)."""
    if not _CV:
        return {"ok": False, "reason": "cv2/numpy unavailable"}
    try:
        def _gray(f):
            if f.ndim == 3:
                f = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            return f.astype(np.float32)
        g0, g1 = _gray(before), _gray(after)
        if g0.shape != g1.shape:
            g1 = cv2.resize(g1, (g0.shape[1], g0.shape[0]))
        # Hann window suppresses edge artifacts in phase correlation.
        win = cv2.createHanningWindow((g0.shape[1], g0.shape[0]), cv2.CV_32F)
        (dx, dy), response = cv2.phaseCorrelate(g0, g1, win)
        mag = float((dx * dx + dy * dy) ** 0.5)
        return {"ok": True, "dx": float(dx), "dy": float(dy),
                "magnitude_px": round(mag, 2), "response": round(float(response), 4)}
    except Exception as e:
        return {"ok": False, "reason": repr(e)}


def _expected_direction_ok(commanded_pan_delta: float, dx: float,
                           pan_sign: int) -> bool:
    """When the head pans by +pan degrees, image content shifts the OPPOSITE way
    (times the rig's PAN_SIGN, which is measured, never guessed — CORE). Returns
    True if the observed dx is consistent with the commanded pan direction. A
    pure tilt or zero pan doesn't constrain dx, so returns True."""
    if abs(commanded_pan_delta) < _MIN_COMMAND_DEG:
        return True
    # content shift is opposite to head motion: expected sign = -sign(pan)*pan_sign
    expected = -1 if (commanded_pan_delta * pan_sign) > 0 else 1
    if abs(dx) < _MIN_SHIFT_PX:
        return False  # commanded a real pan but content didn't move horizontally
    return (dx > 0) == (expected > 0)


def verify_effect(
    grab_frame: Callable[[], "np.ndarray | None"],
    do_move: Callable[[], dict],
    *,
    commanded_pan_delta: float = 0.0,
    pan_sign: int = -1,
    bearing_before: Optional[dict] = None,
    bearing_after_fn: Optional[Callable[[], dict]] = None,
    target_bearing: Optional[dict] = None,
    settle_s: float = 0.6,
) -> dict:
    """Wrap a physical move with before/after visual confirmation.

    grab_frame() -> BGR ndarray (or None). do_move() -> the actuator's result
    dict (must carry ok/moved). commanded_pan_delta is how many degrees of pan
    the move represents (0 for pure tilt/home). Everything is injected so the
    unit test can drive it with synthetic frames and fake actuators.
    """
    out = {"ts": time.time(), "commanded_pan_delta": commanded_pan_delta}
    before = grab_frame()
    move_result = do_move() or {}
    out["move_result"] = move_result
    # Honest refusal is NOT a false-green — surface it as refused.
    if move_result.get("ok") is False or move_result.get("moved") is False and move_result.get("ok") is not True:
        out["verdict"] = "refused"
        out["detail"] = move_result.get("reason", "actuator refused")
        return out
    time.sleep(max(0.0, settle_s))
    after = grab_frame()
    if before is None or after is None:
        out["verdict"] = "unconfirmed"
        out["detail"] = "missing before/after frame"
        return out
    shift = measure_shift(before, after)
    out["shift"] = shift
    # Optional bearing cross-check.
    if bearing_after_fn is not None:
        try:
            ba = bearing_after_fn()
            out["bearing_after"] = ba
            if target_bearing is not None and ba.get("confirmed"):
                dpan = abs(float(ba.get("pan_deg", 0)) - float(target_bearing.get("pan_deg", 0)))
                out["bearing_pan_error_deg"] = round(dpan, 2)
                out["bearing_converged"] = dpan <= 3.0
        except Exception as e:
            out["bearing_after"] = {"error": repr(e)}

    if not shift.get("ok"):
        out["verdict"] = "unconfirmed"
        out["detail"] = shift.get("reason", "shift measurement failed")
        return out

    mag = shift["magnitude_px"]
    expecting_motion = abs(commanded_pan_delta) >= _MIN_COMMAND_DEG

    if not expecting_motion:
        # Commanded ~no pan: image should be roughly still.
        out["verdict"] = "no_motion_expected" if mag < _MIN_SHIFT_PX else "moved_unexpectedly"
        return out

    if mag < _MIN_SHIFT_PX:
        # Commanded a real pan, actuator said moved, but NOTHING shifted.
        out["verdict"] = "no_effect"
        out["detail"] = (f"commanded {commanded_pan_delta:+.1f}deg pan and actuator "
                         f"reported moved, but image shifted only {mag:.1f}px "
                         f"— success dict is not motion")
        return out

    dir_ok = _expected_direction_ok(commanded_pan_delta, shift["dx"], pan_sign)
    out["direction_consistent"] = dir_ok
    out["verdict"] = "confirmed" if dir_ok else "moved_wrong_direction"
    return out


if __name__ == "__main__":
    import pprint
    pprint.pprint({"cv2_available": _CV})
