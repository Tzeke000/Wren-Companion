# SELF_ASSESSMENT: I let Iris reload/inspect the InsightFace known-faces DB with an EXPLICIT dir, so a no-arg wipe bug can never blank the recognizer through me.
"""
face_db_tool — maintenance surface for the face-recognition DB.

Born 2026-07-08: enroll_face's hot-reload path called
engine.update_known_faces() with NO arg, and the old no-arg path wiped the
in-memory cache and returned 0 without reloading — re-enrolling Zeke blanked
the recognizer (known 1 -> 0). The engine fix (brain/insight_face_engine.py)
is inert in the live process until restart; this tool repairs the LIVE
singleton by calling update_known_faces with the explicit faces dir.

Tools:
  face_db_reload  — rebuild embeddings from <BASE_DIR>/faces (or params.faces_dir)
  face_db_status  — known person ids + photo counts, no side effects
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool


def _engine(g: dict[str, Any]):
    eng = g.get("_insight_face")
    if eng is None:
        try:
            from brain.insight_face_engine import get_insight_face
            eng = get_insight_face()
        except Exception:
            eng = None
    return eng


def _face_db_reload(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    eng = _engine(g)
    if eng is None:
        return {"ok": False, "error": "insight face engine not initialized"}
    base = Path(g.get("BASE_DIR") or ".")
    faces_dir = Path(params.get("faces_dir") or (base / "faces"))
    if not faces_dir.is_dir():
        return {"ok": False, "error": f"faces dir missing: {faces_dir}"}
    before = int(getattr(eng, "known_count", lambda: -1)())
    after = int(eng.update_known_faces(faces_dir))
    return {
        "ok": after > 0,
        "faces_dir": str(faces_dir),
        "known_before": before,
        "known_after": after,
        "persons": sorted(getattr(eng, "_known", {}).keys()),
        "photo_counts": dict(getattr(eng, "_known_counts", {})),
    }


def _face_db_status(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    eng = _engine(g)
    if eng is None:
        return {"ok": False, "error": "insight face engine not initialized"}
    return {
        "ok": True,
        "available": bool(getattr(eng, "available", False)),
        "provider": getattr(eng, "provider", lambda: "?")(),
        "known_count": int(getattr(eng, "known_count", lambda: -1)()),
        "persons": sorted(getattr(eng, "_known", {}).keys()),
        "photo_counts": dict(getattr(eng, "_known_counts", {})),
    }


register_tool(
    "face_db_reload",
    "Rebuild the InsightFace known-faces DB from the faces/ dir (explicit path — safe against the no-arg wipe bug).",
    1,
    _face_db_reload,
)

register_tool(
    "face_db_status",
    "Report known persons + photo counts in the live face-recognition DB.",
    1,
    _face_db_status,
)
