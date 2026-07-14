# SELF_ASSESSMENT: I am Iris's PRECISE-MOTION hands for the Vector body — encoder/gyro-exact behaviors via the direct anki_vector SDK (not wire-pod's raw wheels).
"""
Vector precise-motion tools — 2026-07-14.

These use brain.vector_action, which opens an ON-DEMAND direct-SDK control
session (proven to coexist with the inhabit/observe daemon — nerves keep
flowing), runs an encoder/gyro-EXACT behavior, and disconnects. This retires
the timed-burst drift fighting: turn_in_place instead of guessing spin
duration, drive_straight instead of guessing forward time, drive_on_charger
for the reliable dock seat I could never thread by hand.

Distinct from vector_body_tool.py (wire-pod /api-sdk raw move_wheels). Don't
interleave a raw drive with an action-session behavior — one holds control at
a time. For a multi-step routine (figure-8), use vector_sequence so ONE
session spans all the waypoints.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool
from brain import vector_action as va


def _turn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        angle = float(params.get("angle_deg"))
    except Exception:
        return {"ok": False, "error": "angle_deg required (float; + = left/CCW)"}
    speed = float(params.get("speed_deg_s") or 90.0)
    return va.do_turn(angle, speed)


def _straight(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        dist = float(params.get("dist_mm"))
    except Exception:
        return {"ok": False, "error": "dist_mm required (float; + fwd, - back)"}
    speed = float(params.get("speed_mm_s") or 100.0)
    return va.do_straight(dist, speed)


def _go_to_pose(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        x = float(params.get("x"))
        y = float(params.get("y"))
    except Exception:
        return {"ok": False, "error": "x and y required (mm)"}
    angle = float(params.get("angle_deg") or 0.0)
    relative = bool(params.get("relative", True))
    return va.do_go_to_pose(x, y, angle, relative)


def _dock(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return va.do_dock()


def _undock(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return va.do_undock()


def _head(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        a = float(params.get("angle_deg"))
    except Exception:
        return {"ok": False, "error": "angle_deg required (-22 down .. +45 up)"}
    return va.do_head(a)


def _lift(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        r = float(params.get("ratio"))
    except Exception:
        return {"ok": False, "error": "ratio required (0.0 down .. 1.0 up)"}
    return va.do_lift(r)


def _sequence(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    steps = params.get("steps")
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "error": "steps required: list of {op,...} dicts "
                "(op = turn|straight|pose|dock|undock|head|lift)"}
    return va.run_sequence(steps)


register_tool("vector_turn", "PRECISE gyro-exact rotation in place. params: angle_deg (+ = left/CCW), speed_deg_s opt. Retires timed-spin guessing.", 1, _turn)
register_tool("vector_straight", "PRECISE encoder-exact straight drive. params: dist_mm (+ fwd/- back), speed_mm_s opt.", 1, _straight)
register_tool("vector_go_to_pose", "Drive to a pose. params: x,y (mm), angle_deg opt, relative opt (default true = relative to current pose). Figure-8 = waypoints.", 1, _go_to_pose)
register_tool("vector_dock", "NATIVE reliable charger seat (drive_on_charger) — the last-3cm dock I couldn't thread by hand.", 1, _dock)
register_tool("vector_undock", "Drive off the charger cleanly (drive_off_charger).", 1, _undock)
register_tool("vector_set_head", "PRECISE head pitch. params: angle_deg (-22 down .. +45 up).", 1, _head)
register_tool("vector_set_lift", "PRECISE lift height. params: ratio (0.0 down .. 1.0 up).", 1, _lift)
register_tool("vector_sequence", "Run a list of precise primitives in ONE control session (consistent pose frame). params: steps=[{op,...}]. op=turn|straight|pose|dock|undock|head|lift.", 1, _sequence)
