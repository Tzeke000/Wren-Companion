"""
Eye tracking and gaze estimation using MediaPipe Face Landmarker (tasks API).
Falls back gracefully if mediapipe not available.

2026-08-27 PORTED off the legacy `mp.solutions.face_mesh` API — the venv's
mediapipe (0.10.35, installed fork day 2026-05-09) only ships the new `tasks`
API, so this module had silently init-failed since day one (`available=False`,
every consumer took its fallback branch). Now uses FaceLandmarker VIDEO mode
with the model at models/mediapipe/face_landmarker.task; that model always
outputs 478 landmarks, so the iris indices (468-477) work unchanged.
`FaceMeshVideo` below is shared with expression_detector.py.

Calibration: 9-point tkinter overlay → maps iris coords to screen pixels.
Bootstrap: Ava decides what to do with gaze data. She develops her own sense
of when to mention it, when to stay quiet, when it's relevant.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

_MEDIAPIPE_OK = False
try:
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision
    _MEDIAPIPE_OK = True
except ImportError:
    pass

_MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "mediapipe" / "face_landmarker.task"


class FaceMeshVideo:
    """Thin thread-safe wrapper: BGR frame in → 478-landmark list out (or None).

    FaceLandmarker VIDEO mode needs strictly-increasing timestamps (same trick
    as iris_hands) and is not documented thread-safe, so detect() is locked.
    Each consumer owns its own instance (model is 3.7MB — cheap)."""

    def __init__(self) -> None:
        opts = mp_vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(_MODEL_PATH)),
            running_mode=mp_vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._lm = mp_vision.FaceLandmarker.create_from_options(opts)
        self._lock = threading.Lock()
        self._last_ts_ms = 0

    def detect(self, frame_bgr: Any) -> Optional[list]:
        """Return the landmark list (NormalizedLandmark with .x/.y/.z) or None."""
        if frame_bgr is None:
            return None
        import cv2
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB) if len(frame_bgr.shape) == 3 else frame_bgr
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        with self._lock:
            ts_ms = int(time.time() * 1000)
            if ts_ms <= self._last_ts_ms:
                ts_ms = self._last_ts_ms + 1
            self._last_ts_ms = ts_ms
            res = self._lm.detect_for_video(mp_img, ts_ms)
        if not res.face_landmarks:
            return None
        return res.face_landmarks[0]

# MediaPipe Face Mesh iris landmark indices (refine_landmarks=True required)
# Left iris:  468 (center), 469, 470, 471, 472 (edge points)
# Right iris: 473 (center), 474, 475, 476, 477 (edge points)
_LEFT_IRIS  = [468, 469, 470, 471, 472]
_RIGHT_IRIS = [473, 474, 475, 476, 477]
_LEFT_EYE_OUTER = [33, 133]
_RIGHT_EYE_OUTER = [362, 263]

_CALIB_PATH = "state/gaze_calibration.json"

# 3x3 grid region names
_REGIONS_3X3 = [
    ["top_left",    "top_center",    "top_right"],
    ["mid_left",    "center",        "mid_right"],
    ["bottom_left", "bottom_center", "bottom_right"],
]


class EyeTracker:
    def __init__(self, base_dir: Path):
        self._base_dir = base_dir
        self._face_mesh: Optional[FaceMeshVideo] = None
        self._calib: Optional[dict[str, Any]] = None
        self._available = False
        self._attention_history: deque[str] = deque(maxlen=60)  # last 60 checks
        self._last_face_ts: float = 0.0
        self._away_since: float = 0.0
        self._looking_away_since: float = 0.0
        self._init_mediapipe()
        self._load_calibration()

    def _init_mediapipe(self) -> None:
        if not _MEDIAPIPE_OK:
            return
        if not _MODEL_PATH.is_file():
            print(f"[eye_tracker] model missing: {_MODEL_PATH}")
            return
        try:
            self._face_mesh = FaceMeshVideo()
            self._available = True
        except Exception as e:
            print(f"[eye_tracker] FaceLandmarker init failed: {e}")

    @property
    def available(self) -> bool:
        return self._available

    @property
    def calibrated(self) -> bool:
        return self._calib is not None

    # ── Calibration ───────────────────────────────────────────────────────────

    def calibrate(self, screen_w: int = 1920, screen_h: int = 1080) -> bool:
        """
        9-point tkinter calibration. Returns True on success.
        Shows red dot at each point; user looks at it for 3s while camera captures iris position.
        """
        if not self._available:
            print("[eye_tracker] calibrate: MediaPipe not available")
            return False
        try:
            import tkinter as tk
            import cv2
            import numpy as np
        except ImportError as e:
            print(f"[eye_tracker] calibrate: dependency missing: {e}")
            return False

        targets_norm = [
            (0.1, 0.1), (0.5, 0.1), (0.9, 0.1),
            (0.1, 0.5), (0.5, 0.5), (0.9, 0.5),
            (0.1, 0.9), (0.5, 0.9), (0.9, 0.9),
        ]

        screen_pts: list[tuple[float, float]] = []
        iris_pts: list[tuple[float, float]] = []

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("[eye_tracker] calibrate: camera not available")
            return False

        root = tk.Tk()
        root.attributes("-fullscreen", True)
        root.attributes("-topmost", True)
        root.configure(bg="black")
        canvas = tk.Canvas(root, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()

        result_container: list[bool] = [False]

        def run_calibration():
            for nx, ny in targets_norm:
                px, py = int(nx * sw), int(ny * sh)
                canvas.delete("all")
                canvas.create_oval(px - 20, py - 20, px + 20, py + 20, fill="red", outline="white", width=2)
                canvas.create_text(px, py - 35, text="Look here", fill="white", font=("Helvetica", 14))
                canvas.update()
                time.sleep(0.5)  # settle

                frames_iris: list[tuple[float, float]] = []
                t0 = time.time()
                while time.time() - t0 < 3.0:
                    ok, frame = cap.read()
                    if not ok:
                        continue
                    iris = self._get_iris_position(frame)
                    if iris is not None:
                        frames_iris.append(iris)
                    time.sleep(0.05)

                if len(frames_iris) >= 5:
                    avg_x = sum(f[0] for f in frames_iris) / len(frames_iris)
                    avg_y = sum(f[1] for f in frames_iris) / len(frames_iris)
                    screen_pts.append((float(px), float(py)))
                    iris_pts.append((avg_x, avg_y))

            root.after(0, root.destroy)
            result_container[0] = len(screen_pts) >= 6

        t = threading.Thread(target=run_calibration, daemon=True)
        t.start()
        root.mainloop()
        cap.release()
        t.join(timeout=35.0)

        if not result_container[0] or len(iris_pts) < 6:
            print(f"[eye_tracker] calibrate: insufficient samples ({len(iris_pts)})")
            return False

        # Fit simple linear mapping: iris_x → screen_x, iris_y → screen_y
        ix = [p[0] for p in iris_pts]
        iy = [p[1] for p in iris_pts]
        sx = [p[0] for p in screen_pts]
        sy = [p[1] for p in screen_pts]

        # Linear regression coefficients
        def linreg(xs: list, ys: list) -> tuple[float, float]:
            n = len(xs)
            mx, my = sum(xs) / n, sum(ys) / n
            num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
            den = sum((xs[i] - mx) ** 2 for i in range(n)) + 1e-9
            slope = num / den
            intercept = my - slope * mx
            return slope, intercept

        sx_slope, sx_inter = linreg(ix, sx)
        sy_slope, sy_inter = linreg(iy, sy)

        self._calib = {
            "screen_w": sw, "screen_h": sh,
            "sx_slope": sx_slope, "sx_inter": sx_inter,
            "sy_slope": sy_slope, "sy_inter": sy_inter,
            "calibrated_at": time.time(),
            "sample_count": len(iris_pts),
        }
        path = self._base_dir / _CALIB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._calib, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[eye_tracker] calibration saved ({len(iris_pts)} samples)")
        return True

    def _load_calibration(self) -> None:
        path = self._base_dir / _CALIB_PATH
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Validate required keys before accepting
            required = ("sx_slope", "sx_inter", "sy_slope", "sy_inter")
            if isinstance(data, dict) and all(k in data for k in required):
                self._calib = data
            else:
                print("[eye_tracker] calibration file missing required keys; ignoring")
        except Exception as e:
            print(f"[eye_tracker] calibration load failed: {e}")

    # ── Iris extraction ───────────────────────────────────────────────────────

    def _get_iris_position(self, frame: Any) -> Optional[tuple[float, float]]:
        """Extract normalized iris center from frame. Returns (x, y) in [0,1] or None."""
        if not self._available or self._face_mesh is None or frame is None:
            return None
        try:
            lm = self._face_mesh.detect(frame)
            if lm is None:
                return None
            # Average left and right iris centers
            points: list[tuple[float, float]] = []
            for idx in _LEFT_IRIS + _RIGHT_IRIS:
                if idx < len(lm):
                    points.append((lm[idx].x, lm[idx].y))
            if not points:
                return None
            cx = sum(p[0] for p in points) / len(points)
            cy = sum(p[1] for p in points) / len(points)
            self._last_face_ts = time.time()
            return (cx, cy)
        except Exception:
            return None

    def get_face_landmarks(self, frame: Any) -> Optional[Any]:
        """Return the raw landmark list (478 NormalizedLandmarks) or None."""
        if not self._available or self._face_mesh is None or frame is None:
            return None
        try:
            lm = self._face_mesh.detect(frame)
            if lm is not None:
                self._last_face_ts = time.time()
            return lm
        except Exception:
            return None

    # ── Gaze estimation ───────────────────────────────────────────────────────

    def get_gaze_point(self, frame: Any) -> Optional[tuple[int, int]]:
        """Map iris position to screen pixel coordinates. Returns None if not calibrated."""
        if self._calib is None:
            return None
        iris = self._get_iris_position(frame)
        if iris is None:
            return None
        x_screen = self._calib["sx_slope"] * iris[0] + self._calib["sx_inter"]
        y_screen = self._calib["sy_slope"] * iris[1] + self._calib["sy_inter"]
        sw = int(self._calib.get("screen_w") or 1920)
        sh = int(self._calib.get("screen_h") or 1080)
        x_screen = max(0, min(sw, int(x_screen)))
        y_screen = max(0, min(sh, int(y_screen)))
        return (x_screen, y_screen)

    def get_gaze_region(self, frame: Any, screen_w: int = 1920, screen_h: int = 1080) -> str:
        """Return one of 9 region names or 'off_screen' / 'unknown'."""
        pt = self.get_gaze_point(frame)
        if pt is None:
            iris = self._get_iris_position(frame)
            if iris is None:
                return "unknown"
            # Use raw iris position as proxy if not calibrated
            col = min(2, int(iris[0] * 3))
            row = min(2, int(iris[1] * 3))
            return _REGIONS_3X3[row][col]
        x, y = pt
        if x < 0 or x > screen_w or y < 0 or y > screen_h:
            return "off_screen"
        col = min(2, int((x / screen_w) * 3))
        row = min(2, int((y / screen_h) * 3))
        return _REGIONS_3X3[row][col]

    def is_looking_at_screen(self, frame: Any) -> bool:
        region = self.get_gaze_region(frame)
        return region not in ("off_screen", "unknown")

    def get_attention_state(self, frame: Any) -> str:
        """'focused' | 'distracted' | 'away' | 'absent'"""
        now = time.time()
        iris = self._get_iris_position(frame)

        if iris is None:
            # No face
            if now - self._last_face_ts > 30.0:
                self._attention_history.append("absent")
                return "absent"
            self._attention_history.append("away")
            return "away"

        looking = self.is_looking_at_screen(frame)
        if looking:
            self._looking_away_since = 0.0
            self._attention_history.append("focused")
            return "focused"
        else:
            if self._looking_away_since == 0.0:
                self._looking_away_since = now
            away_sec = now - self._looking_away_since
            if away_sec > 10.0:
                self._attention_history.append("away")
                return "away"
            self._attention_history.append("distracted")
            return "distracted"


# ── Module singleton ──────────────────────────────────────────────────────────

_SINGLETON: Optional[EyeTracker] = None


def get_eye_tracker(base_dir: Optional[Path] = None) -> Optional[EyeTracker]:
    global _SINGLETON
    if _SINGLETON is None and base_dir is not None:
        _SINGLETON = EyeTracker(base_dir)
    return _SINGLETON


def bootstrap_eye_tracker(g: dict[str, Any]) -> Optional[EyeTracker]:
    global _SINGLETON
    base = Path(g.get("BASE_DIR") or ".")
    et = EyeTracker(base)
    _SINGLETON = et
    g["_eye_tracker"] = et
    state = "available" if et.available else "unavailable (mediapipe/model missing)"
    calib = "calibrated" if et.calibrated else "not calibrated"
    print(f"[eye_tracker] {state}, {calib}")
    return et
