# SELF_ASSESSMENT: Read-only diagnostic — reports each link of the face pipeline (capture thread -> insightface -> _face_results) and runs ONE live analyze on the current buffered frame, so a blind-recognizer fault names its broken link instead of guessing.
"""face_pipeline_probe — which link of the face pipeline is dead?

Born 2026-08-22: Zeke visible dead-center, frames fresh, insightface
"loaded" per health — yet face_count=0 and no boxes. The pipeline is
capture-loop -> insight.analyze_frame -> g[_face_results] -> annotator/
attention, and health only reports the ENGINE flag, not the links.
"""
from __future__ import annotations

import time
from typing import Any

from tools.tool_registry import register_tool


def _probe(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True}
    fr = g.get("_face_results")
    out["face_results"] = {
        "type": type(fr).__name__,
        "len": (len(fr) if isinstance(fr, list) else None),
    }
    ins = g.get("_insight_face")
    out["insight"] = {
        "present": ins is not None,
        "available": bool(getattr(ins, "available", False)) if ins else None,
    }
    cm = g.get("camera_manager")
    out["camera_manager_running"] = bool(getattr(cm, "running", None)) if cm else None
    # repair=true: rebuild the InsightFace singleton in-process (2026-08-22:
    # engine reported available=True yet detected 0 faces in 5ms on a clear
    # frontal face — dead inference session behind a live flag). The capture
    # loop re-reads g["_insight_face"] every frame, so the rebuild is picked
    # up immediately; ~60s cudnn warmup possible.
    if (params or {}).get("repair"):
        try:
            import brain.insight_face_engine as ife
            if (params or {}).get("reload_code"):
                # Pick up on-disk edits to the ENGINE CLASS: methods of a live
                # instance can't be code-swapped, so reload the module and let
                # bootstrap build a fresh instance from the new class.
                import importlib
                ife = importlib.reload(ife)
            with ife._SINGLETON_LOCK:
                ife._SINGLETON = None
            t0 = time.time()
            eng = ife.bootstrap_insight_face(g)
            out["repair"] = {
                "rebuilt": eng is not None,
                "provider": (getattr(eng, "_provider", None) if eng else None),
                "known_people": (eng.known_count() if eng else 0),
                "s": round(time.time() - t0, 1),
            }
            ins = eng
        except Exception as e:
            out["repair_error"] = repr(e)[:300]
            return out
    # Live one-shot: run insight on the CURRENT buffered frame, or on a
    # saved image (params.image_path) to test recognition against a KNOWN
    # face when nobody is posing for the camera right now.
    try:
        img_path = str((params or {}).get("image_path") or "")
        if img_path:
            import cv2
            frame = cv2.imread(img_path)
            out["frame"] = {"present": frame is not None, "src": img_path}
            if frame is not None and ins is not None:
                t0 = time.time()
                res = ins.analyze_frame(frame)
                out["live_analyze"] = {
                    "ms": round((time.time() - t0) * 1000, 1),
                    "faces": len(res or []),
                    "best": (max(res, key=lambda r: float(r.get("confidence") or 0))
                             if res else None),
                }
            return out
        from brain.frame_store import get_buffered_frame
        meta = get_buffered_frame(max_age_sec=3.0)
        out["frame"] = {"present": meta.frame is not None,
                        "age_s": round(float(meta.age_sec), 2)}
        if meta.frame is not None and ins is not None:
            t0 = time.time()
            res = ins.analyze_frame(meta.frame)
            out["live_analyze"] = {
                "ms": round((time.time() - t0) * 1000, 1),
                "faces": len(res or []),
                "best": (max(res, key=lambda r: float(r.get("confidence") or 0))
                         if res else None),
            }
    except Exception as e:
        out["live_analyze_error"] = repr(e)[:300]
    return out


register_tool(
    "face_pipeline_probe",
    "Diagnose the face pipeline link-by-link: _face_results state, insightface availability, capture flag, one live analyze on the current frame.",
    1,
    _probe,
)
