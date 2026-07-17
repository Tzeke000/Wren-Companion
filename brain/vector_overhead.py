# SELF_ASSESSMENT: I am Iris's OVERHEAD EYE candidate — ArUco marker detection
# on the PC camera, aimed at absolute (x,y) localization for my Vector body.
"""vector_overhead.py — PC-camera overhead localization (2026-07-17 round 2).

Zeke (2026-07-17 ~04:00): "you already have a camera on the pc where you are —
it's how you see my face." If that camera's view covers the desk floor my body
drives on, it becomes the researched overhead-ArUco absolute-position fix
(±5–10mm) with ZERO new hardware. If it only sees faces, it needs re-aiming
(Zeke-present) or the $40 dedicated cam remains the fallback.

Stage 1 (this file, buildable solo): probe what the camera sees + detect ArUco
markers in the frame (cv2.aruco, DICT_4X4_50 — print IDs 0=dock, 1=my back).
Frame access rides brain.camera_live.read_live_frame — the SHARED device path
(never open a second VideoCapture and fight perception for the camera).

Stage 2 (owed, Zeke-present, after tags are printed): homography calibration
(dock tag = origin, tag size = scale) → marker px → desk mm → feed pose
corrections into the odometry the same way the charger-anchor plan describes.
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "state" / "vector"
PROBE_JPG = OUT_DIR / "overhead_probe.jpg"
CALIB_JSON = OUT_DIR / "overhead_calibration.json"

ARUCO_DICT = "DICT_4X4_50"


def _detector():
    import cv2
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICT))
    try:
        params = cv2.aruco.DetectorParameters()
        return ("new", cv2.aruco.ArucoDetector(d, params))
    except AttributeError:      # older cv2.aruco API
        return ("old", d)


def detect_markers(frame) -> list:
    """ArUco markers in a BGR frame -> [{id, center_px, corners_px}]."""
    import cv2
    mode, det = _detector()
    if mode == "new":
        corners, ids, _ = det.detectMarkers(frame)
    else:
        corners, ids, _ = cv2.aruco.detectMarkers(frame, det)
    out = []
    if ids is None:
        return out
    for mid, quad in zip(ids.flatten().tolist(), corners):
        pts = quad.reshape(-1, 2)
        out.append({
            "id": int(mid),
            "center_px": [round(float(pts[:, 0].mean()), 1),
                          round(float(pts[:, 1].mean()), 1)],
            "corners_px": [[round(float(x), 1), round(float(y), 1)]
                           for x, y in pts.tolist()],
        })
    return out


def probe(save: bool = True) -> dict:
    """Grab ONE frame from the PC camera (shared path — no device fight),
    save it for my own eyes, and report any ArUco markers + basic frame
    stats. This answers 'can the PC cam be my overhead eye?' — Read the
    saved jpg and look for the robot's driving area."""
    import cv2
    try:
        from brain.camera_live import read_live_frame
        frame, ts = read_live_frame()
    except Exception as e:
        return {"ok": False, "error": f"camera_live read failed: {e!r}"[:200]}
    if frame is None:
        return {"ok": False, "error": "no frame from PC camera (device busy/"
                                      "unavailable — perception may own it)"}
    age = round(time.time() - ts, 2) if ts else None
    h, w = frame.shape[:2]
    markers = []
    with contextlib.suppress(Exception):
        markers = detect_markers(frame)
    out = {"ok": True, "w": w, "h": h, "frame_age_s": age,
           "brightness": round(float(cv2.cvtColor(
               frame, cv2.COLOR_BGR2GRAY).mean()), 1),
           "markers": markers, "n_markers": len(markers),
           "dict": ARUCO_DICT,
           "hint": "Read the jpg — does the view cover the desk floor where "
                   "my body drives? Markers appear once Zeke prints the tags."}
    if save:
        with contextlib.suppress(Exception):
            vis = frame.copy()
            for m in markers:
                cx, cy = int(m["center_px"][0]), int(m["center_px"][1])
                cv2.circle(vis, (cx, cy), 8, (0, 220, 0), 2)
                cv2.putText(vis, str(m["id"]), (cx + 10, cy),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 220, 0), 2)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(PROBE_JPG), vis)
            out["frame"] = str(PROBE_JPG)
    return out


def to_desk_mm(center_px) -> dict:
    """px -> desk mm via the stored homography. Stage-2: returns not-calibrated
    until the tag-based calibration (Zeke-present) writes CALIB_JSON."""
    try:
        c = json.loads(CALIB_JSON.read_text(encoding="utf-8"))
        H = c["homography"]
    except Exception:
        return {"ok": False, "error": "not calibrated — needs printed tags + "
                                      "the stage-2 calibration pass"}
    x, y = float(center_px[0]), float(center_px[1])
    d = H[2][0] * x + H[2][1] * y + H[2][2]
    return {"ok": True,
            "x_mm": round((H[0][0] * x + H[0][1] * y + H[0][2]) / d, 1),
            "y_mm": round((H[1][0] * x + H[1][1] * y + H[1][2]) / d, 1)}
