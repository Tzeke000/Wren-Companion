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


def _body_goto(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """MAP-AWARE GOTO: A* through the room blueprint + hazard memory, routing
    AROUND known obstacles; falls back to direct servo-with-detours if no
    usable map. params: x, y (abs mm, required); standoff_mm, max_speed,
    timeout_s (per leg), avoid, max_detours."""
    if params.get("x") is None or params.get("y") is None:
        return {"ok": False, "error": "x and y (abs mm) required"}
    return _pilot().start_mission(
        {"kind": "goto", "x": _num(params.get("x")), "y": _num(params.get("y")),
         "standoff_mm": _num(params.get("standoff_mm"), 30.0),
         "max_speed": _num(params.get("max_speed")),
         "timeout_s": _num(params.get("timeout_s"), 20.0),
         "avoid": bool(params.get("avoid", True)),
         "max_detours": int(_num(params.get("max_detours"), 2) or 2)})


def _body_explore(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """FRONTIER EXPLORATION: repeatedly goto the nearest known-clear/unknown
    boundary + 360 survey so the nav-map daemon absorbs new territory. The
    map grows as I move. params: targets (default 3), timeout_s (overall)."""
    return _pilot().start_mission(
        {"kind": "explore",
         "targets": int(_num(params.get("targets"), 3) or 3),
         "timeout_s": _num(params.get("timeout_s"), 180.0),
         "max_speed": _num(params.get("max_speed")),
         "avoid": True, "max_detours": 2})


def _body_park_smart(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """SMART-PARK: Zeke's docking lesson automated — optional approach servo to
    a staging point (x,y near home), then release possession + close my session
    and let the STOCK brain do the parking it's 4x better at; re-possess once
    docked (or timeout). Ends with the session CLOSED — body_open to re-seat.
    params: x, y (optional staging point), wait_s (default 240)."""
    return _pilot().start_mission(
        {"kind": "smart_park",
         "x": _num(params.get("x")), "y": _num(params.get("y")),
         "standoff_mm": _num(params.get("standoff_mm"), 60.0),
         "timeout_s": _num(params.get("timeout_s"), 25.0),
         "wait_s": _num(params.get("wait_s"), 240.0)})


def _body_hazards(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """HAZARD MEMORY: the journal of every blocked/stuck/detour location
    (pose + origin frame). The planner routes around them. params: origin
    (filter to a pose frame), limit (default 40); clear=true wipes the file."""
    from brain import vector_pilot as vp
    if params.get("clear"):
        try:
            vp.HAZARDS.unlink(missing_ok=True)
            return {"ok": True, "cleared": True}
        except Exception as e:
            return {"ok": False, "error": repr(e)[:150]}
    hz = vp.read_hazards(origin=params.get("origin"))
    limit = int(_num(params.get("limit"), 40) or 40)
    return {"ok": True, "count": len(hz), "hazards": hz[-limit:]}


def _body_pilot(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Pilot status: state (idle/running), current mission, last 10 events."""
    return _pilot().status()


def _session_or_err():
    from brain import vector_session
    s = vector_session.get_session(create=False)
    if s is None or not s.connected:
        return None
    return s


def _body_pose_truth(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """POSE TRUTH: how much to trust my odometry right now (confidence 0..1,
    drift budget since last absolute fix, advice). fix=true attempts an
    absolute fix NOW — charger first, then ANY freshly-seen room landmark
    (Zeke's six permanent markers, 2026-07-17). Needs a FRESH sighting on
    the live session (face a marker, then fix)."""
    from brain import vector_pose
    out = vector_pose.status()
    if params.get("fix"):
        s = _session_or_err()
        if s is None:
            out["fix_attempt"] = {"ok": False, "error": "body session not open"}
        else:
            fixer = getattr(vector_pose, "try_landmark_fix",
                            vector_pose.try_charger_fix)
            out["fix_attempt"] = fixer(s)
            if out["fix_attempt"].get("ok"):
                out.update(vector_pose.status())
    return out


def _body_macro(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """ONE-PRESS MANEUVERS (Zeke 2026-07-17: 'hands on the controller — one
    press away, active and live'). name=
      look_around  — head sweep, 3 frames (safe anywhere, incl. docked)
      peek         — turn +45/frame, -90/frame, +45 back (2 frames)
      back_off     — reverse 80mm
      face_home    — turn toward the charger (bearing from live nerves)
      square_up    — rotate to the nearest 90° heading
    Wheel macros refuse while docked (body_launch first)."""
    import time as _t
    s = _session_or_err()
    if s is None:
        return {"ok": False, "error": "body session not open (call body_open)"}
    name = str(params.get("name") or "").strip()
    st = dict(getattr(s, "_latest", {}) or {})
    wheel_macros = {"peek", "back_off", "face_home", "square_up"}
    if name in wheel_macros and st.get("on_charger"):
        return {"ok": False, "refused": "docked — body_launch before wheel "
                                        "macros (turning on the contacts is "
                                        "bad for both of us)"}
    if name == "look_around":
        frames = []
        for i, ang in enumerate((-15.0, 10.0, 35.0)):
            s.head(ang)
            _t.sleep(0.4)
            r = s.look(name=f"macro_look_{i}")
            if r.get("ok"):
                frames.append(r["path"])
        return {"ok": True, "macro": name, "frames": frames}
    if name == "peek":
        frames = []
        s.turn(45.0)
        _t.sleep(0.3)
        r = s.look(name="macro_peek_left")
        if r.get("ok"):
            frames.append(r["path"])
        s.turn(-90.0)
        _t.sleep(0.3)
        r = s.look(name="macro_peek_right")
        if r.get("ok"):
            frames.append(r["path"])
        s.turn(45.0)
        return {"ok": True, "macro": name, "frames": frames,
                "note": "left frame first, then right; heading restored"}
    if name == "back_off":
        return {"ok": True, "macro": name, "res": s.straight(-80.0, 100.0)}
    if name == "face_home":
        try:
            import json as _json
            from pathlib import Path as _P
            n = _json.loads((_P(r"D:\Wren-Companion\state\vector\nerves.json")
                             ).read_text(encoding="utf-8"))
            if not n.get("charger_seen"):
                return {"ok": False, "error": "charger bearing unknown (engine "
                                              "hasn't seen the dock) — "
                                              "body_scan or sight it first"}
            b = float(n.get("charger_bearing_deg") or 0.0)
        except Exception as e:
            return {"ok": False, "error": f"nerves unreadable: {e!r}"[:150]}
        return {"ok": True, "macro": name, "turned": b, "res": s.turn(b)}
    if name == "square_up":
        h = st.get("heading")
        if h is None:
            return {"ok": False, "error": "no heading in stream yet"}
        target = round(float(h) / 90.0) * 90.0
        delta = ((target - float(h) + 180.0) % 360.0) - 180.0
        return {"ok": True, "macro": name, "delta": round(delta, 1),
                "res": s.turn(delta) if abs(delta) > 1.5 else {"ok": True,
                                                               "already": True}}
    return {"ok": False, "error": f"unknown macro '{name}' — see tool doc",
            "macros": ["look_around", "peek", "back_off", "face_home",
                       "square_up"]}


def _body_abort(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """STOP the current mission now (wheels zeroed). Also what a new mission
    does implicitly (preempt) — this is the explicit brake."""
    return _pilot().abort()


register_tool("body_go", "PILOT: background servo to target (x,y | bearing_deg+dist_mm) — returns instantly, outcome nudges me. Obstacle DETOURS built in (avoid=true default, max_detours=2): blocked/stuck -> back off, ToF-probe both sides, sidestep, retry. params: see doc", 2, _body_go)
register_tool("body_route", "PILOT: background waypoint route with obstacle detours (avoid/max_detours like body_go). params: points=[[x,y],...]", 2, _body_route)
register_tool("body_retrace", "PILOT: escape the way I came — walk my own breadcrumb trail backwards (known-clear path, detours off). params: steps (default 12), timeout_s", 2, _body_retrace)
register_tool("body_goto", "PILOT: MAP-AWARE goto — A* through the room blueprint + hazard memory, routes AROUND known obstacles (falls back to direct servo+detours without a map). params: x, y (abs mm)", 2, _body_goto)
register_tool("body_explore", "PILOT: FRONTIER EXPLORATION — drive to the nearest known/unknown boundary, survey, re-plan as the map grows. Bounded. params: targets (default 3, max 8), timeout_s (overall, default 180)", 2, _body_explore)
register_tool("body_park_smart", "PILOT: SMART-PARK — approach a staging point, then hand the actual parking to the STOCK brain (possession released, session closed, re-possess when docked). Zeke's lesson automated. params: x,y (optional staging), wait_s", 2, _body_park_smart)
register_tool("body_hazards", "PILOT: hazard memory — journal of blocked/stuck/detour locations the planner routes around. params: origin (frame filter), limit, clear=true wipes", 1, _body_hazards)
register_tool("body_pose_truth", "POSE TRUTH: odometry confidence 0..1 + drift budget since last absolute fix + advice; fix=true grabs a charger absolute fix NOW (needs fresh sighting). Charger-anchored SLAM v1.", 1, _body_pose_truth)
register_tool("body_macro", "ONE-PRESS maneuvers: name=look_around (head sweep + 3 frames, dock-safe) | peek (±45° + 2 frames) | back_off | face_home (turn to charger bearing) | square_up (snap to nearest 90°). Wheel macros refuse while docked.", 2, _body_macro)
register_tool("body_scan", "PILOT: background 360 survey (ToF+heading polar sketch, frames opt). params: steps, frames", 2, _body_scan)
register_tool("body_park", "PILOT: background dock on charger (wedge-safe: hangs a thread, not my turn)", 2, _body_park)
register_tool("body_launch", "PILOT: background undock from charger", 2, _body_launch)
register_tool("body_pilot", "PILOT: mission status + recent events", 1, _body_pilot)
register_tool("body_abort", "PILOT: stop current mission NOW", 2, _body_abort)
