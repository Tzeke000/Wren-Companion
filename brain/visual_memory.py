from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from sklearn.cluster import DBSCAN  # type: ignore
except Exception:  # pragma: no cover
    DBSCAN = None


@dataclass
class VisualCluster:
    cluster_id: int
    image_count: int
    named_label: str | None
    representative_image: str | None


class VisualMemory:
    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        self.state_path = self.base_dir / "state" / "visual_clusters.json"
        self.faces_dir = self.base_dir / "faces"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, Any] = {"clusters": [], "updated_at": None}
        self._load()

    def _load(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._state = payload
        except Exception:
            pass

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8")

    def _extract_embedding_placeholder(self, image_path: Path) -> list[float]:
        # Placeholder for DeepFace embedding extraction.
        seed = sum(image_path.name.encode("utf-8")) % 997
        return [((seed + i * 17) % 100) / 100.0 for i in range(12)]

    def cluster_faces(self, image_dir: Path | str | None = None) -> dict[str, Any]:
        directory = Path(image_dir) if image_dir else self.faces_dir
        if not directory.is_absolute():
            directory = (self.base_dir / directory).resolve()
        if not directory.is_dir():
            return {"ok": False, "error": f"faces directory missing: {directory}", "clusters": []}
        images = []
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            images.extend(directory.glob(ext))
        images = sorted(images)
        if not images:
            self._state = {"clusters": [], "updated_at": None}
            self._save()
            return {"ok": True, "clusters": []}
        embeddings = [self._extract_embedding_placeholder(p) for p in images]
        labels = [-1] * len(images)
        if DBSCAN is not None:
            try:
                db = DBSCAN(eps=0.28, min_samples=2)
                labels = list(db.fit_predict(embeddings))
            except Exception:
                labels = [-1] * len(images)
        grouped: dict[int, list[Path]] = {}
        for i, label in enumerate(labels):
            grouped.setdefault(int(label), []).append(images[i])
        clusters = []
        for label, group in sorted(grouped.items(), key=lambda kv: len(kv[1]), reverse=True):
            clusters.append(
                {
                    "cluster_id": int(label),
                    "image_count": len(group),
                    "named_label": None if label < 0 else f"cluster_{label}",
                    "representative_image": str(group[0]) if group else None,
                }
            )
        self._state = {"clusters": clusters, "updated_at": __import__("time").time(), "image_dir": str(directory)}
        self._save()
        return {"ok": True, "clusters": clusters}

    def get_cluster_summary(self) -> dict[str, Any]:
        clusters = list(self._state.get("clusters") or [])
        named = [c for c in clusters if str(c.get("named_label") or "").strip()]
        unknown = [c for c in clusters if not str(c.get("named_label") or "").strip()]
        most_seen = ""
        if clusters:
            top = max(clusters, key=lambda c: int(c.get("image_count") or 0))
            most_seen = str(top.get("named_label") or f"cluster_{top.get('cluster_id')}")
        return {
            "cluster_count": len(clusters),
            "named_clusters": len(named),
            "unknown_clusters": len(unknown),
            "most_seen": most_seen,
        }


def cluster_faces(image_dir: str) -> dict[str, Any]:
    vm = VisualMemory(Path("D:/AvaAgentv2"))
    return vm.cluster_faces(image_dir=image_dir)


def get_cluster_summary() -> dict[str, Any]:
    vm = VisualMemory(Path("D:/AvaAgentv2"))
    return vm.get_cluster_summary()

