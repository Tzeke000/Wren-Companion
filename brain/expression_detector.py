"""
Facial expression detection using MediaPipe Face Mesh landmark geometry.
No ML model inference — pure geometric ratio calculations on 468 landmarks.
Falls back gracefully if mediapipe not available.
"""
from __future__ import annotations

import math
from typing import Any, Optional

_MEDIAPIPE_OK = False
try:
    import mediapipe as mp
    _MEDIAPIPE_OK = True
except ImportError:
    pass

# Key landmark indices from MediaPipe Face Mesh
# Mouth
_MOUTH_LEFT   = 61
_MOUTH_RIGHT  = 291
_MOUTH_TOP    = 13
_MOUTH_BOTTOM = 14
_MOUTH_TOP2   = 0
_MOUTH_BOT2   = 17
# Lips corners relative to center
_LIP_LEFT_CORNER  = 61
_LIP_RIGHT_CORNER = 291
_LIP_CENTER_TOP   = 0
_LIP_CENTER_BOT   = 17

# Eyebrows
_L_BROW_INNER = 55
_L_BROW_OUTER = 70
_L_BROW_MID   = 105
_R_BROW_INNER = 285
_R_BROW_OUTER = 300
_R_BROW_MID   = 334

# Eyes
_L_EYE_TOP    = 159
_L_EYE_BOTTOM = 145
_L_EYE_LEFT   = 33
_L_EYE_RIGHT  = 133
_R_EYE_TOP    = 386
_R_EYE_BOTTOM = 374
_R_EYE_LEFT   = 362
_R_EYE_RIGHT  = 263

# Nose / face reference
_NOSE_TIP  = 1
_CHIN      = 152
_FOREHEAD  = 10


def _dist(a: Any, b: Any) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


