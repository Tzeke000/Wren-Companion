# SELF_ASSESSMENT: I am Iris's gaze — flip-on tracking that keeps my body's
# camera on Zeke's face or a moving object, like looking at someone talking.
"""Face / motion tracking for my Vector body (2026-07-20, Zeke pre-sleep:
"face track and object track... flip it on and off... track my face to look
at me while I'm moving, or something in my hand").

MODES
  face   — firmware face detection (RobotObservedFace events; the engine's
           own detector, tuned for this camera) → keep the face centered.
  motion — PC-side frame differencing on my camera feed → largest moving
           blob's centroid → follow it. Covers "thing in Zeke's hand."

CONTROL (the GrowBot face-track benchmark shape: sense → error → small
corrections, fast):
  vertical error   → head angle nudges (always allowed, docked-safe)
  horizontal error → tiny wheel turn pulses (ONLY undocked; docked = head-only)
  deadband ~8% of frame so I don't twitch; corrections rate-limited ~6Hz.

SAFETY: refuses without a live session; wheel pulses suppressed while docked/
cliff/picked_up; auto-off after TARGET_LOST_OFF_S without a target and hard
cap MAX_RUN_S per activation (no endless hunting while nobody's watching).
Runs in a daemon thread; body_track on/off/status from my turn. brain/* —
INERT until imported fresh (new module: plain import gets it).
"""
from __future__ import annotations

import contextlib
import threading
import time

_LOCK = threading.Lock()
_TRACKER = None

DEADBAND_FRAC = 0.08
HEAD_GAIN_DEG = 14.0          # deg per unit vertical error (frac of half-frame)
TURN_PULSE_MM_S = 42.0        # wheel speed during a horizontal correction pulse
TURN_PULSE_S = 0.11
LOOP_HZ = 6.0
TARGET_LOST_OFF_S = 25.0
MAX_RUN_S = 600.0
FACE_FRESH_S = 0.8            # face event older than this = no target


def _session():
    from brain import vector_session
    s = vector_session.get_session(create=False)
    if s is None or not getattr(s, "connected", False) or s.robot is None:
        return None
    return s


