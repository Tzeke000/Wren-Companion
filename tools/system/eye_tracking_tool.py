"""
Eye tracking calibration tool — Tier 1.
SELF_ASSESSMENT: Tier 1 — Ava initiates gaze calibration when she thinks it would help.
She decides when gaze information is relevant to mention or act on.
"""
from __future__ import annotations

from typing import Any


def _calibrate_eye_tracking_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain.eye_tracker import get_eye_tracker
        et = get_eye_tracker()
        if et is None:
            from brain.eye_tracker import bootstrap_eye_tracker
            et = bootstrap_eye_tracker(g)
        if et is None or not et.available:
            return {"ok": False, "error": "Eye tracker not available — mediapipe may not be installed"}
        success = et.calibrate()
        return {"ok": success, "calibrated": et.calibrated, "message": "Calibration complete" if success else "Calibration failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _get_gaze_info_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain.eye_tracker import get_eye_tracker
        from brain.expression_detector import get_expression_detector
        from brain.frame_store import read_live_frame_with_meta, LIVE_CACHE_MAX_AGE_SEC
        et = get_eye_tracker()
        ed = get_expression_detector()
        meta = read_live_frame_with_meta(max_age=LIVE_CACHE_MAX_AGE_SEC)
        frame = meta.frame
        result: dict[str, Any] = {"gaze_region": "unknown", "attention": "unknown", "expression": "neutral"}
        if frame is not None and et is not None and et.available:
            result["gaze_region"] = et.get_gaze_region(frame)
            result["attention"] = et.get_attention_state(frame)
            result["looking_at_screen"] = et.is_looking_at_screen(frame)
        if frame is not None and ed is not None and ed.available:
            scores = ed.detect_expression(frame)
            result["expression"] = str(scores.get("dominant") or "neutral")
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


try:
    from tools.tool_registry import register_tool
    register_tool("calibrate_eye_tracking", "Run 9-point gaze calibration sequence.", 1, _calibrate_eye_tracking_fn)
    register_tool("get_gaze_info", "Get current gaze region, attention state, and expression.", 1, _get_gaze_info_fn)
except Exception:
    pass
