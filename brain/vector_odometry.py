"""vector_odometry.py — persistent dead-reckoning heading (+rough position) for the body.

THE GAP that stranded me 2026-07-14: I could locate my body in the tower cam (with
Zeke's help) but never knew which way it FACED, so every blind translation gambled on
direction and I couldn't reliably drive home. This integrates every drive command into
a heading estimate so I always know my orientation between camera fixes.

Differential-drive kinematics for a burst (treads at lw,rw mm/s for t seconds):
    v      = (lw + rw) / 2                 # forward speed, mm/s
    omega  = YAW_SIGN * (rw - lw) / WB     # yaw rate, rad/s
    dtheta = omega * t
    heading += dtheta                      # integrate orientation
    x += v*t*cos(heading_mid); y += v*t*sin(heading_mid)   # midpoint position integ.

State: state/vector/odometry.json {heading_deg, x_mm, y_mm, wheelbase_mm, calibrated, ts}
- heading_deg: 0..360, 0 = the heading at the last reset/fix, increasing per YAW_SIGN.
- Correct drift with a camera/known fix via set_heading(); reset() zeroes everything.

CALIBRATION (needs live driving — the ONLY untested part): WHEELBASE_MM (track width) and
YAW_SIGN. Do a measured in-place spin (lw=-s, rw=s for t) that visibly completes exactly
one full turn, then solve WB = YAW_SIGN*(rw-lw)*t / (2*pi). Until then the heading is
DIRECTIONALLY right (turns register, spins cancel) but the DEGREE SCALE is provisional.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ODO = REPO / "state" / "vector" / "odometry.json"

WHEELBASE_MM = 48.0   # PROVISIONAL — Vector track width (mm); calibrate via a measured spin
WHEEL_SCALE = 1.0     # PROVISIONAL — commanded mm/s -> actual mm/s
YAW_SIGN = 1.0        # PROVISIONAL — +1 if (rw-lw)>0 turns CCW; flip after calibration

_lock = threading.Lock()


def _default() -> dict:
    return {"heading_deg": 0.0, "x_mm": 0.0, "y_mm": 0.0,
            "wheelbase_mm": WHEELBASE_MM, "calibrated": False, "ts": time.time()}


def get() -> dict:
    try:
        d = json.loads(ODO.read_text(encoding="utf-8"))
        # heal any missing keys
        base = _default()
        base.update(d)
        return base
    except Exception:
        return _default()


def _save(s: dict) -> dict:
    try:
        ODO.parent.mkdir(parents=True, exist_ok=True)
        s["ts"] = time.time()
        tmp = ODO.with_suffix(".tmp")
        tmp.write_text(json.dumps(s), encoding="utf-8")
        tmp.replace(ODO)
    except Exception:
        pass
    return s


def apply_drive(lw, rw, seconds) -> dict:
    """Integrate one differential-drive burst into the heading/position estimate.
    Call with the ACTUAL elapsed seconds (post edge-guard abort), not the request."""
    with _lock:
        s = get()
        wb = float(s.get("wheelbase_mm") or WHEELBASE_MM)
        lw = float(lw) * WHEEL_SCALE
        rw = float(rw) * WHEEL_SCALE
        t = max(0.0, float(seconds))
        v = (lw + rw) / 2.0
        omega = YAW_SIGN * (rw - lw) / wb
        h0 = math.radians(s["heading_deg"])
        dtheta = omega * t
        h_mid = h0 + dtheta / 2.0
        s["x_mm"] += v * t * math.cos(h_mid)
        s["y_mm"] += v * t * math.sin(h_mid)
        s["heading_deg"] = math.degrees(h0 + dtheta) % 360.0
        return _save(s)


def set_heading(deg, x=None, y=None) -> dict:
    """Correct to a known heading (from a camera fix / known dock pose)."""
    with _lock:
        s = get()
        s["heading_deg"] = float(deg) % 360.0
        if x is not None:
            s["x_mm"] = float(x)
        if y is not None:
            s["y_mm"] = float(y)
        return _save(s)


def reset(heading_deg=0.0, x=0.0, y=0.0) -> dict:
    with _lock:
        s = _default()
        s["heading_deg"] = float(heading_deg) % 360.0
        s["x_mm"] = float(x)
        s["y_mm"] = float(y)
        return _save(s)


def set_calibration(wheelbase_mm=None, yaw_sign=None, mark=True) -> dict:
    """Persist a live-measured wheelbase (and optional yaw sign) and mark calibrated."""
    global YAW_SIGN
    with _lock:
        s = get()
        if wheelbase_mm is not None:
            s["wheelbase_mm"] = float(wheelbase_mm)
        if yaw_sign is not None:
            YAW_SIGN = float(yaw_sign)
            s["yaw_sign"] = float(yaw_sign)
        s["calibrated"] = bool(mark)
        return _save(s)


def bearing_to(tx, ty) -> float:
    """Absolute bearing (deg, 0..360) from current position to a target point."""
    s = get()
    return math.degrees(math.atan2(float(ty) - s["y_mm"],
                                   float(tx) - s["x_mm"])) % 360.0


def turn_needed(target_heading_deg) -> float:
    """Signed shortest turn (deg; sign follows YAW_SIGN convention) to face target."""
    s = get()
    return (float(target_heading_deg) - s["heading_deg"] + 180.0) % 360.0 - 180.0
