# SELF_ASSESSMENT: I am Iris's hand understanding — MediaPipe GestureRecognizer
# (Tasks API, CPU, zero VRAM) riding the existing camera loop as a person-gated
# step. I turn hand pixels into gestures (wave/thumbs-up/point/etc.) and fire
# signal-bus events on gesture TRANSITIONS, never the firehose.
"""
iris_hands — hand tracking + gesture understanding (Zeke directive 2026-07-13).

"Then you can really understand when people wave at you and make symbols or
they're holding things... if it's too many programs that take up too much VRAM
then we need a smart on/off switch."

Design (researched 2026-07-13, filed in hands_and_vector_research_2026-07-13.md):
  - MediaPipe GestureRecognizer, VIDEO running mode: 21 landmarks/hand + canned
    gesture classes [Closed_Fist, Open_Palm, Pointing_Up, Thumb_Down, Thumb_Up,
    Victory, ILoveYou] out of the box. CPU delegate — ZERO VRAM (the 3060 stays
    Wren-and-whisper's). Model: state/hands/gesture_recognizer.task (8.4 MB).
  - WAVE is not a canned class: detected here as Open_Palm + wrist-x oscillation
    (>=2 direction flips within the last ~1.5 s of hand history).
  - Smart on/off (three gates, cheapest first):
      1. g["_hands_enabled"] flag (hands_rest tool — mirrors eyes_rest)
      2. person-present gate: only runs when the face step currently sees >=1
         face (no face, no hands — skips ALL hand compute while the room is empty)
      3. lazy singleton: the model isn't even loaded until the first gated frame
  - Events (signal bus, TRANSITION-fired like the face events):
      gesture_detected {gesture, handedness, score}   — new steady gesture
      hand_wave       {handedness}                    — wave heuristic fired
      gesture_cleared {prior}                         — hands left / gesture ended
  - State for consumers: g["_hand_results"] = {"ts", "hands": [{handedness,
    gesture, score, wrist_xy, landmarks_px}], "wave": bool}

Called from the iris_runtime camera loop (hands_every_n cadence) — this module
NEVER touches the camera itself (video loop stays sole cv2 owner, 2026-07-08 rule).
"""
from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = _ROOT / "state" / "hands" / "gesture_recognizer.task"

_RECOGNIZER = None          # lazy singleton
_LOAD_FAILED = ""           # non-empty = permanent-ish failure, stop retrying
_LAST_TS_MS = 0             # VIDEO mode requires monotonically increasing ts

# Wave heuristic: rolling wrist-x history per handedness.
_WRIST_HIST: dict = {"Left": deque(maxlen=24), "Right": deque(maxlen=24)}
_WAVE_WINDOW_S = 1.6
_WAVE_MIN_FLIPS = 2          # direction changes required
_WAVE_MIN_TRAVEL = 0.04      # normalized x-travel per swing (filters micro-jitter)

# Transition state (so the camera loop can fire signal events on CHANGE only).
_LAST_STEADY: dict = {"gesture": None, "since": 0.0}
_STEADY_MIN_S = 0.5          # a gesture must hold this long to count as steady
_PENDING: dict = {"gesture": None, "since": 0.0}


def available() -> bool:
    return MODEL_PATH.exists() and not _LOAD_FAILED


def load_error() -> str:
    return _LOAD_FAILED


