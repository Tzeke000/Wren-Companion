"""
Camera annotator — overlays face bounding boxes, landmarks, head pose axes,
attention state, and labels on the live frame.

Reads g["_face_results"] (populated by background_ticks when InsightFace
is active) and g["_attention_state"] (populated by heartbeat eye tracker).

If face_results is empty the function is a no-op so plain video keeps
flowing through the pipeline.
"""
from __future__ import annotations

import math
from typing import Any


_BOX_KNOWN = (0, 220, 0)
_BOX_UNKNOWN = (0, 220, 220)
_LANDMARK_FAINT = (0, 180, 0)
_LANDMARK_KEY = (0, 255, 80)
_TEXT_AGE_GENDER = (200, 200, 200)

_KEY_LANDMARK_INDICES = {
    # Approximate key landmark indices from buffalo_l 106-pt set.
    # Drawn at radius 2 instead of 1.
    0, 1, 2, 3, 4,        # nose
    16, 17,                # eyebrow ridges
    33, 35, 40, 41,        # left eye
    87, 89, 94, 95,        # right eye
    52, 61, 67, 76, 77,    # mouth
}

_ATTENTION_COLORS = {
    "focused": (0, 220, 0),
    "distracted": (0, 220, 220),
    "away": (0, 165, 255),
    "absent": (0, 0, 220),
}


def annotate_frame(frame: Any, face_results: list[dict[str, Any]] | None, g: dict[str, Any]) -> Any:
    """Draw face overlays on `frame` and return the modified frame.

    Falls back to the original frame on any error so the camera pipeline
    never breaks.
    """
    if frame is None:
        return frame
    if not face_results:
        return _draw_attention_only(frame, g)

    try:
        import cv2  # type: ignore
    except Exception:
        return frame

    out = frame.copy()
    h = int(out.shape[0]) if hasattr(out, "shape") else 0

    profiles = g.get("_profiles") or {}

    for face in face_results:
        try:
            bbox = face.get("bbox") or [0, 0, 0, 0]
            x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
            pid = str(face.get("person_id") or "unknown")
            conf = float(face.get("confidence") or 0.0)

            color = _BOX_KNOWN if pid != "unknown" else _BOX_UNKNOWN
            cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)

            display = pid.upper()
            if isinstance(profiles, dict):
                p = profiles.get(pid)
                if isinstance(p, dict):
                    name = p.get("name") or p.get("display_name")
                    if isinstance(name, str) and name:
                        display = name
            label = f"{display} {conf*100:.0f}%"
            cv2.putText(
                out, label, (x1, max(15, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2,
            )

            age = face.get("age", 0)
            gender = face.get("gender", "?")
            ag = f"Age:{age} {gender}"
            cv2.putText(
                out, ag, (max(0, x2 - 90), max(15, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, _TEXT_AGE_GENDER, 1,
            )

            lm = face.get("landmarks")
            if lm is not None:
                for i, pt in enumerate(lm):
                    try:
                        lx, ly = int(pt[0]), int(pt[1])
                        if i in _KEY_LANDMARK_INDICES:
                            cv2.circle(out, (lx, ly), 2, _LANDMARK_KEY, -1)
                        else:
                            cv2.circle(out, (lx, ly), 1, _LANDMARK_FAINT, -1)
                    except Exception:
                        continue

                # Head pose arrows from nose tip (landmark 1 — closer to actual tip).
                try:
                    pose = face.get("pose") or [0.0, 0.0, 0.0]
                    pitch = float(pose[0])
                    yaw = float(pose[1])
                    roll = float(pose[2])
                    nose_idx = 1 if len(lm) > 1 else 0
                    nx, ny = int(lm[nose_idx][0]), int(lm[nose_idx][1])
                    L = 55
                    yaw_r = math.radians(yaw)
                    pitch_r = math.radians(pitch)
                    roll_r = math.radians(roll)
                    # Up axis (green)
                    cv2.arrowedLine(
                        out, (nx, ny),
                        (nx + int(L * math.sin(yaw_r)),
                         ny - int(L * math.cos(pitch_r))),
                        (0, 255, 0), 2, tipLength=0.3,
                    )
                    # Right axis (red)
                    cv2.arrowedLine(
                        out, (nx, ny),
                        (nx + int(L * math.cos(yaw_r)),
                         ny + int(L * math.sin(roll_r))),
                        (0, 0, 255), 2, tipLength=0.3,
                    )
                    # Forward axis (blue)
                    cv2.arrowedLine(
                        out, (nx, ny),
                        (nx - int(L * math.sin(roll_r)),
                         ny - int(L * math.sin(yaw_r))),
                        (255, 0, 0), 2, tipLength=0.3,
                    )
                except Exception:
                    pass
        except Exception as e:
            # Skip this face but keep going — robustness over completeness.
            print(f"[camera_annotator] face draw error: {e!r}")
            continue

    return _overlay_attention(out, h, g)


def _draw_attention_only(frame: Any, g: dict[str, Any]) -> Any:
    """When there are no faces, still surface attention/eye-tracking state."""
    try:
        import cv2  # type: ignore
    except Exception:
        return frame
    try:
        h = int(frame.shape[0]) if hasattr(frame, "shape") else 0
        out = frame.copy()
        return _overlay_attention(out, h, g)
    except Exception:
        return frame


def _overlay_attention(frame: Any, h: int, g: dict[str, Any]) -> Any:
    try:
        import cv2  # type: ignore
        # Default to "focused" so the bottom-left readout is always visible —
        # the eye tracker only updates this when a face is being tracked.
        attn = str(g.get("_attention_state") or "focused").strip().lower()
        color = _ATTENTION_COLORS.get(attn, (200, 200, 200))
        y = max(15, h - 10)
        cv2.putText(
            frame, attn.upper(),
            (10, y),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2,
        )
    except Exception:
        pass
    return frame
