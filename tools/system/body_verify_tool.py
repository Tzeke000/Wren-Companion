"""body_verify_move — close the actuator action-loop with visual proof.

Iris_fixes #2 (Zeke 2026-09-30): "did the thing I commanded physically happen"
should close every body action loop. The PTZ actuator's look_at() reports
moved:True when the WinRT/v4l2 command returns 0 — but a success dict is not
motion (CORE scar: the head reached the ceiling with an honest-looking success
trail; I once retracted a TRUE statement because a success dict fooled me).

This tool commands a small, bounded, self-reversing test pan and confirms the
CAMERA IMAGE actually shifted (brain.body_verify, phase correlation) — the only
ground truth independent of the command echo and the device's own bearing
readback. It cross-checks the bearing readback too, but the image is the arbiter.

SAFETY: this MOVES the physical head when called. It is bounded (|pan_delta|<=20),
restores the start bearing by default, and refuses if a follow/servo loop is
active (so it can't fight the tracker). Per standing practice I run the live
version only with Zeke present; the mechanism + its logic are proven offline in
scripts/test_body_verify.py (9/9) so this tool is the live smoke-test, not the
proof.
"""
from __future__ import annotations

import time
from typing import Any

from tools.tool_registry import register_tool


def _pan_sign() -> int:
    try:
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parents[2] / "state" / "room_geometry.json"
        return int(round(float(json.loads(p.read_text(encoding="utf-8")).get("pan_sign", -1))))
    except Exception:
        return -1


def _grab_frame():
    try:
        from brain.frame_store import get_buffered_frame
        r = get_buffered_frame(max_age_sec=3.0)
        return getattr(r, "frame", None)
    except Exception:
        return None


def _follow_active(g: dict) -> bool:
    """True if a follow/servo loop is currently driving the gimbal — we must not
    inject a test move into an active tracker."""
    try:
        st8 = (g or {}).get("_attention_follow_state") or {}
        th = st8.get("thread")
        if th is not None and getattr(th, "is_alive", lambda: False)():
            return True
    except Exception:
        pass
    try:
        st = (g or {}).get("_attention_smooth_state") or {}
        th = st.get("thread")
        if th is not None and getattr(th, "is_alive", lambda: False)():
            return True
    except Exception:
        pass
    return False


def _body_verify_move(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "run").lower()
    if action == "explain":
        return {"ok": True,
                "what": "commands a bounded test pan and confirms the image actually "
                        "shifted (verify-effect); catches success-dict-without-motion",
                "safety": "|pan_delta|<=20, restores start bearing, refuses if a "
                          "follow/servo loop is running",
                "logic_test": "scripts/test_body_verify.py (9/9 offline)"}

    pan_delta = float(params.get("pan_delta", 8.0))
    pan_delta = max(-20.0, min(20.0, pan_delta))
    restore = bool(params.get("restore", True))
    settle_s = float(params.get("settle_s", 0.6))

    if abs(pan_delta) < 1.0:
        return {"ok": False, "error": "pan_delta too small to verify (need >=1 deg)"}
    if _follow_active(g):
        return {"ok": False, "error": "a follow/servo loop is active — stop it first "
                                      "(attention_follow stop / attention_smooth stop) "
                                      "so the test move can't fight the tracker"}

    try:
        from brain import visual_attention as va
        from brain import body_verify as bv
    except Exception as e:
        return {"ok": False, "error": f"import failed: {e!r}"}

    act = va.build_actuator()
    if not getattr(act, "available", lambda: False)():
        return {"ok": False, "error": "no live PTZ actuator (fixed camera or head absent)"}

    start = act.bearing()
    start_pan = float(start.get("pan_deg", 0.0) or 0.0)
    start_tilt = start.get("tilt_deg", None)
    target_pan = start_pan + pan_delta

    result = bv.verify_effect(
        grab_frame=_grab_frame,
        do_move=lambda: act.look_at(target_pan, start_tilt),
        commanded_pan_delta=pan_delta,
        pan_sign=_pan_sign(),
        bearing_after_fn=act.bearing,
        target_bearing={"pan_deg": target_pan},
        settle_s=settle_s,
    )

    restored = None
    if restore:
        try:
            time.sleep(0.2)
            restored = act.look_at(start_pan, start_tilt)
        except Exception as e:
            restored = {"ok": False, "reason": repr(e)}

    return {"ok": True,
            "verdict": result.get("verdict"),
            "detail": result.get("detail"),
            "commanded_pan_delta": pan_delta,
            "start_bearing": start,
            "shift": result.get("shift"),
            "bearing_after": result.get("bearing_after"),
            "bearing_pan_error_deg": result.get("bearing_pan_error_deg"),
            "restored": restored,
            "note": "verdict 'no_effect' = actuator claimed a move that produced NO "
                    "image shift (the false-green #2 targets); 'confirmed' = image "
                    "actually moved the expected way."}


register_tool(
    "body_verify_move",
    "VERIFY-EFFECT for the PTZ head: command a small, self-reversing test pan and "
    "confirm the CAMERA IMAGE actually shifted (not just a success dict). "
    "action='run' (pan_delta deg default 8, restore=true, settle_s) | 'explain'. "
    "Refuses if a follow/servo loop is active. Catches 'moved:True' with no real "
    "motion as verdict='no_effect'. MOVES the head when run — use with Zeke present.",
    2,
    _body_verify_move,
)
