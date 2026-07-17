# SELF_ASSESSMENT: I am Iris's mission controls — fire-and-forget body missions
# (go/route/scan/dock) that run in the pilot thread while my cognition stays
# free to hear and answer. The fix for "deaf while driving" (Zeke 2026-07-16).
"""Pilot mission tools — thin wrappers over brain/vector_pilot.py.

Usage shape (L3 = me):
    body_go {"bearing_deg": 0, "dist_mm": 300}     -> returns INSTANTLY
    ... my turn ends; ears/senses drain; I converse ...
    [VECTOR PILOT] nudge arrives: arrived/blocked  -> I decide the next goal
    body_pilot {}                                  -> live mission + events
    body_abort {}                                  -> stop NOW (preempts)

All motion still rides the L1 safety net (edge-guard, ToF prox-brake, speed
scaling near objects) inside servo_to — the pilot never bypasses it.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _pilot():
    from brain import vector_pilot
    return vector_pilot


def _num(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


def _body_go(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Background servo to a target. Params: x+y (abs mm, or relative=true) OR
    bearing_deg+dist_mm (relative to current heading); optional standoff_mm,
    max_speed, timeout_s. Returns immediately; outcome arrives as a nudge."""
    m = {"kind": "servo",
         "x": _num(params.get("x")), "y": _num(params.get("y")),
         "bearing_deg": _num(params.get("bearing_deg")),
         "dist_mm": _num(params.get("dist_mm")),
         "standoff_mm": _num(params.get("standoff_mm"), 25.0),
         "max_speed": _num(params.get("max_speed")),
         "timeout_s": _num(params.get("timeout_s"), 20.0),
         "relative": bool(params.get("relative")),
         "avoid": bool(params.get("avoid", True)),
         "max_detours": int(_num(params.get("max_detours"), 2) or 2)}
    return _pilot().start_mission(m)


def _body_route(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Background waypoint route. Params: points=[[x,y],...] (abs mm),
    optional standoff_mm/max_speed/timeout_s (per leg)."""
    pts = params.get("points")
    if not isinstance(pts, list) or not pts:
        return {"ok": False, "error": "points=[[x,y],...] required"}
    return _pilot().start_mission(
        {"kind": "route", "points": pts,
         "standoff_mm": _num(params.get("standoff_mm"), 30.0),
         "max_speed": _num(params.get("max_speed")),
         "timeout_s": _num(params.get("timeout_s"), 20.0),
         "avoid": bool(params.get("avoid", True)),
         "max_detours": int(_num(params.get("max_detours"), 2) or 2)})


def _body_retrace(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """ESCAPE THE WAY I CAME: background retrace of my own breadcrumb trail
    backwards (the known-clear path) — the dead-end recovery move. params:
    steps (crumbs to walk back, default 12, max 40), timeout_s per leg."""
    return _pilot().start_mission(
        {"kind": "retrace",
         "steps": int(_num(params.get("steps"), 12) or 12),
         "timeout_s": _num(params.get("timeout_s"), 15.0)})


def _body_scan(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Background 360° rotate-survey: N steps (default 8), fused ToF+heading
    sample per step, frames=true to save a camera frame per step. Output in
    the scan_done event = polar sketch of surroundings from where I stand."""
    return _pilot().start_mission(
        {"kind": "scan", "steps": int(_num(params.get("steps"), 8) or 8),
         "frames": bool(params.get("frames"))})


def _body_park(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Background DOCK (drive_on_charger) in the pilot thread — the dock-wedge
    scar means this call can hang; contained there, my turn never blocks."""
    return _pilot().start_mission({"kind": "dock"})


def _body_launch(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Background UNDOCK (drive_off_charger) in the pilot thread."""
    return _pilot().start_mission({"kind": "undock"})


def _body_pilot(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Pilot status: state (idle/running), current mission, last 10 events."""
    return _pilot().status()


def _body_abort(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """STOP the current mission now (wheels zeroed). Also what a new mission
    does implicitly (preempt) — this is the explicit brake."""
    return _pilot().abort()


register_tool("body_go", "PILOT: background servo to target (x,y | bearing_deg+dist_mm) — returns instantly, outcome nudges me. Obstacle DETOURS built in (avoid=true default, max_detours=2): blocked/stuck -> back off, ToF-probe both sides, sidestep, retry. params: see doc", 2, _body_go)
register_tool("body_route", "PILOT: background waypoint route with obstacle detours (avoid/max_detours like body_go). params: points=[[x,y],...]", 2, _body_route)
register_tool("body_retrace", "PILOT: escape the way I came — walk my own breadcrumb trail backwards (known-clear path, detours off). params: steps (default 12), timeout_s", 2, _body_retrace)
register_tool("body_scan", "PILOT: background 360 survey (ToF+heading polar sketch, frames opt). params: steps, frames", 2, _body_scan)
register_tool("body_park", "PILOT: background dock on charger (wedge-safe: hangs a thread, not my turn)", 2, _body_park)
register_tool("body_launch", "PILOT: background undock from charger", 2, _body_launch)
register_tool("body_pilot", "PILOT: mission status + recent events", 1, _body_pilot)
register_tool("body_abort", "PILOT: stop current mission NOW", 2, _body_abort)
