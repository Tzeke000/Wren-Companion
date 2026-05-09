"""
Per-person expression calibrator.

Some people have naturally elevated eyebrows. Without calibration the generic
"surprise" detector reads them as constantly surprised. This module learns
each person's facial geometry baseline (slow EMA over ~1000 frames) and then
detects expression as deviation from THAT baseline rather than a global one.

State: state/expression_baseline_{person_id}.json
Updates: every frame with a recognized person (alpha=0.001).
Calibration threshold: 300 samples → calibrated=True.

Bootstrap-friendly: thresholds are deviations from the user's own neutral,
not pre-baked "this is what surprise looks like."
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional


_ALPHA = 0.001                 # EMA learning rate per frame
_MIN_SAMPLES_CALIBRATED = 300  # frames before we trust the baseline
_DEVIATION_SURPRISED = 0.06    # eyebrow_ratio above baseline → surprise
_DEVIATION_FROWNING = -0.04    # eyebrow_ratio below baseline → frown
_MOUTH_SMILE_PX = 4            # corner-y vs center-y delta to call a smile

# Landmark index ranges for buffalo_l 106-point model.
_EYEBROW_RANGE = (17, 27)  # 17..26 inclusive
_EYE_RANGE = (33, 47)      # 33..46 inclusive (both eyes covered)
_MOUTH_LEFT_CORNER = 76
_MOUTH_RIGHT_CORNER = 77
_MOUTH_CENTER = 52


class ExpressionCalibrator:
    def __init__(self, base_dir: Path):
        self._base_dir = Path(base_dir)
        self._state_dir = self._base_dir / "state"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._loaded_pids: set[str] = set()

    # ── persistence ────────────────────────────────────────────────────────────

    def _path(self, person_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in str(person_id))
        return self._state_dir / f"expression_baseline_{safe}.json"

    def _load(self, person_id: str) -> dict[str, Any]:
        if person_id in self._cache:
            return self._cache[person_id]
        p = self._path(person_id)
        baseline: dict[str, Any] = {
            "eyebrow_ratio": 0.0,
            "mouth_corner_offset": 0.0,
            "sample_count": 0,
            "calibrated": False,
            "last_updated": 0.0,
        }
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    baseline.update(data)
            except Exception:
                pass
        self._cache[person_id] = baseline
        self._loaded_pids.add(person_id)
        return baseline

    def _save(self, person_id: str) -> None:
        if person_id not in self._cache:
            return
        try:
            self._path(person_id).write_text(
                json.dumps(self._cache[person_id], indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[expression_calibrator] save error for {person_id}: {e}")

    # ── geometry helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _measure_eyebrow_ratio(landmarks: Any) -> Optional[float]:
        """Return (eye_y - eyebrow_y) / face_height. Larger = brows raised."""
        try:
            lm = landmarks
            if lm is None or len(lm) < 96:
                return None
            eyebrow_ys = [float(lm[i][1]) for i in range(*_EYEBROW_RANGE) if i < len(lm)]
            eye_ys = [float(lm[i][1]) for i in range(*_EYE_RANGE) if i < len(lm)]
            if not eyebrow_ys or not eye_ys:
                return None
            eyebrow_y = sum(eyebrow_ys) / len(eyebrow_ys)
            eye_y = sum(eye_ys) / len(eye_ys)
            # Face height: max y - min y across all landmarks.
            ys = [float(p[1]) for p in lm]
            face_height = max(ys) - min(ys)
            if face_height <= 1e-3:
                return None
            return (eye_y - eyebrow_y) / face_height
        except Exception:
            return None

    @staticmethod
    def _mouth_corner_offset(landmarks: Any) -> Optional[float]:
        """Negative offset = corners higher than center (smiling)."""
        try:
            lm = landmarks
            if lm is None or len(lm) <= max(_MOUTH_RIGHT_CORNER, _MOUTH_CENTER):
                return None
            left = float(lm[_MOUTH_LEFT_CORNER][1])
            right = float(lm[_MOUTH_RIGHT_CORNER][1])
            center = float(lm[_MOUTH_CENTER][1])
            avg_corner = (left + right) / 2.0
            return avg_corner - center
        except Exception:
            return None

    # ── public API ─────────────────────────────────────────────────────────────

    def calibrate_baseline(self, person_id: str, landmarks: Any) -> dict[str, Any]:
        """Update the EMA baseline for `person_id` from a single frame.

        Returns the current baseline dict (read-only snapshot)."""
        if not person_id or person_id == "unknown":
            return {}
        ratio = self._measure_eyebrow_ratio(landmarks)
        mouth = self._mouth_corner_offset(landmarks)
        with self._lock:
            baseline = self._load(person_id)
            n = int(baseline.get("sample_count") or 0)
            if ratio is not None:
                baseline["eyebrow_ratio"] = (
                    baseline.get("eyebrow_ratio", 0.0) * (1 - _ALPHA) + ratio * _ALPHA
                    if n > 0 else ratio
                )
            if mouth is not None:
                baseline["mouth_corner_offset"] = (
                    baseline.get("mouth_corner_offset", 0.0) * (1 - _ALPHA) + mouth * _ALPHA
                    if n > 0 else mouth
                )
            baseline["sample_count"] = n + 1
            baseline["last_updated"] = time.time()
            if not baseline["calibrated"] and baseline["sample_count"] >= _MIN_SAMPLES_CALIBRATED:
                baseline["calibrated"] = True
                print(f"[expression_calibrator] {person_id} calibrated after {baseline['sample_count']} samples")
            # Persist every 30 frames or on calibration transition.
            if baseline["sample_count"] % 30 == 0 or baseline.get("_just_calibrated"):
                self._save(person_id)
            return dict(baseline)

    def detect_expression(self, person_id: str, landmarks: Any) -> str:
        """Return expression label: surprised | frowning | smiling | neutral."""
        try:
            ratio = self._measure_eyebrow_ratio(landmarks)
            mouth = self._mouth_corner_offset(landmarks)
            if ratio is None:
                return "neutral"

            with self._lock:
                baseline = self._load(person_id) if person_id and person_id != "unknown" else {}
            calibrated = bool(baseline.get("calibrated"))

            if not calibrated:
                # Generic thresholds — these intentionally flag false positives less
                # often (high cutoff) until per-person baseline kicks in.
                if ratio > 0.10:
                    return "surprised"
                if mouth is not None and mouth < -_MOUTH_SMILE_PX:
                    return "smiling"
                return "neutral"

            base_ratio = float(baseline.get("eyebrow_ratio") or 0.0)
            base_mouth = float(baseline.get("mouth_corner_offset") or 0.0)
            deviation = ratio - base_ratio
            if deviation > _DEVIATION_SURPRISED:
                return "surprised"
            if deviation < _DEVIATION_FROWNING:
                return "frowning"
            if mouth is not None:
                mouth_dev = mouth - base_mouth
                if mouth_dev < -_MOUTH_SMILE_PX:
                    return "smiling"
            return "neutral"
        except Exception as e:
            print(f"[expression_calibrator] detect error: {e!r}")
            return "neutral"

    def get_baseline(self, person_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._load(person_id))

    def is_calibrated(self, person_id: str) -> bool:
        with self._lock:
            return bool(self._load(person_id).get("calibrated"))


# ── singleton ─────────────────────────────────────────────────────────────────

_SINGLETON: Optional[ExpressionCalibrator] = None
_SINGLETON_LOCK = threading.Lock()


def get_expression_calibrator(base_dir: Path | None = None) -> Optional[ExpressionCalibrator]:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    if base_dir is None:
        return None
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = ExpressionCalibrator(Path(base_dir))
    return _SINGLETON


def bootstrap_expression_calibrator(g: dict[str, Any]) -> Optional[ExpressionCalibrator]:
    base = Path(g.get("BASE_DIR") or ".")
    cal = get_expression_calibrator(base)
    g["_expression_calibrator"] = cal
    return cal
