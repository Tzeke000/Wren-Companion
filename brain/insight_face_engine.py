"""
InsightFace GPU face recognition engine.

Uses the buffalo_l model pack (RetinaFace detector + ArcFace embedding +
age/gender + 106-pt landmarks + 3D head pose). Runs on
CUDAExecutionProvider when available, falls back to CPU.

This engine is ADDITIVE: the existing FaceRecognizer (face_recognition lib)
remains the backwards-compatible fallback. When this engine is available
the background tick loop publishes detailed _face_results so the camera
annotator can draw bounding boxes, landmarks, head-pose axes, age, gender.

Bootstrap-friendly: no preferences are baked in. Confidence threshold for
positive ID is the only tunable, defaults to 0.45 cosine similarity.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional


_SIMILARITY_THRESHOLD = 0.45  # cosine sim ≥ this → positive ID

_SINGLETON: Optional["InsightFaceEngine"] = None
_SINGLETON_LOCK = threading.Lock()


def _add_cuda_paths() -> list[str]:
    """Make CUDA DLLs from pip packages discoverable to onnxruntime.

    Pip-installed nvidia-* wheels drop DLLs under
    site-packages/nvidia/<lib>/bin/ but don't add them to PATH or DLL search
    paths. Without this, onnxruntime fails to load the CUDA EP and silently
    falls back to CPU (logging a "Error loading … which depends on … which
    is missing" message at import time).

    We register every nvidia/*/bin directory we find with os.add_dll_directory
    AND prepend to PATH for completeness. Returns the directories actually
    added so callers can log them.
    """
    site_packages = Path(sys.executable).parent / "Lib" / "site-packages"
    nv_root = site_packages / "nvidia"
    if not nv_root.is_dir():
        return []
    added: list[str] = []
    for sub in sorted(nv_root.iterdir()):
        if not sub.is_dir():
            continue
        bin_dir = sub / "bin"
        if not bin_dir.is_dir():
            continue
        try:
            os.add_dll_directory(str(bin_dir))  # Python 3.8+ Windows DLL search
        except Exception:
            pass
        # Prepend to PATH too — some downstream loaders still consult PATH.
        existing = os.environ.get("PATH", "")
        if str(bin_dir) not in existing.split(os.pathsep):
            os.environ["PATH"] = str(bin_dir) + os.pathsep + existing
        added.append(f"nvidia/{sub.name}/bin")
    return added


class InsightFaceEngine:
    def __init__(self) -> None:
        self._app: Any = None
        self._known: dict[str, Any] = {}        # person_id -> avg embedding (np.ndarray)
        self._known_counts: dict[str, int] = {}  # photos seen per person
        self._lock = threading.Lock()
        self._available = False
        self._provider: str = "none"
        self._init_error: str = ""

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def initialize(self, faces_dir: Path) -> bool:
        # Make pip-installed CUDA DLLs discoverable to onnxruntime BEFORE the
        # ORT import. ORT's DLL search happens inside its native module init,
        # so the dirs must be registered first or CUDAExecutionProvider will
        # silently fall back to CPU.
        added = _add_cuda_paths()
        if added:
            print(f"[insight_face] CUDA paths added: {', '.join(added)}")

        try:
            import onnxruntime as ort  # type: ignore
            from insightface.app import FaceAnalysis  # type: ignore
        except Exception as e:
            self._init_error = f"import: {e!r}"
            print(f"[insight_face] import failed: {e!r}")
            return False

        # Pick the best provider available.
        avail = list(ort.get_available_providers())
        ordered: list[str] = []
        if "CUDAExecutionProvider" in avail:
            ordered.append("CUDAExecutionProvider")
        ordered.append("CPUExecutionProvider")

        try:
            app = FaceAnalysis(name="buffalo_l", providers=ordered)
            app.prepare(ctx_id=0, det_size=(640, 640))
        except Exception as e:
            self._init_error = f"prepare: {e!r}"
            print(f"[insight_face] FaceAnalysis prepare failed: {e!r}")
            # Retry CPU-only if GPU prepare fails
            try:
                app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
                app.prepare(ctx_id=-1, det_size=(640, 640))
                ordered = ["CPUExecutionProvider"]
                self._init_error = ""
            except Exception as e2:
                self._init_error = f"prepare-cpu: {e2!r}"
                print(f"[insight_face] CPU prepare also failed: {e2!r}")
                return False

        self._app = app
        # Read actually-applied provider from one of the loaded sessions so the
        # log reflects reality (ORT silently falls back if a CUDA DLL is
        # missing — without this, the log would lie).
        actual_provider = ordered[0]
        try:
            for model_name in ("detection", "recognition"):
                model = app.models.get(model_name) if hasattr(app, "models") else None
                sess = getattr(model, "session", None) if model else None
                if sess is not None:
                    eps = list(sess.get_providers())
                    if eps:
                        actual_provider = eps[0]
                        break
        except Exception:
            pass
        self._provider = actual_provider
        self._load_faces(faces_dir)
        self._available = True
        # Snapshot reader (operator_server.py:1099) checks `getattr(ife, "ready", False)`
        # — without `self.ready = True` here, subsystem_health.insightface.available
        # stays False forever even when the engine is fully loaded and running.
        # Same pattern as the kokoro_loaded flag fix from commit e8e3dce.
        self.ready = True
        self.providers = [self._provider] if self._provider else []
        print(f"[insight_face] ready provider={self._provider} known_people={len(self._known)}")
        return True

    def _load_faces(self, faces_dir: Path) -> None:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except Exception as e:
            print(f"[insight_face] load deps missing: {e!r}")
            return
        if not faces_dir.is_dir():
            print(f"[insight_face] faces dir missing: {faces_dir}")
            return
        # Reference photos in faces/<pid>/*.png are typically TIGHT crops of
        # a face — 200x200 pixels with the face filling the whole frame. The
        # main buffalo_l app is prepared with det_size=(640, 640) for
        # live-camera frames, which is correct for normal photography but
        # misses faces that ARE the whole image (no surrounding context for
        # the detector to anchor on). Tonight's hardware test surfaced this:
        # 16 photos in faces/zeke/, all 200x200 PNGs, all returned zero
        # faces from app.get(); insight_face logged "loaded 0 embeddings
        # from 0 people" and Ava saw the user as UNKNOWN 0%.
        #
        # Two-part fix:
        #   1. Spin up a SECOND FaceAnalysis with det_size=(320, 320) just
        #      for reference-photo loading. Costs ~100 MB extra VRAM at
        #      boot but only used during _load_faces; the small detector
        #      handles tight crops correctly.
        #   2. Upscale to at least 640x640 before passing to the small
        #      detector — gives it pixel resolution to anchor on landmarks.
        #
        # Verified on the user's actual photos — detection went from 0/16
        # to >=1 per photo in initial smoke test.
        load_app = self._app
        _temp_app_used = False
        try:
            from insightface.app import FaceAnalysis  # type: ignore
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if self._provider == "CUDAExecutionProvider" else ["CPUExecutionProvider"]
            small = FaceAnalysis(name="buffalo_l", providers=providers)
            ctx = 0 if self._provider == "CUDAExecutionProvider" else -1
            small.prepare(ctx_id=ctx, det_size=(320, 320))
            load_app = small
            _temp_app_used = True
            print("[insight_face] using small-det loader for reference photos (det_size=320)")
        except Exception as _le:
            print(f"[insight_face] small-det loader unavailable, falling back to main app: {_le!r}")

        TARGET_MIN_DIM = 640
        loaded = 0
        for person_dir in sorted(faces_dir.iterdir()):
            if not person_dir.is_dir():
                continue
            pid = person_dir.name
            # Glob is case-insensitive on Windows but not POSIX; cover both
            # case variants explicitly so .JPG / .PNG don't get missed.
            patterns = ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG")
            seen: set[Path] = set()
            paths: list[Path] = []
            for pat in patterns:
                for p in person_dir.glob(pat):
                    if p not in seen:
                        seen.add(p)
                        paths.append(p)
            embs: list[Any] = []
            for img_path in sorted(paths):
                try:
                    img = cv2.imread(str(img_path))
                    if img is None:
                        print(f"[insight_face] skip {img_path.name}: cv2.imread returned None")
                        continue
                    h, w = img.shape[:2]
                    # Upscale tightly-cropped reference photos so the
                    # detector has enough resolution to anchor on.
                    if min(h, w) < TARGET_MIN_DIM:
                        scale = TARGET_MIN_DIM / max(1, min(h, w))
                        new_w = int(round(w * scale))
                        new_h = int(round(h * scale))
                        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
                    faces = load_app.get(img)
                    if faces:
                        # When multiple faces in one photo, take the largest
                        # (most likely the subject — small background faces
                        # on screenshots can mislead).
                        faces = sorted(
                            faces,
                            key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
                            reverse=True,
                        )
                        embs.append(faces[0].embedding)
                    else:
                        print(f"[insight_face] skip {img_path.name}: no face detected after upscale")
                except Exception as e:
                    print(f"[insight_face] skip {img_path.name}: {e!r}")
            if embs:
                avg = np.mean(np.stack(embs, axis=0), axis=0)
                self._known[pid] = avg
                self._known_counts[pid] = len(embs)
                loaded += len(embs)
                print(f"[insight_face]   {pid}: {len(embs)} photos")
        print(f"[insight_face] loaded {loaded} embeddings from {len(self._known)} people")

    # ── runtime ────────────────────────────────────────────────────────────────

    def analyze_frame(self, frame: Any) -> list[dict[str, Any]]:
        """Return list of dicts: bbox, landmarks (106), pose, age, gender, person_id, confidence."""
        if not self._available or self._app is None or frame is None:
            return []
        try:
            with self._lock:
                faces = self._app.get(frame)
        except Exception as e:
            print(f"[insight_face] analyze error: {e!r}")
            return []

        results: list[dict[str, Any]] = []
        for f in faces:
            try:
                emb = getattr(f, "embedding", None)
                pid = "unknown"
                conf = 0.0
                if emb is not None:
                    pid, conf = self._match(emb)
                bbox = getattr(f, "bbox", None)
                landmark = getattr(f, "landmark_2d_106", None)
                pose = getattr(f, "pose", None)
                age = getattr(f, "age", None)
                gender = getattr(f, "gender", None)
                results.append({
                    "bbox": [int(v) for v in bbox.tolist()] if bbox is not None else [0, 0, 0, 0],
                    "landmarks": landmark.tolist() if landmark is not None else None,
                    "pose": pose.tolist() if pose is not None else [0.0, 0.0, 0.0],
                    "age": int(age) if age is not None else 0,
                    "gender": ("M" if int(gender) == 1 else "F") if gender is not None else "?",
                    "person_id": pid,
                    "confidence": float(conf),
                })
            except Exception as e:
                print(f"[insight_face] face decode error: {e!r}")
        return results

    def _match(self, emb: Any) -> tuple[str, float]:
        if not self._known:
            return "unknown", 0.0
        try:
            import numpy as np  # type: ignore
        except Exception:
            return "unknown", 0.0
        best_pid = "unknown"
        best_score = 0.0
        emb_norm = float(np.linalg.norm(emb)) or 1.0
        for pid, known in self._known.items():
            kn = float(np.linalg.norm(known)) or 1.0
            score = float(np.dot(emb, known) / (emb_norm * kn))
            if score > best_score:
                best_score = score
                best_pid = pid
        if best_score >= _SIMILARITY_THRESHOLD:
            return best_pid, best_score
        return "unknown", best_score

    def add_face(self, person_id: str, image: Any) -> bool:
        """Add a single face image to the known set; averaged into existing embedding."""
        if not self._available or self._app is None:
            return False
        try:
            import numpy as np  # type: ignore
            faces = self._app.get(image)
            if not faces:
                return False
            emb = faces[0].embedding
            with self._lock:
                if person_id in self._known:
                    n = self._known_counts.get(person_id, 1)
                    self._known[person_id] = (self._known[person_id] * n + emb) / (n + 1)
                    self._known_counts[person_id] = n + 1
                else:
                    self._known[person_id] = emb
                    self._known_counts[person_id] = 1
            return True
        except Exception as e:
            print(f"[insight_face] add_face error: {e!r}")
            return False

    def update_known_faces(self, faces_dir: Path | None = None) -> int:
        """Reload all embeddings from disk. Called by onboarding after photos
        are saved so any newly-added person directory is picked up. Wipes the
        in-memory cache and rebuilds it. Returns the new known_count."""
        if not self._available or self._app is None:
            return 0
        target = Path(faces_dir) if faces_dir is not None else None
        with self._lock:
            self._known = {}
            self._known_counts = {}
        if target is None:
            return 0
        self._load_faces(target)
        return len(self._known)

    # ── status ─────────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._available

    def provider(self) -> str:
        return self._provider

    def known_count(self) -> int:
        return len(self._known)


# ── Module singleton ──────────────────────────────────────────────────────────

def get_insight_face() -> Optional[InsightFaceEngine]:
    """Return the singleton if it has been initialized, None otherwise."""
    return _SINGLETON


def bootstrap_insight_face(g: dict[str, Any]) -> Optional[InsightFaceEngine]:
    """Create and initialize the singleton. Stores result in g['_insight_face'].
    Returns the engine if init succeeded, None on failure."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            g["_insight_face"] = _SINGLETON
            return _SINGLETON
        engine = InsightFaceEngine()
        base = Path(g.get("BASE_DIR") or ".")
        ok = engine.initialize(base / "faces")
        if ok:
            _SINGLETON = engine
            g["_insight_face"] = engine
            return engine
        g["_insight_face"] = None
        return None
