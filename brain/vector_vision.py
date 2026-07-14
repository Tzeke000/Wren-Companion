"""vector_vision.py — object detection for the Vector body's camera feed.

Zeke 2026-07-14: "you need to be able to live see your body's feed as needed so
you have your body's object detection." The gap that made me strand: I could grab
frames but had no automatic sense of WHAT was in them — I leaned on Zeke to name
objects. This runs mediapipe EfficientDet-Lite2 (COCO classes, CPU, ~loaded once)
on a frame and returns labeled boxes + a coarse where/size hint for navigation.

Model: models/mediapipe/efficientdet_lite2.tflite (downloaded from Google's
mediapipe-models bucket). Detector is created ONCE and cached (tflite load is slow).
Frames from the dim, fisheye robot cam are brightened before detection.
"""
from __future__ import annotations

import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO / "models" / "mediapipe" / "efficientdet_lite2.tflite"

_detector = None
_lock = threading.Lock()


def _get_detector(score_threshold: float = 0.30, max_results: int = 20):
    global _detector
    with _lock:
        if _detector is None:
            import mediapipe as mp  # noqa
            from mediapipe.tasks import python as mpp
            from mediapipe.tasks.python import vision
            base = mpp.BaseOptions(model_asset_path=str(MODEL_PATH))
            _detector = vision.ObjectDetector.create_from_options(
                vision.ObjectDetectorOptions(
                    base_options=base, score_threshold=score_threshold,
                    max_results=max_results))
        return _detector


def detect_frame(bgr, brighten: bool = True) -> list[dict]:
    """Detect objects in a BGR (cv2) image. Returns list of
    {label, score, box:[x,y,w,h], where:left|center|right, band:near|mid|far,
    rel_size}, sorted by score. Never raises — [] on any failure."""
    try:
        import cv2
        import mediapipe as mp
        if bgr is None:
            return []
        img = cv2.convertScaleAbs(bgr, alpha=1.7, beta=25) if brighten else bgr
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        res = _get_detector().detect(
            mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        h, w = bgr.shape[:2]
        out = []
        for d in res.detections:
            c = d.categories[0]
            bb = d.bounding_box
            cx = bb.origin_x + bb.width / 2.0
            area = (bb.width * bb.height) / float(w * h)
            where = ("left" if cx < w / 3 else
                     "right" if cx > 2 * w / 3 else "center")
            # bigger box ~= closer (very rough, fisheye-dependent)
            band = "near" if area > 0.10 else "far" if area < 0.02 else "mid"
            out.append({
                "label": c.category_name, "score": round(float(c.score), 2),
                "box": [int(bb.origin_x), int(bb.origin_y),
                        int(bb.width), int(bb.height)],
                "where": where, "band": band, "rel_size": round(area, 3),
            })
        return sorted(out, key=lambda o: -o["score"])
    except Exception:
        return []
