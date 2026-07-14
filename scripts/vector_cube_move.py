"""vector_cube_move.py — pick up the cube, move it, put it back (2026-07-14).

Zeke: "try picking up your cube then putting it back." Uses the direct-SDK
native cube kit (wire-pod doesn't proxy it): connect_cube (BLE) -> observe it
-> pickup_object -> drive it somewhere -> place_object_on_ground_here -> pick
up again -> return to the RECORDED origin pose -> place back. One control
session; camera + nav-map captured at each step.

Defensive: the cube must be BLE-connectable (charged, in range). If connect
or observe fails, it reports and bails without flailing.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FRAME = REPO / "state" / "vector"

from brain import vector_action as va
from brain import vector_map as vm


def log(m):
    print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def photo(robot, name):
    try:
        img = robot.camera.latest_image
        if img and img.raw_image:
            p = FRAME / f"cube_{name}.jpg"
            img.raw_image.save(str(p))
            log(f"  photo -> {p.name}")
    except Exception as e:
        log(f"  photo fail: {e!r}")


def snap_map(robot, tag, name):
    r = vm.capture_map(robot, tag=tag)
    if r.get("ok"):
        try:
            shutil.copy(str(vm.MAP_PNG), str(FRAME / f"cube_map_{name}.png"))
        except Exception:
            pass
        log(f"  MAP {name}: {r['cells']} cells {r.get('counts')}")
    return r


def observe_cube(robot, secs=8.0):
    """Look around until the connected cube has a known pose (is observed)."""
    from anki_vector.util import degrees
    cube = robot.world.connected_light_cube
    if cube and getattr(cube, "pose", None) and cube.pose.is_valid:
        return cube
    # sweep the head + a small turn to find it
    va.set_head_angle(robot, 0.0)
    t0 = time.time()
    sweep = [0, -25, 25, -45, 45]
    si = 0
    while time.time() - t0 < secs:
        cube = robot.world.connected_light_cube
        if cube and getattr(cube, "pose", None) and cube.pose.is_valid:
            return cube
        if si < len(sweep):
            try:
                robot.behavior.turn_in_place(degrees(sweep[si] -
                                             (sweep[si-1] if si else 0)))
            except Exception:
                pass
            si += 1
        time.sleep(0.5)
    return robot.world.connected_light_cube


def main():
    log("connecting (control)...")
    with va.control_session() as robot:
        try:
            robot.camera.init_camera_feed()
            robot.nav_map.init_nav_map_feed(frequency=0.5)
        except Exception as e:
            log(f"feed init: {e!r}")
        time.sleep(1.0)
        va.set_head_angle(robot, 0.0)
        photo(robot, "00_start")

        log("connecting cube over BLE...")
        try:
            conn = robot.world.connect_cube()
            log(f"  connect_cube: {conn}")
        except Exception as e:
            log(f"  connect_cube FAILED: {e!r}")
        cube = robot.world.connected_light_cube
        if not cube:
            log("NO CUBE connected — is it charged / in range? bailing.")
            return
        log(f"cube connected: {cube}")
        try:
            cube.set_lights(None)
        except Exception:
            pass

        log("locating cube (looking for its marker)...")
        cube = observe_cube(robot, secs=10.0)
        if not (cube and getattr(cube, "pose", None) and cube.pose.is_valid):
            log("cube not OBSERVED (no valid pose) — can't grasp. bailing.")
            photo(robot, "05_notseen")
            return
        cp = cube.pose
        origin = (cp.position.x, cp.position.y, cp.rotation.angle_z.degrees)
        log(f"cube ORIGIN pose: x={origin[0]:.0f} y={origin[1]:.0f} "
            f"th={origin[2]:.0f}")
        photo(robot, "06_seen")
        snap_map(robot, "cube seen", "seen")

        log("pickup_object...")
        r = robot.behavior.pickup_object(cube, num_retries=2)
        log(f"  pickup: {getattr(r,'result',r)}")
        photo(robot, "10_picked")

        log("carrying it forward 180mm...")
        va.drive_straight(robot, 180, 80)
        photo(robot, "11_moved")

        log("place_object_on_ground_here...")
        r = robot.behavior.place_object_on_ground_here(num_retries=1)
        log(f"  place: {getattr(r,'result',r)}")
        photo(robot, "12_placed")
        snap_map(robot, "cube moved", "moved")

        log("backing off to re-see the cube...")
        va.drive_straight(robot, -120, 80)
        cube = observe_cube(robot, secs=8.0)
        if not (cube and cube.pose and cube.pose.is_valid):
            log("lost the cube after placing — leaving it here. Zeke can reset.")
            return

        log("pickup again to RETURN it home...")
        r = robot.behavior.pickup_object(cube, num_retries=2)
        log(f"  pickup2: {getattr(r,'result',r)}")

        from anki_vector.util import Pose, degrees
        log(f"driving cube back to origin ({origin[0]:.0f},{origin[1]:.0f})...")
        # approach the origin (offset back a bit so the cube lands on the spot)
        r = robot.behavior.go_to_pose(
            Pose(x=origin[0]-60, y=origin[1], z=0.0, angle_z=degrees(origin[2])),
            relative_to_robot=False, num_retries=2)
        log(f"  go_to_origin: {getattr(r,'result',r)}")
        log("placing back...")
        r = robot.behavior.place_object_on_ground_here(num_retries=1)
        log(f"  place_back: {getattr(r,'result',r)}")
        photo(robot, "20_replaced")
        snap_map(robot, "cube replaced", "replaced")
        va.drive_straight(robot, -100, 80)  # back off from the cube
        photo(robot, "21_done")
        log("cube move-and-replace complete.")
    log("session closed.")


if __name__ == "__main__":
    main()