class ExpressionDetector:
    def __init__(self):
        self._face_mesh = None
        self._available = False
        self._last_expr: dict[str, Any] = {}
        self._init()

    def _init(self) -> None:
        if not _MEDIAPIPE_OK:
            return
        try:
            self._face_mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=1,
                refine_landmarks=False,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self._available = True
        except Exception as e:
            print(f"[expression_detector] MediaPipe init failed: {e}")

    @property
    def available(self) -> bool:
        return self._available

    def detect_expression(self, frame: Any) -> dict[str, Any]:
        """
        Returns dict of expression intensities 0-1 plus dominant label.
        Falls back to neutral if no face or not available.
        """
        empty = {
            "smile": 0.0, "frown": 0.0, "raised_eyebrows": 0.0,
            "furrowed_brows": 0.0, "wide_eyes": 0.0, "squinting": 0.0,
            "mouth_open": 0.0, "dominant": "neutral",
        }
        if not self._available or frame is None:
            return empty
        try:
            import cv2
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if len(frame.shape) == 3 else frame
            result = self._face_mesh.process(rgb)
            if not result.multi_face_landmarks:
                return empty
            lm = result.multi_face_landmarks[0].landmark
        except Exception:
            return empty

        try:
            # Face height reference (forehead → chin)
            face_h = _dist(lm[_FOREHEAD], lm[_CHIN]) + 1e-6

            # --- Smile detection ---
            # Ratio: horizontal mouth width / vertical lip opening
            mouth_w = _dist(lm[_MOUTH_LEFT], lm[_MOUTH_RIGHT])
            mouth_h = _dist(lm[_MOUTH_TOP], lm[_MOUTH_BOTTOM])
            # Smile: wide mouth relative to face width, corners pulled up
            lip_left_y  = lm[_LIP_LEFT_CORNER].y
            lip_right_y = lm[_LIP_RIGHT_CORNER].y
            lip_center_y = (lm[_LIP_CENTER_TOP].y + lm[_LIP_CENTER_BOT].y) / 2
            corner_lift = lip_center_y - (lip_left_y + lip_right_y) / 2
            smile_raw = _clamp01((corner_lift / (face_h * 0.02) + mouth_w / (face_h * 0.4)) / 2)

            # --- Frown detection ---
            corner_drop = (lip_left_y + lip_right_y) / 2 - lip_center_y
            frown_raw = _clamp01(corner_drop / (face_h * 0.025))

            # --- Mouth open ---
            mouth_open_raw = _clamp01((mouth_h / face_h) / 0.06)

            # --- Eye openness ---
            l_eye_h = _dist(lm[_L_EYE_TOP], lm[_L_EYE_BOTTOM])
            r_eye_h = _dist(lm[_R_EYE_TOP], lm[_R_EYE_BOTTOM])
            l_eye_w = _dist(lm[_L_EYE_LEFT], lm[_L_EYE_RIGHT])
            r_eye_w = _dist(lm[_R_EYE_LEFT], lm[_R_EYE_RIGHT])
            l_ear = (l_eye_h / (l_eye_w + 1e-6))
            r_ear = (r_eye_h / (r_eye_w + 1e-6))
            avg_ear = (l_ear + r_ear) / 2
            # EAR normal ~0.3, wide >0.35, squint <0.2
            wide_eyes_raw  = _clamp01((avg_ear - 0.30) / 0.12)
            squinting_raw  = _clamp01((0.25 - avg_ear) / 0.08)

            # --- Brow positions ---
            l_brow_h  = abs(lm[_L_BROW_MID].y - lm[_L_EYE_TOP].y)
            r_brow_h  = abs(lm[_R_BROW_MID].y - lm[_R_EYE_TOP].y)
            avg_brow_h = (l_brow_h + r_brow_h) / 2
            # Raised brows: brow farther from eye
            raised_raw = _clamp01((avg_brow_h / face_h - 0.04) / 0.04)
            # Furrowed: brows close together horizontally
            brow_sep = _dist(lm[_L_BROW_INNER], lm[_R_BROW_INNER])
            mouth_sep = _dist(lm[_MOUTH_LEFT], lm[_MOUTH_RIGHT])
            furrow_raw = _clamp01(1.0 - (brow_sep / (mouth_sep + 1e-6)))

            result_dict = {
                "smile": round(smile_raw, 3),
                "frown": round(frown_raw, 3),
                "raised_eyebrows": round(raised_raw, 3),
                "furrowed_brows": round(furrow_raw, 3),
                "wide_eyes": round(wide_eyes_raw, 3),
                "squinting": round(squinting_raw, 3),
                "mouth_open": round(mouth_open_raw, 3),
                "dominant": self._dominant({
                    "smile": smile_raw, "frown": frown_raw,
                    "raised_eyebrows": raised_raw, "furrowed_brows": furrow_raw,
                    "wide_eyes": wide_eyes_raw, "squinting": squinting_raw,
                    "mouth_open": mouth_open_raw,
                }),
            }
            self._last_expr = result_dict
            return result_dict
        except Exception as e:
            return empty

    def _dominant(self, scores: dict[str, float]) -> str:
        best = max(scores.items(), key=lambda kv: kv[1])
        if best[1] < 0.2:
            return "neutral"
        mapping = {
            "smile": "smiling",
            "frown": "frowning",
            "raised_eyebrows": "surprised",
            "furrowed_brows": "concentrating",
            "wide_eyes": "surprised",
            "squinting": "squinting",
            "mouth_open": "surprised",
        }
        return mapping.get(best[0], "neutral")

    def get_expression_summary(self) -> str:
        """Natural language summary of last detected expression."""
        e = self._last_expr
        if not e:
            return "neutral"
        return str(e.get("dominant") or "neutral")


# ── Module singleton ──────────────────────────────────────────────────────────

_SINGLETON: Optional[ExpressionDetector] = None


def get_expression_detector() -> Optional[ExpressionDetector]:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = ExpressionDetector()
    return _SINGLETON


def bootstrap_expression_detector(g: dict[str, Any]) -> Optional[ExpressionDetector]:
    global _SINGLETON
    det = ExpressionDetector()
    _SINGLETON = det
    g["_expression_detector"] = det
    state = "available" if det.available else "unavailable (mediapipe missing)"
    print(f"[expression_detector] {state}")
    return det
