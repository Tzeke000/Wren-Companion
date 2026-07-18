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

import threading
import time
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
        if ch is None or getattr(ch, "pose", None) is None:
            # 2026-07-17: post-reboot the charger OBJECT can exist with pose
            # None (half-initialized world state) — treat as not-known.
            return {"ok": True, "charger_known": False,
                    "note": "engine has NOT localized the charger this "
                            "connection (object may exist with no pose) — "
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


def _body_overhead(params: dict, g: dict) -> dict:
    """OVERHEAD-EYE PROBE (2026-07-17): one frame from the PC camera (shared
    device path — no fight with perception) + ArUco marker detection. Answers
    'can the PC cam localize my body on the desk?' Read the saved jpg to judge
    the view. Markers appear once the tags are printed; px→mm needs the
    stage-2 calibration (Zeke-present)."""
    from brain import vector_overhead
    return vector_overhead.probe(save=bool(params.get("save", True)))


def _body_cones(params: dict, g: dict) -> dict:
    """CONE REFEREE (2026-07-17: Zeke watched me drag cone 1 to cone 4 —
    thin cones are invisible to ToF and dragging never trips stuck-detect).
    Overhead-webcam orange-cone snapshot + drift compare: call BEFORE a
    drive leg to baseline, AFTER to self-grade. moved=[] = clean pass."""
    from brain import vector_overhead
    return vector_overhead.cone_check(
        save=bool(params.get("save", True)),
        drift_px=float(params.get("drift_px", 12.0)),
        sample=params.get("sample"))


def _guarded_behavior(s, fn, label: str, timeout_s: float) -> dict:
    """Run a blocking SDK behavior in a side thread with a join deadline —
    the dock-hang lesson (2026-07-17) applied to cube maneuvers. Suspends
    expressive reflexes + yields guard control for the duration: a looming
    cube mid-dock trips startle exactly like the looming charger did."""
    out: dict = {}
    prev = getattr(s, "_reflex_on", True)
    s._reflex_on = False
    s._yield_control_until = time.time() + timeout_s + 5.0
    try:
        def _run():
            try:
                out["ok"] = True
                out["result"] = str(fn())[:200]
            except Exception as e:
                out["ok"] = False
                out["error"] = repr(e)[:250]
        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout_s)
        if th.is_alive():
            return {"ok": False, "hung": True,
                    "note": f"{label} still blocking after {timeout_s:.0f}s — "
                            f"detached. body_close + body_open before ANY "
                            f"other SDK behavior (the dock-hang rule)."}
        return out
    finally:
        s._reflex_on = prev
        s._yield_control_until = 0.0


def _cube_of(s):
    return getattr(s.robot.world, "connected_light_cube", None)


def _body_cube(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """MY HANDS (built live-test morning 2026-07-17 — Zeke: 'your cube ...
    you can test with on your own'). The cube is Vector's only native
    manipulable object: find/dock/lift/carry/place ride firmware behaviors.
    actions: status (default) | connect | disconnect | lights | dock |
    pickup | place | roll. Motion actions are hang-guarded; cube must be
    CONNECTED (BLE) for behaviors — needs its N/LR1 battery in."""
    s = _live_session()
    if s is None:
        return {"ok": False, "error": "no body session open"}
    action = str(params.get("action") or "status").lower()
    w = s.robot.world
    try:
        if action == "status":
            c = _cube_of(s)
            if c is None:
                return {"ok": True, "connected": False,
                        "note": "no cube connected — body_cube action=connect "
                                "(cube needs its battery; BLE takes ~5s)"}
            d = {"ok": True, "connected": True,
                 "factory_id": str(getattr(c, "factory_id", "?"))}
            try:
                d["is_visible"] = bool(c.is_visible)
                d["last_seen_s_ago"] = round(float(
                    getattr(c, "time_since_last_seen", -1.0)), 1)
            except Exception:
                pass
            try:
                p = c.pose
                d["pose"] = {"x_mm": round(float(p.position.x), 1),
                             "y_mm": round(float(p.position.y), 1),
                             "origin_id": int(getattr(p, "origin_id", -1))}
            except Exception:
                pass
            return d
        if action == "connect":
            r = _guarded_behavior(s, w.connect_cube, "connect_cube", 20.0)
            if r.get("ok"):
                c = _cube_of(s)
                r["connected"] = c is not None
                if c is None:
                    r["note"] = ("connect returned but no cube attached — "
                                 "battery in? (N/LR1 1.5V) close enough? "
                                 "try again once")
            return r
        if action == "disconnect":
            return _guarded_behavior(s, w.disconnect_cube, "disconnect_cube", 10.0)
        if action == "lights":
            return _guarded_behavior(s, w.flash_cube_lights, "flash_cube_lights", 10.0)
        # ---- motion actions need the connected cube object
        c = _cube_of(s)
        if c is None and action in ("dock", "pickup", "roll"):
            return {"ok": False, "error": "no cube connected — body_cube "
                                          "action=connect first"}
        b = s.robot.behavior
        if action == "dock":
            return _guarded_behavior(
                s, lambda: b.dock_with_cube(c, num_retries=2), "dock_with_cube", 45.0)
        if action == "pickup":
            return _guarded_behavior(
                s, lambda: b.pickup_object(c, num_retries=2), "pickup_object", 60.0)
        if action == "place":
            return _guarded_behavior(
                s, lambda: b.place_object_on_ground_here(0),
                "place_object_on_ground_here", 30.0)
        if action == "roll":
            return _guarded_behavior(
                s, lambda: b.roll_cube(c, num_retries=2), "roll_cube", 60.0)
        return {"ok": False, "error": f"unknown action '{action}' — use status/"
                                      f"connect/disconnect/lights/dock/pickup/"
                                      f"place/roll"}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:250]}


