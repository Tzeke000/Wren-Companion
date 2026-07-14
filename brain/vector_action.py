"""vector_action.py — Iris's PRECISE-MOTION layer for the Vector body (2026-07-14).

The wire-pod /api-sdk path only proxies RAW motion (move_wheels/head/lift) —
that's what forced all of 2026-07-14's timed-burst drift fighting (short
bursts don't rotate, long ones overshoot, the last-3cm dock seat never
threaded by hand). Vector's REAL kit is the anki_vector *behavior* API:
encoder/gyro-exact primitives (turn_in_place, drive_straight, go_to_pose)
and native docking (drive_on_charger — the reliable seat).

CONNECTION MODEL (proven by scripts/test_vector_control_conn.py, 2026-07-14):
  A control-holding direct-SDK connection OPENS in ~0.7s ALONGSIDE the
  inhabit daemon's observe connection, and the observe daemon SURVIVES it
  (nerves.json keeps flowing). So this layer uses ON-DEMAND control sessions:
  connect(control) -> run precise behavior(s) -> disconnect. The observe
  daemon (nerves/ears/battery) is never touched. For a SEQUENCE (figure-8),
  one session holds control across several behaviors so the pose frame stays
  consistent.

SAFETY: behaviors have native cliff avoidance (Vector won't drive off an
edge on a behavior). We ALSO refuse translational moves when the nerve
daemon reports picked_up (no surface under the treads). Non-translational
moves (head/lift/turn/eye) are always allowed.
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERIAL = "0dd1cdaf"
NERVES = REPO / "state" / "vector" / "nerves.json"
LAST_SPOKE = REPO / "state" / "vector" / "last_spoke.json"

# behavior limits (Vector hardware)
HEAD_MIN_DEG, HEAD_MAX_DEG = -22.0, 45.0   # set_head_angle range
LIFT_MIN, LIFT_MAX = 0.0, 1.0              # set_lift_height ratio


def _nerves(max_age_s: float = 2.5) -> dict:
    try:
        d = json.loads(NERVES.read_text(encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) <= max_age_s:
            return d
    except Exception:
        pass
    return {}


@contextlib.contextmanager
def control_session(timeout: float = 20.0):
    """Open an on-demand direct-SDK connection WITH behavior control, yield the
    anki_vector Robot, and always disconnect. Proven to coexist with the observe
    daemon. cache_animation_lists=False keeps connect ~0.7s."""
    import anki_vector
    robot = anki_vector.Robot(SERIAL, cache_animation_lists=False,
                              default_logging=False)
    robot.connect(timeout=timeout)
    try:
        yield robot
    finally:
        with contextlib.suppress(Exception):
            robot.disconnect()


def _behavior_result(res) -> dict:
    """Normalize an anki_vector behavior result into a plain dict."""
    out = {"ok": True}
    try:
        # BehaviorResult / ActionResult expose .result (a status enum)
        r = getattr(res, "result", res)
        out["result"] = str(r)
        # many results are truthy on success; a failed action often has a
        # non-success code in its string form
        s = str(r).lower()
        if any(bad in s for bad in ("fail", "abort", "cancel", "timeout",
                                    "not_started", "no_charger", "err")):
            out["ok"] = False
    except Exception as e:
        out["result"] = f"<unreadable: {e!r}>"
    return out


# ---------------------------------------------------------------- primitives

def turn_in_place(robot, angle_deg: float, speed_deg_s: float = 90.0) -> dict:
    """Gyro-exact rotation. +angle = counter-clockwise (left)."""
    from anki_vector.util import degrees
    res = robot.behavior.turn_in_place(
        degrees(float(angle_deg)),
        speed=degrees(float(speed_deg_s)),
        angle_tolerance=degrees(2.0))
    out = _behavior_result(res)
    out["angle_deg"] = angle_deg
    return out


def drive_straight(robot, dist_mm: float, speed_mm_s: float = 100.0) -> dict:
    """Encoder-exact straight line. +dist forward, -dist backward. picked_up guard."""
    n = _nerves()
    if n.get("picked_up"):
        return {"ok": False, "refused": "picked up / no surface under treads"}
    from anki_vector.util import distance_mm, speed_mmps
    res = robot.behavior.drive_straight(
        distance_mm(float(dist_mm)), speed_mmps(abs(float(speed_mm_s))))
    out = _behavior_result(res)
    out["dist_mm"] = dist_mm
    return out


def go_to_pose(robot, x: float, y: float, angle_deg: float = 0.0,
               relative: bool = True) -> dict:
    """Drive to a pose (x,y in mm, heading angle_deg). relative=True means
    relative to the robot's CURRENT pose (the sane default for waypoints);
    False = absolute in the session's pose frame."""
    n = _nerves()
    if n.get("picked_up"):
        return {"ok": False, "refused": "picked up / no surface under treads"}
    from anki_vector.util import Pose, degrees
    pose = Pose(x=float(x), y=float(y), z=0.0, angle_z=degrees(float(angle_deg)))
    res = robot.behavior.go_to_pose(pose, relative_to_robot=relative,
                                    num_retries=1)
    out = _behavior_result(res)
    out.update({"x": x, "y": y, "angle_deg": angle_deg, "relative": relative})
    return out


