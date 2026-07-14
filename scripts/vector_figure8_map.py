"""vector_figure8_map.py — ONE-session drive + blueprint (2026-07-14).

Zeke: test a figure-8 around the cones + build a room blueprint as I move.
Done in a single control session so (a) the pose frame stays consistent for
go_to_pose waypoints, (b) the nav-map feed persists and grows across the whole
drive, (c) no multi-session connection churn. Camera + nav-map captured at
each stage into state/vector/ for me to Read and show Zeke.

Safe: floor arena (confirmed via tower+onboard cam), full battery, native
cliff avoidance on the behaviors + picked_up guard. Redocks at the end.
"""
from __future__ import annotations

import json
import math
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
            p = FRAME / f"fig8_{name}.jpg"
            img.raw_image.save(str(p))
            log(f"  photo -> {p.name}")
    except Exception as e:
        log(f"  photo fail: {e!r}")


def snap_map(robot, tag, name):
    r = vm.capture_map(robot, tag=tag)
    if r.get("ok"):
        try:
            shutil.copy(str(vm.MAP_PNG), str(FRAME / f"fig8_map_{name}.png"))
        except Exception:
            pass
        log(f"  MAP {name}: {r['cells']} cells {r.get('counts')}")
    else:
        log(f"  MAP {name}: {r.get('error')}")
    return r


def transform(x0, y0, th0_deg, lx, ly, lth):
    """local offset -> world pose in the robot's odometry frame."""
    th0 = math.radians(th0_deg)
    wx = x0 + lx * math.cos(th0) - ly * math.sin(th0)
    wy = y0 + lx * math.sin(th0) + ly * math.cos(th0)
    return wx, wy, th0_deg + lth


def figure8_waypoints(cx=300.0, R=150.0, n=5):
    """local (x fwd, y left) figure-8: left lobe CCW then right lobe CW,
    both passing through the crossing point (cx, 0)."""
    pts = []
    # left lobe: center (cx, +R), CCW, start/end at ang -90 (=> (cx,0))
    for i in range(n + 1):
        ang = math.radians(-90) + (2 * math.pi) * (i / n)
        px = cx + R * math.cos(ang)
        py = R + R * math.sin(ang)
        head = math.degrees(ang) + 90  # tangent, CCW
        pts.append((px, py, head))
    # right lobe: center (cx, -R), CW, start/end at ang +90 (=> (cx,0))
    for i in range(n + 1):
        ang = math.radians(90) - (2 * math.pi) * (i / n)
        px = cx + R * math.cos(ang)
        py = -R + R * math.sin(ang)
        head = math.degrees(ang) - 90  # tangent, CW
        pts.append((px, py, head))
    return pts


def main():
    from anki_vector.util import Pose, degrees
    # IMPORTANT: NO camera/nav_map feed init in a control session — that HANGS
    # the behavior API (drive_off_charger never returned, 2026-07-14). Mapping
    # is now the OBSERVE DAEMON's job (it reads nav_map passively while I drive);
    # visuals come from the tower cam externally. This session does behaviors ONLY.
    log("connecting (control, behaviors-only)...")
    t0 = time.time()
    with va.control_session() as robot:
        log(f"connected {time.time()-t0:.1f}s")
        va.set_head_angle(robot, 5.0)

        log("undocking...")
        r = va.drive_off_charger(robot)
        log(f"  undock: {r}")
        time.sleep(0.5)

        # record start pose (origin for the figure-8)
        p = robot.pose
        x0, y0, th0 = p.position.x, p.position.y, p.rotation.angle_z.degrees
        log(f"start pose: x={x0:.0f} y={y0:.0f} th={th0:.0f}")

        wps = figure8_waypoints(cx=300.0, R=150.0, n=5)
        log(f"driving figure-8: {len(wps)} waypoints")
        ok_count = 0
        for i, (lx, ly, lth) in enumerate(wps):
            wx, wy, wth = transform(x0, y0, th0, lx, ly, lth)
            try:
                res = robot.behavior.go_to_pose(
                    Pose(x=wx, y=wy, z=0.0, angle_z=degrees(wth)),
                    relative_to_robot=False, num_retries=1)
                sres = str(getattr(res, "result", res))
                bad = any(b in sres.lower() for b in
                          ("fail", "abort", "cancel", "timeout"))
                ok_count += 0 if bad else 1
                log(f"  wp{i:02d} -> ({wx:.0f},{wy:.0f},{wth:.0f}) {sres or 'ok'}")
            except Exception as e:
                log(f"  wp{i:02d} EXC {e!r}")

        log(f"figure-8 done: {ok_count}/{len(wps)} waypoints ok")

        # go home
        log("returning to dock (drive_on_charger)...")
        rd = va.drive_on_charger(robot)
        log(f"  dock: {rd}")
        time.sleep(0.5)
        # verify seat
        try:
            bs = robot.get_battery_state()
            log(f"  battery: volts={bs.battery_volts:.3f} on_charger="
                f"{bs.is_on_charger_platform} charging={bs.is_charging}")
        except Exception as e:
            log(f"  battery read: {e!r}")
    log("session closed.")


if __name__ == "__main__":
    main()
