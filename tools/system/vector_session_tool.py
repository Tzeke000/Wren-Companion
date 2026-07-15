# SELF_ASSESSMENT: I am Iris's PERSISTENT body — one held Vector control session
# with a live camera feed, so I stay SEATED in the robot instead of jumping out
# for every command.
"""Persistent body-session tools — 2026-07-15.

Zeke: "not move then check cam then move then check cam again ... you need to be
streamed the info ... do all the vector body things while seated in the body,
jumping out the body for a tool call wastes time and effort."

These wrap brain/vector_session.py (the held anki_vector control connection +
live camera feed + safety guard). The connection lives in iris_runtime module
state, so `body_open` once, then look/drive/turn/head/lift/dock all reuse the
SAME session — control never drops, the head never spuriously resets, and
`body_look` samples the live feed instantly (no per-grab MJPEG open/close).

Distinct from:
  * vector_body_tool.py  (vector_*)  — wire-pod /api-sdk RAW motion (per-call HTTP)
  * vector_action_tool.py (vector_turn/straight/...) — on-demand SDK session PER call
These body_* tools are the "stay seated" path; use them for piloting. The
observe/inhabit daemon (nerves/ears/battery/nav_map) keeps running alongside.

Hot-loadable via iris_tool_reload so the body layer can grow mid-session.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _sess():
    from brain import vector_session
    return vector_session


def _body_open(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Seat myself: open ONE held control session + live camera feed. Coexists
    with the observe daemon. Idempotent (returns status if already open)."""
    try:
        timeout = float(params.get("timeout") or 20.0)
    except Exception:
        timeout = 20.0
    return _sess().open_session(timeout=timeout)


def _body_close(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Un-seat: stop wheels, release control, disconnect. Stock brain resumes."""
    reason = str(params.get("reason") or "requested")
    return _sess().close_session(reason=reason)


def _body_status(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Session + REAL battery (SDK is_charging) + wheels/head/reflex + nerves."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": True, "connected": False, "note": "no body session open"}
    return s.status()


def _body_look(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """MY EYES, instant: sample the live feed -> jpg path to Read. No stream
    open/close. Requires body_open."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open (call body_open)"}
    name = str(params.get("name") or "body_view").strip() or "body_view"
    return s.look(name=name)


def _body_drive(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Continuous raw wheel drive (NO head reset), deadman + edge guarded.
    lw/rw mm/s (max 120), ttl secs (default 0.8, auto-stops unless re-issued).
    lw=rw>0 forward; lw=rw<0 back; lw=-rw spins in place."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open (call body_open)"}
    return s.drive(lw=params.get("lw") or 0, rw=params.get("rw") or 0,
                   ttl=params.get("ttl") or 0.8)


def _body_stop(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Stop all motion now."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    return s.stop()


def _body_turn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Gyro-EXACT turn in place. angle_deg +left / -right. Restores head after."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        angle = float(params.get("angle_deg"))
    except Exception:
        return {"ok": False, "error": "angle_deg (float, +left/-right) required"}
    return s.turn(angle, speed_deg_s=float(params.get("speed_deg_s") or 90.0))


def _body_straight(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Encoder-EXACT straight line. dist_mm +fwd / -back. Cliff-safe. Restores head."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        dist = float(params.get("dist_mm"))
    except Exception:
        return {"ok": False, "error": "dist_mm (float, +fwd/-back) required"}
    return s.straight(dist, speed_mm_s=float(params.get("speed_mm_s") or 100.0))


def _body_pose(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Drive to a pose (x,y mm, heading deg), relative to current pose by default."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        x = float(params.get("x")); y = float(params.get("y"))
    except Exception:
        return {"ok": False, "error": "x and y (mm) required"}
    return s.go_to_pose(x, y, angle_deg=float(params.get("angle_deg") or 0.0),
                        relative=bool(params.get("relative", True)))


def _body_head(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Set head pitch (-22 down .. +45 up) and remember it (restored after behaviors)."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        a = float(params.get("angle_deg"))
    except Exception:
        return {"ok": False, "error": "angle_deg (-22..45) required"}
    return s.head(a)


def _body_lift(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Set fork lift height 0.0 (down) .. 1.0 (up)."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        r = float(params.get("ratio"))
    except Exception:
        return {"ok": False, "error": "ratio (0.0..1.0) required"}
    return s.lift(r)


def _body_dock(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """NATIVE dock seat (drive_on_charger). Reliable last-3cm. ~55s from distance."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    return s.dock()


def _body_undock(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Drive off the charger."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    return s.undock()


register_tool("body_open", "SEAT myself in Vector: open ONE held control session + live camera feed (coexists with observe daemon). Idempotent. Then use body_look/drive/turn/... without jumping out.", 1, _body_open)
register_tool("body_close", "Un-seat: stop, release control, disconnect. Stock brain resumes.", 1, _body_close)
register_tool("body_status", "Body session status + REAL battery (SDK is_charging) + wheels/head/reflex + nerves.", 1, _body_status)
register_tool("body_look", "MY EYES (instant): sample the live camera feed -> jpg path to Read. No stream open/close. Needs body_open.", 1, _body_look)
register_tool("body_drive", "Continuous raw drive (NO head reset), deadman+edge guarded. lw/rw mm/s (max120), ttl s (auto-stops). lw=rw>0 fwd, lw=-rw spin.", 1, _body_drive)
register_tool("body_stop", "Stop all Vector motion now.", 1, _body_stop)
register_tool("body_turn", "Gyro-EXACT turn in place. angle_deg +left/-right. Restores head after.", 1, _body_turn)
register_tool("body_straight", "Encoder-EXACT straight. dist_mm +fwd/-back. Cliff-safe. Restores head.", 1, _body_straight)
register_tool("body_pose", "Drive to a pose (x,y mm, angle_deg heading), relative to current by default.", 1, _body_pose)
register_tool("body_head", "Set Vector head pitch (-22 down..45 up); remembered + restored after behaviors.", 1, _body_head)
register_tool("body_lift", "Set Vector fork lift 0.0 (down)..1.0 (up).", 1, _body_lift)
register_tool("body_dock", "NATIVE dock seat (drive_on_charger). Reliable. ~55s from distance.", 1, _body_dock)
register_tool("body_undock", "Drive Vector off the charger.", 1, _body_undock)