def _body_landmarks(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """MY ROOM MARKERS (Zeke placed 2026-07-17, HIS layout — three PAIRS,
    wall marker at exactly 100mm height + same-shape floor marker directly
    below: BED POST = Diamonds2(wall)+Diamonds3(floor); DRESSER-BY-COUCH =
    Circles2(wall)+Circles3(floor), best webcam visibility; CHARGER WALL =
    Triangles4(wall)+Triangles5(floor). 90mm symbol squares. Floor markers
    face UP — mainly for the overhead webcam; wall markers are MINE.
    They never move = absolute landmarks.)
    action='define' registers all SIX as unique custom walls (per-connection
    — re-define after every body_open). action='list' (default) returns every
    custom object the engine knows, with pose + freshness."""
    s = _live_session()
    if s is None:
        return {"ok": False, "error": "no body session open"}
    action = str(params.get("action") or "list").lower()
    try:
        from brain import vector_session as vs
        if action == "define":
            # ALL-16 define set (2026-07-17 mystery solved): engine log proved a
            # marker outside the assumed 6-layout was physically in the room
            # ('MARKER_SDK_3TRIANGLES' observed, "No objects in library"). We
            # now define every printable marker so the ENGINE surveys for us.
            # Single source of truth: vector_session.define_all_markers (also
            # auto-runs at body_open).
            md = vs.define_all_markers(s.robot)
            out: dict[str, Any] = {"ok": not md.get("errors"),
                                   "defined": md.get("defined"),
                                   "note": "all 16 printable markers defined — "
                                           "any placed sheet now self-identifies "
                                           "in action=list when seen"}
            if md.get("errors"):
                out["errors"] = md["errors"]
            return out
        # ---- list: what does the engine know right now?
        # all=true drops the Custom filter (shows cube/charger too — useful to
        # prove the Markers pipeline is live this session via native objects)
        show_all = bool(params.get("all"))
        objs = []
        for o in list(getattr(s.robot.world, "all_objects", []) or []):
            tname = type(o).__name__
            if not show_all and "Custom" not in tname:
                continue
            d: dict[str, Any] = {"class": tname}
            with __import__("contextlib").suppress(Exception):
                # instance nests its type under .archetype (SDK CustomObject);
                # fall back to a direct attr for archetype objects themselves
                ct = getattr(getattr(o, "archetype", None), "custom_type", None) \
                    or getattr(o, "custom_type", "")
                d["object_type"] = str(ct)[:40]
                d["marker"] = vs.marker_name_for_type(ct)
            with __import__("contextlib").suppress(Exception):
                d["is_visible"] = bool(o.is_visible)
                d["last_seen_s_ago"] = round(float(
                    getattr(o, "time_since_last_seen", -1.0)), 1)
            with __import__("contextlib").suppress(Exception):
                p = o.pose
                d["pose"] = {"x_mm": round(float(p.position.x), 1),
                             "y_mm": round(float(p.position.y), 1),
                             "heading_deg": round(float(p.rotation.angle_z.degrees), 1),
                             "origin_id": int(getattr(p, "origin_id", -1))}
            objs.append(d)
        return {"ok": True, "landmarks": objs, "count": len(objs),
                "note": "empty = none seen yet this connection (define first, "
                        "then LOOK at a marker)"}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:250]}


register_tool("body_marker_vision", "Enable firmware marker detection on the live session (charger/cube/custom fiducials)", 2, _body_marker_vision)
register_tool("body_landmarks", "My ROOM MARKERS (Circles2/3, Diamonds2/3, 90mm): action=define registers them as unique walls (per-connection); action=list shows known landmark poses", 2, _body_landmarks)
register_tool("body_cube", "MY HANDS — cube find/dock/pickup/place/roll via firmware behaviors (hang-guarded). actions: status/connect/disconnect/lights/dock/pickup/place/roll", 2, _body_cube)
register_tool("body_charger", "Engine's known charger pose — MUST be known before body_park (unseen charger = dock hang)", 1, _body_charger)
register_tool("body_overhead", "OVERHEAD-EYE probe: PC-camera frame + ArUco marker detection (localization stage 1). Read the saved jpg to judge whether the view covers my driving area.", 1, _body_overhead)
register_tool("body_cones", "CONE REFEREE via overhead webcam: snapshot orange-cone positions + drift-compare vs last snapshot (drag detector). Baseline before a leg, self-grade after.", 1, _body_cones)
