"""probe_vector_api.py — empirical ground-truth dump of the anki_vector SDK
surface I'm uncertain about (2026-07-15). Read-only where possible. Prints:
  * CameraImage fields + whether latest_image advances (feed live vs stale)
  * robot.status flags, robot.proximity, robot.pose, battery attrs
  * set_wheel_motors signature, ControlPriorityLevel enum
  * world/cube + camera marker-detection presence
Run: py -3.11 scripts/probe_vector_api.py
"""
from __future__ import annotations
import inspect, sys, time
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
SERIAL = "0dd1cdaf"


def hdr(t): print(f"\n=== {t} ===", flush=True)


def attrs(obj, names):
    for n in names:
        try:
            v = getattr(obj, n)
            print(f"  {n} = {v!r}"[:160], flush=True)
        except Exception as e:
            print(f"  {n} -> ERR {e!r}"[:160], flush=True)


def public(obj):
    return [n for n in dir(obj) if not n.startswith("_")]


def main():
    import anki_vector

    # --- no-connection introspection ---
    hdr("set_wheel_motors signature")
    try:
        from anki_vector.motors import MotorComponent
        print("  ", inspect.signature(MotorComponent.set_wheel_motors), flush=True)
    except Exception as e:
        print("  ERR", repr(e), flush=True)

    hdr("ControlPriorityLevel enum")
    try:
        from anki_vector.connection import ControlPriorityLevel as CPL
        for m in CPL:
            print(f"  {m.name} = {m.value}", flush=True)
    except Exception as e:
        print("  ERR", repr(e), flush=True)

    # --- connect (control so status/pose fully populate) ---
    hdr("connecting (control)")
    robot = anki_vector.Robot(SERIAL, cache_animation_lists=False,
                              default_logging=False)
    robot.connect(timeout=20)
    print("  connected", flush=True)

    try:
        hdr("camera feed + frame freshness")
        robot.camera.init_camera_feed()
        time.sleep(1.2)
        seen = []
        for i in range(4):
            img = robot.camera.latest_image
            if img is None:
                print(f"  [{i}] latest_image None", flush=True); time.sleep(0.4); continue
            if i == 0:
                print("  CameraImage public attrs:", public(img), flush=True)
            attrs(img, ["image_id", "image_recv_time", "image_resolution"])
            seen.append(getattr(img, "image_id", None))
            time.sleep(0.4)
        print("  image_id sequence:", seen,
              "-> feed LIVE" if len(set(x for x in seen if x is not None)) > 1
              else "-> STALE/repeat?", flush=True)

        hdr("robot.status public attrs")
        print(" ", public(robot.status), flush=True)
        attrs(robot.status, [
            "is_cliff_detected", "is_picked_up", "is_being_held",
            "are_wheels_moving", "is_on_charger", "is_charging",
            "is_button_pressed", "is_falling", "is_in_calm_power_mode"])

        hdr("robot.proximity.last_sensor_reading")
        try:
            pr = robot.proximity.last_sensor_reading
            print("  attrs:", public(pr), flush=True)
            attrs(pr, ["distance", "signal_quality", "is_valid",
                       "found_object", "is_lift_in_fov", "unobstructed"])
        except Exception as e:
            print("  ERR", repr(e), flush=True)

        hdr("robot.pose")
        try:
            p = robot.pose
            print("  attrs:", public(p), flush=True)
            print("  position:", p.position.x, p.position.y, p.position.z, flush=True)
            print("  angle_z deg:", p.rotation.angle_z.degrees, flush=True)
        except Exception as e:
            print("  ERR", repr(e), flush=True)

        hdr("battery state attrs")
        try:
            bs = robot.get_battery_state()
            print("  attrs:", public(bs), flush=True)
            attrs(bs, ["battery_volts", "battery_level", "is_charging",
                       "is_on_charger_platform", "suggested_charger_sec"])
        except Exception as e:
            print("  ERR", repr(e), flush=True)

        hdr("world / cube")
        try:
            print("  world public:", [n for n in public(robot.world)
                                      if "cube" in n.lower() or "light" in n.lower()
                                      or "connect" in n.lower()], flush=True)
            print("  connected_light_cube:",
                  getattr(robot.world, "connected_light_cube", "n/a"), flush=True)
        except Exception as e:
            print("  ERR", repr(e), flush=True)

        hdr("camera marker detection presence")
        for meth in ["enable_marker_detection", "enable_custom_object_detection"]:
            print(f"  camera.{meth}:", hasattr(robot.camera, meth), flush=True)
        print("  robot has 'vision':", hasattr(robot, "vision"), flush=True)

        hdr("behavior public methods (drive/dock/pickup)")
        print(" ", [n for n in public(robot.behavior)
                    if any(k in n.lower() for k in
                           ("drive", "dock", "pickup", "place", "pose",
                            "turn", "head", "lift", "go_to"))], flush=True)
    finally:
        robot.disconnect()
        print("\n  disconnected", flush=True)


if __name__ == "__main__":
    main()
