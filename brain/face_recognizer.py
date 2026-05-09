"""
Phase 81 — Proper face recognition using face_recognition library.

FaceRecognizer loads embeddings from faces/{person_id}/ on startup,
then provides recognition and onboarding photo registration.
Falls back gracefully if face_recognition not installed.

Confidence threshold: 0.6 for positive ID (lower distance = better match).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

_CONFIDENCE_THRESHOLD = 0.6  # face distance ≤ this → positive ID
_SINGLETON: dict[str, "FaceRecognizer"] = {}
_SINGLETON_LOCK = threading.Lock()  # prevents concurrent creation + load on first call


class FaceRecognizer:
    def __init__(self, base_dir: Path):
        self._base_dir = base_dir
        self._known_encodings: list[Any] = []
        self._known_ids: list[str] = []
        self._known_names: list[str] = []
        self._available = False
        self._loaded_at: float = 0.0
        self._loaded = False
        self._load_lock = threading.Lock()  # serialises concurrent load_known_faces calls
        try:
            import face_recognition  # noqa: F401
            self._available = True
        except ImportError:
            print("[face_recognizer] face_recognition not available — recognition disabled")

    @property
    def available(self) -> bool:
        return self._available

    def load_known_faces(self, force: bool = False) -> int:
        """Load all face images from faces/ directory. Returns count of known faces loaded.
        Thread-safe: only one thread loads at a time; subsequent calls are no-ops unless force=True."""
        if not self._available:
            return 0
        # Fast path: already loaded and not forced
        if self._loaded and not force:
            return len(self._known_encodings)
        with self._load_lock:
            # Double-check inside lock
            if self._loaded and not force:
                return len(self._known_encodings)
            try:
                import face_recognition
            except ImportError:
                return 0

            faces_dir = self._base_dir / "faces"
            encodings: list[Any] = []
            ids: list[str] = []
            names: list[str] = []

            if not faces_dir.is_dir():
                self._known_encodings = encodings
                self._known_ids = ids
                self._known_names = names
                self._loaded = True
                self._loaded_at = time.time()
                return 0

            count = 0
            for person_dir in sorted(faces_dir.iterdir()):
                if not person_dir.is_dir():
                    continue
                person_id = person_dir.name
                name = self._get_display_name(person_id)
                for img_path in sorted(person_dir.glob("*.jpg")) + sorted(person_dir.glob("*.png")):
                    try:
                        img = face_recognition.load_image_file(str(img_path))
                        encs = face_recognition.face_encodings(img)
                        if encs:
                            encodings.append(encs[0])
                            ids.append(person_id)
                            names.append(name)
                            count += 1
                    except Exception as e:
                        print(f"[face_recognizer] skip {img_path.name}: {e}")

            self._known_encodings = encodings
            self._known_ids = ids
            self._known_names = names
            self._loaded_at = time.time()
            self._loaded = True
            print(f"[face_recognizer] loaded {count} encodings from {len(set(ids))} people")
            return count

    def update_known_faces(self) -> int:
        """Reload embeddings — call after new photos added."""
        return self.load_known_faces(force=True)

    def recognize_frame(self, frame: Any) -> list[dict[str, Any]]:
        """
        Returns list of dicts: {person_id, name, confidence, bounding_box}.
        confidence is 0–1 (1 = perfect match).
        """
        if not self._available or not self._known_encodings:
            return []
        try:
            import face_recognition
            import numpy as np

            # Ensure RGB (face_recognition expects RGB, OpenCV gives BGR)
            if isinstance(frame, np.ndarray) and frame.ndim == 3 and frame.shape[2] == 3:
                rgb = frame[:, :, ::-1].copy()
            else:
                rgb = frame

            locations = face_recognition.face_locations(rgb, model="hog")
            if not locations:
                return []

            encodings = face_recognition.face_encodings(rgb, locations)
            results: list[dict[str, Any]] = []
            for enc, loc in zip(encodings, locations):
                distances = face_recognition.face_distance(self._known_encodings, enc)
                if len(distances) == 0:
                    results.append({
                        "person_id": "unknown", "name": "Unknown",
                        "confidence": 0.0, "bounding_box": loc,
                    })
                    continue
                best_idx = int(np.argmin(distances))
                best_dist = float(distances[best_idx])
                confidence = max(0.0, 1.0 - best_dist)
                if best_dist <= _CONFIDENCE_THRESHOLD:
                    results.append({
                        "person_id": self._known_ids[best_idx],
                        "name": self._known_names[best_idx],
                        "confidence": round(confidence, 3),
                        "bounding_box": loc,
                    })
                else:
                    results.append({
                        "person_id": "unknown", "name": "Unknown",
                        "confidence": round(confidence, 3),
                        "bounding_box": loc,
                    })
            return results
        except Exception as e:
            print(f"[face_recognizer] recognize_frame error: {e}")
            return []

    def get_best_match(self, frame: Any) -> tuple[str, float]:
        """Returns (person_id, confidence) for most likely person. person_id='unknown' if none."""
        results = self.recognize_frame(frame)
        if not results:
            return "unknown", 0.0
        best = max(results, key=lambda r: r["confidence"])
        return best["person_id"], best["confidence"]

    def add_face(self, person_id: str, image_path: Path) -> bool:
        """Add a single face image to known set without full reload."""
        if not self._available:
            return False
        try:
            import face_recognition
            img = face_recognition.load_image_file(str(image_path))
            encs = face_recognition.face_encodings(img)
            if encs:
                name = self._get_display_name(person_id)
                self._known_encodings.append(encs[0])
                self._known_ids.append(person_id)
                self._known_names.append(name)
                return True
        except Exception as e:
            print(f"[face_recognizer] add_face error: {e}")
        return False

    def _get_display_name(self, person_id: str) -> str:
        profile_path = self._base_dir / "profiles" / f"{person_id}.json"
        if profile_path.is_file():
            try:
                data = json.loads(profile_path.read_text(encoding="utf-8"))
                return str(data.get("name") or person_id)
            except Exception:
                pass
        return person_id


def get_recognizer(base_dir: Path) -> FaceRecognizer:
    """Return cached singleton. Thread-safe: only one thread creates+loads the recognizer."""
    key = str(base_dir)
    # Fast path: singleton already set
    if key in _SINGLETON:
        return _SINGLETON[key]
    with _SINGLETON_LOCK:
        # Double-check inside lock in case another thread just set it
        if key in _SINGLETON:
            return _SINGLETON[key]
        rec = FaceRecognizer(base_dir)
        rec.load_known_faces()
        _SINGLETON[key] = rec
    return _SINGLETON[key]
