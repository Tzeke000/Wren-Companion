"""jog_sign_calibrate.py — re-derive the Pixy HID jog sign convention, CHIP OFF.

Why (2026-08-25 handoff §5): the 08-21 calibration in attention_smooth_tool.py
("x=+10 -> scene shifts +8.5 deg right") was measured WHILE THE CHIP'S OWN
TRACKER WAS ON. If the chip counter-corrected during those bursts, the
measured direction is the chip's correction, not the jog's motion — and every
sign in the servo control law is inverted. That inversion exactly predicts
the observed chip-off behaviour ("almost like it's trying to avoid my head").

The decisive number: sign of scene shift for jog x=+10, chip verified OFF.

Sign frame (ties to the 08-25 VERIFIED odometry convention — absolute pan +10
measured as sx=+10 within 1.5%):
    phaseCorrelate(before, after) -> (sx, sy) px; deg = px * 68/160 = 0.425
    sx > 0  == pan increased        sy > 0 == tilt increased
    Centering a target at dx>0 (right of screen) REQUIRES sx < 0.
    Servo law is ux = -(GAIN*dx): for dx>0 it writes jog x<0.
    => servo is CORRECT iff jog x=+10 produces sx > 0 (the 08-21 claim).
       If jog x=+10 produces sx < 0, the control law sign is FLIPPED.

Safety: no soft rails here — bursts are small (<=9 deg) and alternate +/- so
they net to ~zero. Run ONLY with attention_smooth and sentry stopped (this
script cannot see runtime threads — check before running). Frames come from
the runtime's endpoint; never open the camera directly (runtime holds it).

Usage:  .venv/Scripts/python.exe scripts/jog_sign_calibrate.py [--rounds 2]
"""
from __future__ import annotations

import struct
import sys
import time
import urllib.request

import cv2
import numpy as np

sys.path.insert(0, ".")
from scripts.pixy_chip import open_dev, query, set_mode, is_on  # noqa: E402

FRAME_URL = "http://127.0.0.1:5876/api/v1/vision/latest_frame"
DEG_PER_PX = 68.0 / 160.0
REPORT = 32
STREAM_HZ = 20.0
SETTLE_S = 0.6
# (x_units, y_units, seconds) — alternate so each pair nets ~zero
BURSTS = [(+10.0, 0.0, 1.0), (-10.0, 0.0, 1.0),
          (0.0, +8.0, 0.8), (0.0, -8.0, 0.8)]


def _vec_report(x: float, y: float, z: float = 0.0) -> bytes:
    payload = [0x09, 0x63, 0x01, 0x20, 0x00, 0x0C, 0x00, 0x0C,
               *struct.pack("<fff", x, y, z)]
    return bytes(list(payload) + [0] * (REPORT - len(payload)))


def grab_small(prev_bytes: bytes | None = None,
               tries: int = 12) -> tuple[np.ndarray, bytes]:
    """Fresh downscaled gray from the runtime. Retries until the JPEG bytes
    differ from prev_bytes (the endpoint could serve a cached frame)."""
    last = b""
    for _ in range(tries):
        with urllib.request.urlopen(FRAME_URL, timeout=3) as r:
            last = r.read()
        if prev_bytes is None or last != prev_bytes:
            arr = cv2.imdecode(np.frombuffer(last, np.uint8), cv2.IMREAD_COLOR)
            if arr is not None and float(arr.std()) > 10.0:  # real image rule
                small = np.float32(cv2.cvtColor(
                    cv2.resize(arr, (160, 120)), cv2.COLOR_BGR2GRAY)) / 255.0
                return small, last
        time.sleep(0.15)
    raise RuntimeError("no fresh live frame from runtime (cached or blank)")


def stream_jog(dev, x: float, y: float, seconds: float) -> None:
    period = 1.0 / STREAM_HZ
    end = time.time() + seconds
    while time.time() < end:
        dev.write(_vec_report(x, y))
        time.sleep(period)
    dev.write(_vec_report(0.0, 0.0))


def ensure_chip_off(dev) -> None:
    mode, _ = query(dev)
    if is_on(mode):
        set_mode(dev, 0x00)
        time.sleep(0.4)
        mode, _ = query(dev)
    if is_on(mode) or mode is None:
        raise RuntimeError(f"chip refuses OFF (mode={mode}) — aborting, "
                           "measurement would be contaminated")


def main() -> int:
    rounds = 2
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    dev = open_dev()
    results: list[dict] = []
    try:
        print("chip -> OFF (verified)...")
        ensure_chip_off(dev)
        for rnd in range(rounds):
            for (bx, by, secs) in BURSTS:
                ensure_chip_off(dev)  # RE-ARM HAZARD: re-verify per burst
                before, raw = grab_small()
                stream_jog(dev, bx, by, secs)
                time.sleep(SETTLE_S)
                after, _ = grab_small(prev_bytes=raw)
                (sx, sy), resp = cv2.phaseCorrelate(before, after)
                rec = {"round": rnd, "jog": (bx, by), "secs": secs,
                       "sx_px": round(sx, 2), "sy_px": round(sy, 2),
                       "pan_deg": round(sx * DEG_PER_PX, 2),
                       "tilt_deg": round(sy * DEG_PER_PX, 2),
                       "resp": round(float(resp), 3)}
                results.append(rec)
                print(rec, flush=True)
                time.sleep(0.4)
    finally:
        try:
            print("chip -> back ON...")
            set_mode(dev, 0x01)
            time.sleep(0.4)
            mode, _ = query(dev)
            print(f"chip readback: mode={mode} (want on)")
        finally:
            dev.close()

    # ── verdict ──
    px = [r for r in results if r["jog"][0] > 0 and r["resp"] >= 0.10]
    py_ = [r for r in results if r["jog"][1] > 0 and r["resp"] >= 0.10]
    print("\n=== VERDICT ===")
    for name, rs, key, units, secs in (
            ("PAN", px, "pan_deg", 10.0, 1.0),
            ("TILT", py_, "tilt_deg", 8.0, 0.8)):
        if not rs:
            print(f"{name}: NO RELIABLE SAMPLES (resp too low)")
            continue
        mean = sum(r[key] for r in rs) / len(rs)
        rate = abs(mean) / (units * secs)
        agrees = mean > 0  # 08-21 claimed jog+ -> scene/pan +
        print(f"{name}: jog+{units:g} for {secs}s -> mean {mean:+.2f} deg "
              f"({rate:.3f} deg/s/unit)  "
              f"{'MATCHES 08-21 (servo sign OK)' if agrees else 'INVERTED vs 08-21 (SERVO SIGN FLIPPED — fix control law)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
