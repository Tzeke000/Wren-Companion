# SELF_ASSESSMENT: One-shot repair tool — rebinds orb_http._grab_camera_frame_b64 in the LIVE runtime so the orb's camera view gets serve-time overlays, without a restart and without importlib.reload (which would wipe orb_http._g).
"""
orb_frame_patch_tool — activate the 2026-08-21 orb overlay fix live.

Why not brain_hot_swap: it importlib.reload()s the module, and orb_http's
top level re-executes `_g: dict = {}` — wiping the runtime globals binding
that start() installed, which would break every orb endpoint at once.

This tool instead REBINDS the module attribute: the running uvicorn route
handler resolves `_grab_camera_frame_b64` through orb_http.__dict__ at call
time, so a rebind takes effect on the next HTTP request. The matching edit
is already on disk in brain/orb_http.py, so the next stack restart makes
this tool a no-op (safe to call anytime; idempotent).
"""
from __future__ import annotations

import base64
import sys
from typing import Any

from tools.tool_registry import register_tool


def _orb_frame_annotate_patch(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    import brain.orb_http as oh

    def _grab_camera_frame_b64() -> tuple:
        """Live-patched copy of brain.orb_http._grab_camera_frame_b64
        (2026-08-21): identical to the on-disk version, incl. serve-time
        display annotation. Buffer stays CLEAN; overlays drawn on a copy."""
        try:
            from brain.frame_store import get_buffered_frame
            meta = get_buffered_frame(max_age_sec=2.0)
            if meta.frame is None:
                return None, 0.0
            import cv2
            frame = meta.frame
            try:
                from brain.camera_annotator import annotate_display as _annotate
                frame = _annotate(frame.copy(), oh._g.get("_face_results"), oh._g)
            except Exception:
                pass  # raw frame beats no frame
            ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            if not ok:
                return None, 0.0
            return base64.b64encode(jpg.tobytes()).decode("ascii"), float(meta.age_sec)
        except Exception as e:
            print(f"[orb_http] frame_store read failed: {e!r}", file=sys.stderr, flush=True)
            return None, 0.0

    already = getattr(oh._grab_camera_frame_b64, "__name__", "") == "_grab_camera_frame_b64" and \
        oh._grab_camera_frame_b64.__module__ == __name__
    oh._grab_camera_frame_b64 = _grab_camera_frame_b64
    _grab_camera_frame_b64.__module__ = __name__
    return {
        "ok": True,
        "already_patched": bool(already),
        "note": "orb live_frame now annotates at serve time; disk edit in brain/orb_http.py covers the next restart",
    }


register_tool(
    "orb_frame_annotate_patch",
    "Live-activate the orb camera overlay fix: rebind orb_http._grab_camera_frame_b64 (serve-time annotation) without reload/restart. Idempotent.",
    2,
    _orb_frame_annotate_patch,
)
