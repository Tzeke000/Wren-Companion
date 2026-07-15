"""vector_cone_box_demo.py — drive among the cones + push the box (2026-07-14).

Zeke: "you still owe me moving around the cones and moving the box." The light
cube can't BLE-connect until it's charged (weekend), so no fork-lift pickup —
but I can PUSH the box by driving into it, and weave the cones with the precise
primitives. Behaviors ONLY (no camera/navmap feeds in a control session — that
hangs the behavior API); the observe daemon maps in parallel. Modest distances
to stay inside the arena. Redocks at the end.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FRAME = REPO / "state" / "vector"
from brain import vector_action as va


def log(m):
    print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def nerves():
    try:
        return json.loads((FRAME / "nerves.json").read_text())
    except Exception:
        return {}


def main():
    from anki_vector.util import Pose, degrees
    log("connecting (behaviors-only)...")
    with va.control_session() as robot:
        p = robot.pose
        x0, y0, th0 = p.position.x, p.position.y, p.rotation.angle_z.degrees
        log(f"start pose x={x0:.0f} y={y0:.0f} th={th0:.0f}")

        log("undock...")
        log(f"  {va.drive_off_charger(robot)}")
        time.sleep(0.4)

        # 1) drive forward between the middle cones toward the box + PUSH it
        log("driving between the cones toward the box, pushing it forward...")
        log(f"  {va.drive_straight(robot, 430, 90)}")   # thread middle gap, nudge box
        n = nerves()
        log(f"  nerves prox={n.get('prox_mm')} cliff={n.get('cliff')}")

        # 2) back out
        log("backing out of the cone line...")
        log(f"  {va.drive_straight(robot, -230, 90)}")

        # 3) weave/slalom past the cones
        log("slalom through the cones...")
        for i, (ang, dist) in enumerate([(-32, 240), (64, 240), (-64, 220), (32, 160)]):
            log(f"  leg {i}: turn {ang} -> fwd {dist}")
            va.turn_in_place(robot, ang, 90)
            r = va.drive_straight(robot, dist, 90)
            if not r.get("ok"):
                log(f"    stopped: {r}")
                break

        # 4) head home — go back toward the start pose, then native seat
        log("returning toward the dock...")
        r = robot.behavior.go_to_pose(
            Pose(x=x0, y=y0, z=0.0, angle_z=degrees(th0)),
            relative_to_robot=False, num_retries=2)
        log(f"  go_to_start: {getattr(r,'result',r)}")
        log("drive_on_charger (native seat)...")
        rd = va.drive_on_charger(robot)
        log(f"  dock: {rd}")
        time.sleep(1.0)
        try:
            bs = robot.get_battery_state()
            log(f"  battery volts={bs.battery_volts:.3f} on_charger={bs.is_on_charger_platform} "
                f"charging={bs.is_charging}")
        except Exception as e:
            log(f"  battery: {e!r}")
    log("done.")


if __name__ == "__main__":
    main()
