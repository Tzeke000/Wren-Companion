"""Always-on head odometry — knowing where I'm pointed, whoever moved me.

Built 2026-08-25 because Zeke turned my head with his hands while I was facing
the wall, and I had no idea it had happened. He asked the right question:
*"can you not see that your head degree changed? even if you're not the one
who moved it? we should fix that."*

The answer was: I can — but only while actively following someone. The visual
odometry that measures my own motion lived inside the pursuit servo loop, and
I had switched that loop OFF before he changed. So I disabled the one sense
that would have felt his hand, then was surprised I hadn't felt it.

Two separate things I had been treating as one:
  * The DEVICE REGISTER echoes the last instruction it was given. It reported
    pan −120 / tilt +20 with `confirmed: True` while the head was physically
    pointing somewhere else entirely. It is a record of intent, not of pose.
  * VISUAL ODOMETRY measures the world sliding across the frame, so it catches
    ANY movement regardless of cause — commanded, hand-pushed, or knocked.
    Proven 2026-08-25: sweeps driven from a separate process were tracked
    correctly by the running loop, which had not issued them.

So this runs the measurement continuously and cheaply (~2ms on a 160x120
grayscale), independent of whether anything is being tracked. It also does
something the servo version could not: because it knows what motion was
COMMANDED, motion that appears without a command is EXTERNAL — someone moved
me — and that is worth noticing rather than silently absorbing.
"""
from __future__ import annotations

import threading
import time
from typing import Any

_DEG_PER_PX = 68.0 / 160.0    # same constant the servo uses; verified to
                              # <1.5% against known angles on 2026-08-25
_MIN_RESP = 0.10              # below this the correlation is not trustworthy
# ── SAMPLE RATE = CAMERA RATE (fixed 2026-08-25, first live test) ──
# At 10Hz this MISSED a 30 degree snap entirely: 157 clean updates, zero
# skips, and a final estimate of -0.7 deg after the head had physically moved
# +30. Cause: a fast absolute move covers ~70px on the 160px working image
# between two samples, and phase correlation ALIASES at that size — it does
# not fail loudly, it returns a small plausible number. Exactly the collapse I
# measured this morning at 30 degree steps, and I should have predicted it.
#
# Zeke had already named the fix before I hit the bug: "maybe having the cam at
# 30 fps and then the facial recognition also at 30 fps and the tracking ... at
# 30 fps will help." Sampling at camera rate keeps the per-sample shift small
# enough to measure. The reject threshold is now well BELOW the aliasing point
# rather than at it, so an unmeasurable jump is refused and counted instead of
# quietly averaged in as nearly-zero.
_MAX_SHIFT_PX = 40            # ~17 deg/sample; beyond this, reject not alias
_PERIOD_S = 1.0 / 30.0        # match the 30fps capture loop
_EXTERNAL_DEG = 3.0           # uncommanded motion beyond this = someone moved me

_state: dict[str, Any] = {
    "running": False, "pan_deg": 0.0, "tilt_deg": 0.0,
    "updates": 0, "skips": 0, "starved": 0,
    "external_events": 0, "last_external": None,
    "last_update_ts": 0.0, "started_ts": 0.0,
}
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
# Degrees the SERVO says it commanded since the last odometry sample. The servo
# calls note_commanded(); anything measured beyond it had another cause.
_commanded = {"pan": 0.0, "tilt": 0.0}


def _log(msg: str) -> None:
    print(f"[head_odometry] {msg}", flush=True)


def note_commanded(pan_deg: float, tilt_deg: float) -> None:
    """Called by whatever is driving the head, so this module can tell
    self-motion from being pushed. Never blocks; best-effort."""
    with _lock:
        _commanded["pan"] += float(pan_deg)
        _commanded["tilt"] += float(tilt_deg)


def seed(pan_deg: float, tilt_deg: float) -> None:
    """Anchor the estimate to a known bearing (e.g. straight after an absolute
    move, when the register IS momentarily truthful)."""
    with _lock:
        _state["pan_deg"] = float(pan_deg)
        _state["tilt_deg"] = float(tilt_deg)


def status() -> dict:
    with _lock:
        s = dict(_state)
    s["stale_s"] = round(time.time() - float(s.get("last_update_ts") or 0.0), 2)
    return s


def _loop() -> None:
    import cv2
    import numpy as np
    from brain import frame_store

    prev = None
    prev_ts = 0.0
    _log("running")
    while not _stop.is_set():
        try:
            res = frame_store.get_buffered_frame(max_age_sec=1.0)
            if res.frame is None or res.capture_ts <= prev_ts:
                with _lock:
                    _state["starved"] += 1
                _stop.wait(_PERIOD_S)
                continue
            small = np.float32(cv2.cvtColor(
                cv2.resize(res.frame, (160, 120)), cv2.COLOR_BGR2GRAY)) / 255.0
            if prev is not None:
                (sx, sy), resp = cv2.phaseCorrelate(prev, small)
                if (resp >= _MIN_RESP and abs(sx) < _MAX_SHIFT_PX
                        and abs(sy) < _MAX_SHIFT_PX):
                    d_pan = sx * _DEG_PER_PX
                    d_tilt = sy * _DEG_PER_PX
                    with _lock:
                        cp = _commanded["pan"]
                        ct = _commanded["tilt"]
                        _commanded["pan"] = 0.0
                        _commanded["tilt"] = 0.0
                        _state["pan_deg"] = max(-150.0, min(
                            150.0, _state["pan_deg"] + d_pan))
                        _state["tilt_deg"] = max(-60.0, min(
                            90.0, _state["tilt_deg"] + d_tilt))
                        _state["updates"] += 1
                        _state["last_update_ts"] = time.time()
                        # Motion nobody asked for. Not an error — Zeke turning
                        # my head is a perfectly good reason for it to move —
                        # but I should KNOW, rather than absorb it silently and
                        # then be confidently wrong about where I am pointed.
                        unexplained = max(abs(d_pan - cp), abs(d_tilt - ct))
                        if unexplained >= _EXTERNAL_DEG:
                            _state["external_events"] += 1
                            _state["last_external"] = {
                                "ts": time.time(),
                                "measured": [round(d_pan, 1), round(d_tilt, 1)],
                                "commanded": [round(cp, 1), round(ct, 1)],
                                "unexplained_deg": round(unexplained, 1)}
                else:
                    with _lock:
                        _state["skips"] += 1
            prev, prev_ts = small, res.capture_ts
        except Exception as e:  # noqa: BLE001 — must never take the body down
            _log(f"tick failed (continuing): {e!r}")
        _stop.wait(_PERIOD_S)
    _log("stopped")


def start() -> dict:
    global _thread
    with _lock:
        if _state["running"]:
            return {"ok": True, "already": True}
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="head_odometry", daemon=True)
    _thread.start()
    with _lock:
        _state["running"] = True
        _state["started_ts"] = time.time()
    return {"ok": True, "started": True}


def stop() -> dict:
    _stop.set()
    with _lock:
        _state["running"] = False
    return {"ok": True, "stopped": True}
