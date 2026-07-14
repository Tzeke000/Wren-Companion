"""test_vector_control_conn.py — STEP 1 connection-coexistence probe (2026-07-14).

The direct-SDK precise-motion action layer needs a CONTROL connection
(behavior_control_level=RESERVE_CONTROL, the default). The inhabit daemon
already holds an OBSERVE connection (level=None). Vector is documented to
allow ~1 SDK conn — so before building anything, prove empirically:

  1. Can a control-holding direct-SDK connection OPEN while the observe
     daemon runs? (connection succeeds at all)
  2. Does the observe daemon SURVIVE it? (nerves.json ts keeps advancing
     during and after my control session)
  3. Which precise behaviors actually EXIST in this anki_vector build?

ZERO MOTION. Reads pose, probes method availability, disconnects. Safe on
the dock. Run:
  D:\\Wren-Componanion\\.venv\\Scripts\\python.exe scripts\\test_vector_control_conn.py
"""
import json
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NERVES = REPO / "state" / "vector" / "nerves.json"
SERIAL = "0dd1cdaf"


def nerves_ts():
    try:
        return float(json.loads(NERVES.read_text(encoding="utf-8")).get("ts", 0))
    except Exception:
        return 0.0


def main():
    import anki_vector

    print(f"anki_vector version: {getattr(anki_vector, '__version__', '?')}")
    ts_before = nerves_ts()
    print(f"[observe daemon] nerves ts before: {ts_before:.2f} "
          f"(age {time.time()-ts_before:.1f}s)")

    t0 = time.time()
    try:
        with anki_vector.Robot(SERIAL, cache_animation_lists=False) as robot:
            dt = time.time() - t0
            print(f"*** CONTROL CONNECTION OPENED in {dt:.2f}s "
                  f"(alongside the observe daemon) ***")
            try:
                p = robot.pose
                print(f"pose: x={p.position.x:.0f} y={p.position.y:.0f} "
                      f"angle_z={p.rotation.angle_z.degrees:.1f}deg")
            except Exception as e:
                print(f"pose read failed: {e!r}")

            # give the observe daemon a few polls to prove it survived
            time.sleep(3.0)
            ts_mid = nerves_ts()
            print(f"[observe daemon] nerves ts during my control session: "
                  f"{ts_mid:.2f} (advanced {ts_mid-ts_before:.2f}s -> "
                  f"{'ALIVE' if ts_mid > ts_before else 'STALLED/KICKED'})")

            # which precise behaviors exist in this build
            print("--- behavior API availability ---")
            beh = robot.behavior
            for m in ("turn_in_place", "drive_straight", "go_to_pose",
                      "drive_on_charger", "drive_off_charger",
                      "set_head_angle", "set_lift_height", "set_eye_color",
                      "dock_with_cube", "pickup_object",
                      "place_object_on_ground_here", "roll_cube",
                      "go_to_object", "find_faces", "turn_towards_face",
                      "say_text"):
                print(f"  behavior.{m:32s} {hasattr(beh, m)}")
            print("--- world / cube ---")
            for attr in ("connect_cube", "world"):
                print(f"  robot.{attr:20s} {hasattr(robot, attr)}")
        print("*** disconnected cleanly (control released) ***")
    except Exception as e:
        print(f"!!! control connection FAILED: {e!r}")

    time.sleep(3.0)
    ts_after = nerves_ts()
    print(f"[observe daemon] nerves ts after disconnect: {ts_after:.2f} "
          f"(advanced {ts_after-ts_before:.2f}s total -> "
          f"{'SURVIVED' if ts_after > ts_before else 'DEAD'})")


if __name__ == "__main__":
    main()
