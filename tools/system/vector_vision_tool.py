# SELF_ASSESSMENT: I am Iris's marker-vision switch — turns on the firmware's
# fiducial detection for the LIVE body session (charger home-marker, cube,
# custom wall/cone markers) and reports what the engine currently sees.
"""Marker-vision tools — 2026-07-16 late (the dock saga).

Root cause of the drive_on_charger hangs: marker detection is per-CONNECTION;
my held session never enabled it, so the engine never populated
robot.world.charger on my connection and the dock behavior waited forever for
a charger it could not see. body_marker_vision enables it live (no reconnect)
and body_charger reports the known charger pose — the pre-dock sanity check:
NEVER body_park unless body_charger says the charger is known/seen.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _live_session():
    from brain import vector_session
    s = vector_session.get_session(create=False)
    if s is None or not getattr(s, "connected", False) or s.robot is None:
        return None
    return s


def _body_marker_vision(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Enable the firmware's marker detection on the LIVE session (idempotent).
    Makes robot.world.charger (+ cube + custom markers) populate when seen."""
    s = _live_session()
    if s is None:
        return {"ok": False, "error": "no body session open"}
    try:
        # SDK 0.8.1: the switch is enable_custom_object_detection (enables the
        # Markers vision mode — charger + cube + custom fiducials all ride it).
        # enable_marker_detection only exists in newer SDK forks.
        v = s.robot.vision
        if hasattr(v, "enable_marker_detection"):
            try:
                v.enable_marker_detection(detect_markers=True)
            except TypeError:
                v.enable_marker_detection()
        else:
            v.enable_custom_object_detection(True)
        return {"ok": True, "marker_vision": "on"}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:250]}


def _body_charger(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Where does the engine think the charger is? None = NEVER body_park (the
    dock behavior hangs forever hunting an unseen charger — 23:52 scar)."""
    s = _live_session()
    if s is None:
        return {"ok": False, "error": "no body session open"}
    try:
        ch = s.robot.world.charger
        if ch is None:
            return {"ok": True, "charger_known": False,
                    "note": "engine has NOT seen the charger this connection — "
                            "face it with marker vision on, then re-check"}
        p = ch.pose
        return {"ok": True, "charger_known": True,
                "pose": {"x_mm": round(float(p.position.x), 1),
                         "y_mm": round(float(p.position.y), 1),
                         "heading_deg": round(float(p.rotation.angle_z.degrees), 1),
                         "origin_id": int(getattr(p, "origin_id", -1))},
                "last_seen_s_ago": round(float(getattr(ch, "time_since_last_seen", -1)), 1)}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:250]}


register_tool("body_marker_vision", "Enable firmware marker detection on the live session (charger/cube/custom fiducials)", 2, _body_marker_vision)
register_tool("body_charger", "Engine's known charger pose — MUST be known before body_park (unseen charger = dock hang)", 1, _body_charger)