def drive_on_charger(robot) -> dict:
    """NATIVE reliable dock seat — the last-3cm I could never thread by hand."""
    res = robot.behavior.drive_on_charger()
    return _behavior_result(res)


def drive_off_charger(robot) -> dict:
    res = robot.behavior.drive_off_charger()
    return _behavior_result(res)


def set_head_angle(robot, angle_deg: float) -> dict:
    """Precise head pitch. -22 (down) .. +45 (up)."""
    from anki_vector.util import degrees
    a = max(HEAD_MIN_DEG, min(HEAD_MAX_DEG, float(angle_deg)))
    res = robot.behavior.set_head_angle(degrees(a))
    out = _behavior_result(res)
    out["head_deg"] = a
    return out


def set_lift_height(robot, ratio: float) -> dict:
    """Precise lift. 0.0 (down) .. 1.0 (up)."""
    r = max(LIFT_MIN, min(LIFT_MAX, float(ratio)))
    res = robot.behavior.set_lift_height(r)
    out = _behavior_result(res)
    out["lift_ratio"] = r
    return out


# ---------------------------------------------------------------- one-shots

def _oneshot(fn, *a, **kw) -> dict:
    """Open a control session, run one primitive, close. Returns the primitive's
    dict plus connect timing."""
    t0 = time.time()
    try:
        with control_session() as robot:
            connect_s = time.time() - t0
            out = fn(robot, *a, **kw)
            out["connect_s"] = round(connect_s, 2)
            return out
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


# convenience one-shot wrappers (the tool layer calls these)
def do_turn(angle_deg, speed_deg_s=90.0):
    return _oneshot(turn_in_place, angle_deg, speed_deg_s)


def do_straight(dist_mm, speed_mm_s=100.0):
    return _oneshot(drive_straight, dist_mm, speed_mm_s)


def do_go_to_pose(x, y, angle_deg=0.0, relative=True):
    return _oneshot(go_to_pose, x, y, angle_deg, relative)


def do_dock():
    return _oneshot(drive_on_charger)


def do_undock():
    return _oneshot(drive_off_charger)


def do_head(angle_deg):
    return _oneshot(set_head_angle, angle_deg)


def do_lift(ratio):
    return _oneshot(set_lift_height, ratio)


# ---------------------------------------------------------------- sequences

_PRIMS = {
    "turn": turn_in_place,
    "straight": drive_straight,
    "pose": go_to_pose,
    "dock": drive_on_charger,
    "undock": drive_off_charger,
    "head": set_head_angle,
    "lift": set_lift_height,
}


def run_sequence(steps: list) -> dict:
    """Hold ONE control session across a list of primitives so the pose frame
    stays consistent (figure-8 = go_to_pose waypoints). Each step is a dict:
      {"op":"turn","angle_deg":90}
      {"op":"straight","dist_mm":100}
      {"op":"pose","x":100,"y":0,"angle_deg":45,"relative":true}
      {"op":"dock"} / {"op":"undock"} / {"op":"head","angle_deg":10} ...
    Stops early on the first failed step (returns what ran)."""
    results = []
    t0 = time.time()
    try:
        with control_session() as robot:
            for i, step in enumerate(steps):
                op = str(step.get("op", "")).strip().lower()
                fn = _PRIMS.get(op)
                if fn is None:
                    results.append({"ok": False, "step": i,
                                    "error": f"unknown op {op!r}"})
                    break
                kw = {k: v for k, v in step.items() if k != "op"}
                try:
                    r = fn(robot, **kw)
                except TypeError as e:
                    r = {"ok": False, "error": f"bad args for {op}: {e!r}"}
                r["step"] = i
                r["op"] = op
                results.append(r)
                if not r.get("ok"):
                    break
        return {"ok": all(r.get("ok") for r in results),
                "steps_run": len(results),
                "total_s": round(time.time() - t0, 2),
                "results": results}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300], "results": results}