def _get_recognizer():
    """Lazy-load the GestureRecognizer (VIDEO mode, 2 hands, CPU)."""
    global _RECOGNIZER, _LOAD_FAILED
    if _RECOGNIZER is not None:
        return _RECOGNIZER
    if _LOAD_FAILED:
        return None
    try:
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision
        base = mp_python.BaseOptions(model_asset_path=str(MODEL_PATH))
        opts = mp_vision.GestureRecognizerOptions(
            base_options=base,
            running_mode=mp_vision.RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        _RECOGNIZER = mp_vision.GestureRecognizer.create_from_options(opts)
        print("[iris_hands] gesture recognizer loaded (CPU, 2 hands, VIDEO mode)",
              file=sys.stderr, flush=True)
        return _RECOGNIZER
    except Exception as e:
        _LOAD_FAILED = repr(e)
        print(f"[iris_hands] LOAD FAILED (hands disabled): {e!r}", file=sys.stderr, flush=True)
        return None


def _detect_wave(handedness: str, wrist_x: float, now: float) -> bool:
    """Open-palm wrist-x oscillation: >=_WAVE_MIN_FLIPS direction reversals with
    real travel inside _WAVE_WINDOW_S. History is (ts, x) per handedness."""
    hist = _WRIST_HIST[handedness]
    hist.append((now, wrist_x))
    pts = [(t, x) for (t, x) in hist if now - t <= _WAVE_WINDOW_S]
    if len(pts) < 6:
        return False
    flips = 0
    travel = 0.0
    direction = 0
    last_x = pts[0][1]
    swing = 0.0
    for _, x in pts[1:]:
        dx = x - last_x
        last_x = x
        if abs(dx) < 1e-4:
            continue
        d = 1 if dx > 0 else -1
        swing += abs(dx)
        if direction == 0:
            direction = d
        elif d != direction:
            if swing >= _WAVE_MIN_TRAVEL:
                flips += 1
                travel += swing
            swing = 0.0
            direction = d
    return flips >= _WAVE_MIN_FLIPS


def process_frame(frame_bgr, g: dict) -> "dict | None":
    """Run one gated hands pass. Returns the result dict it stored on
    g['_hand_results'], or None if skipped/unavailable. Fires signal-bus
    events on gesture transitions. Caller (camera loop) handles cadence and
    the person-present gate; this function re-checks the enable flag only."""
    global _LAST_TS_MS
    if not g.get("_hands_enabled", True):
        return None
    rec = _get_recognizer()
    if rec is None:
        return None
    try:
        import cv2
        import mediapipe as mp
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        ts_ms = int(time.time() * 1000)
        if ts_ms <= _LAST_TS_MS:            # VIDEO mode: strictly increasing
            ts_ms = _LAST_TS_MS + 1
        _LAST_TS_MS = ts_ms
        res = rec.recognize_for_video(mp_img, ts_ms)
    except Exception as e:
        print(f"[iris_hands] recognize error (non-fatal): {e!r}", file=sys.stderr, flush=True)
        return None

    now = time.time()
    h, w = frame_bgr.shape[:2]
    hands = []
    wave = False
    top_gesture = None
    for i, lm_list in enumerate(res.hand_landmarks or []):
        handedness = "Right"
        try:
            handedness = res.handedness[i][0].category_name
        except Exception:
            pass
        gesture, score = "None", 0.0
        try:
            gcat = res.gestures[i][0]
            gesture, score = gcat.category_name, float(gcat.score)
        except Exception:
            pass
        wrist = lm_list[0]
        if gesture == "Open_Palm" and _detect_wave(handedness, float(wrist.x), now):
            wave = True
        hands.append({
            "handedness": handedness,
            "gesture": gesture,
            "score": round(score, 3),
            "wrist_xy": [int(wrist.x * w), int(wrist.y * h)],
            "landmarks_px": [[int(p.x * w), int(p.y * h)] for p in lm_list],
        })
        if gesture not in ("None", "") and (top_gesture is None or score > top_gesture[1]):
            top_gesture = (gesture, score, handedness)

    out = {"ts": now, "hands": hands, "wave": wave}
    g["_hand_results"] = out

    # ── Transition events (steady-state debounced) ───────────────────────────
    bus = g.get("_signal_bus")
    cur = ("wave" if wave else (top_gesture[0] if top_gesture else None))
    if cur != _PENDING["gesture"]:
        _PENDING["gesture"] = cur
        _PENDING["since"] = now
    steady_long_enough = (now - _PENDING["since"]) >= (0.0 if cur == "wave" else _STEADY_MIN_S)
    if steady_long_enough and cur != _LAST_STEADY["gesture"]:
        prior = _LAST_STEADY["gesture"]
        _LAST_STEADY["gesture"] = cur
        _LAST_STEADY["since"] = now
        try:
            if bus is not None:
                if cur == "wave":
                    hd = next((x["handedness"] for x in hands if x["gesture"] == "Open_Palm"), "?")
                    bus.fire("hand_wave", data={"handedness": hd}, priority="high")
                elif cur is not None:
                    sc, hd = 0.0, "?"
                    if top_gesture:
                        _, sc, hd = top_gesture[0], top_gesture[1], top_gesture[2]
                    bus.fire("gesture_detected",
                             data={"gesture": cur, "handedness": hd, "score": round(float(sc), 3)},
                             priority="medium")
                elif prior is not None:
                    bus.fire("gesture_cleared", data={"prior": prior}, priority="low")
        except Exception:
            pass
    return out