class Tracker:
    def __init__(self):
        self.mode = "off"
        self.thread: threading.Thread | None = None
        self.stop_evt = threading.Event()
        self.started = 0.0
        self.last_target_ts = 0.0
        self.corrections = 0
        self.last_err = (0.0, 0.0)
        self.note = ""
        self._face_rect = None            # (cx_frac, cy_frac, ts)
        self._face_sub = False
        self._prev_gray = None

    # ------------------------------------------------------------ face mode
    def _on_face(self, robot, event_type, event, done=None):
        """RobotObservedFace handler — engine gives img_rect in camera coords
        (640x360 stream). Store the face center as frame fractions."""
        with contextlib.suppress(Exception):
            r = event.img_rect
            cx = (r.x_top_left + r.width / 2.0) / 640.0
            cy = (r.y_top_left + r.height / 2.0) / 360.0
            self._face_rect = (cx, cy, time.time())

    def _face_target(self, s):
        if not self._face_sub:
            with contextlib.suppress(Exception):
                s.robot.vision.enable_face_detection(detect_faces=True)
            with contextlib.suppress(Exception):
                from anki_vector.events import Events
                s.robot.events.subscribe(self._on_face,
                                         Events.robot_observed_face)
                self._face_sub = True
        fr = self._face_rect
        if fr and time.time() - fr[2] <= FACE_FRESH_S:
            return fr[0], fr[1]
        return None

    # ---------------------------------------------------------- motion mode
    def _motion_target(self, s):
        """Frame-diff centroid of the largest moving blob (PC-side, cheap)."""
        try:
            import cv2
            import numpy as np
            img = s.robot.camera.latest_image
            if img is None:
                return None
            arr = np.array(img.raw_image.convert("L").resize((320, 180)))
            prev, self._prev_gray = self._prev_gray, arr
            if prev is None:
                return None
            diff = cv2.absdiff(arr, prev)
            diff = cv2.GaussianBlur(diff, (9, 9), 0)
            _, th = cv2.threshold(diff, 18, 255, cv2.THRESH_BINARY)
            th = cv2.dilate(th, None, iterations=2)
            cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
            if not cnts:
                return None
            big = max(cnts, key=cv2.contourArea)
            if cv2.contourArea(big) < 140:        # ignore sensor noise
                return None
            m = cv2.moments(big)
            if m["m00"] == 0:
                return None
            return (m["m10"] / m["m00"]) / 320.0, (m["m01"] / m["m00"]) / 180.0
        except Exception:
            return None

    # ------------------------------------------------------------ main loop
    def _loop(self):
        period = 1.0 / LOOP_HZ
        self.last_target_ts = time.time()
        while not self.stop_evt.is_set():
            t0 = time.time()
            if t0 - self.started > MAX_RUN_S:
                self.note = "auto-off: max run time"
                break
            if t0 - self.last_target_ts > TARGET_LOST_OFF_S:
                self.note = "auto-off: target lost too long"
                break
            s = _session()
            if s is None:
                self.note = "auto-off: body session closed"
                break
            st = dict(s._latest or {})
            if st.get("cliff") or st.get("picked_up"):
                time.sleep(period)
                continue
            target = (self._face_target(s) if self.mode == "face"
                      else self._motion_target(s))
            if target is not None:
                self.last_target_ts = t0
                ex = target[0] - 0.5          # + = target right of center
                ey = target[1] - 0.5          # + = target below center
                self.last_err = (round(ex, 3), round(ey, 3))
                moved = False
                if abs(ey) > DEADBAND_FRAC:
                    with contextlib.suppress(Exception):
                        cur = float(st.get("head_deg") or 0.0)
                        # camera y grows downward; target low => head down
                        s.head(max(-22.0, min(45.0,
                                              cur - ey * HEAD_GAIN_DEG)))
                        moved = True
                if (abs(ex) > DEADBAND_FRAC and not st.get("on_charger")):
                    with contextlib.suppress(Exception):
                        v = TURN_PULSE_MM_S * (1.0 if ex > 0 else -1.0)
                        s._raw_wheels(v, -v)      # + = clockwise/right
                        time.sleep(TURN_PULSE_S)
                        s._raw_wheels(0.0, 0.0)
                        s._wheels = (0.0, 0.0)
                        moved = True
                if moved:
                    self.corrections += 1
            dt = time.time() - t0
            if dt < period:
                self.stop_evt.wait(period - dt)
        # clean exit
        with contextlib.suppress(Exception):
            s = _session()
            if s is not None:
                s._raw_wheels(0.0, 0.0)
                s._wheels = (0.0, 0.0)
        self.mode = "off"

    # --------------------------------------------------------------- control
    def start(self, mode: str) -> dict:
        s = _session()
        if s is None:
            return {"ok": False, "error": "no body session (body_open first)"}
        self.stop()
        self.mode = mode
        self.stop_evt.clear()
        self.started = time.time()
        self.corrections = 0
        self.note = ""
        self._prev_gray = None
        self._face_rect = None
        self.thread = threading.Thread(target=self._loop,
                                       name=f"body-track-{mode}", daemon=True)
        self.thread.start()
        docked = bool(dict(s._latest or {}).get("on_charger"))
        return {"ok": True, "tracking": mode,
                "docked_head_only": docked,
                "auto_off": {"target_lost_s": TARGET_LOST_OFF_S,
                             "max_run_s": MAX_RUN_S}}

    def stop(self) -> dict:
        was = self.mode
        self.stop_evt.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        self.mode = "off"
        return {"ok": True, "stopped": was}

    def status(self) -> dict:
        return {"ok": True, "mode": self.mode,
                "running_s": round(time.time() - self.started, 1)
                if self.mode != "off" else 0.0,
                "corrections": self.corrections,
                "last_err": self.last_err,
                "note": self.note}


def get_tracker() -> Tracker:
    global _TRACKER
    with _LOCK:
        if _TRACKER is None:
            _TRACKER = Tracker()
        return _TRACKER
