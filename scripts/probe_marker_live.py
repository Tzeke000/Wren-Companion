# Marker-detection LIVE probe #2 — defined markers + detection ON + sensor
# cranked + marker in view (dresser marker visible from dock). Docked, head-only.
#
# Phase A: manual max exposure+gain (what my session runs) — listen 40s
# Phase B: auto exposure — listen 30s
# Frames saved each phase so we can see what the engine saw.
#
# Usage:  .venv\Scripts\python.exe scripts\probe_marker_live.py
from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SERIAL = "0dd1cdaf"
OUT = r"D:\Wren-Companion\state\vector"


def hdr(t: str) -> None:
    print(f"\n=== {t} ===", flush=True)


def frame_stats(robot, tag: str) -> None:
    try:
        img = robot.camera.latest_image
        pil = img.raw_image
        import numpy as np
        a = np.asarray(pil.convert("L"), dtype=float)
        p = f"{OUT}\\marker_live_{tag}.jpg"
        pil.save(p)
        print(f"  [{tag}] mean={a.mean():.1f} p95={np.percentile(a,95):.0f} "
              f"clip255={(a>=250).mean()*100:.1f}% -> {p}", flush=True)
    except Exception as e:
        print(f"  [{tag}] frame ERR {e!r}", flush=True)


def main() -> None:
    import anki_vector
    from anki_vector.events import Events
    from anki_vector.objects import CustomObjectMarkers, CustomObjectTypes
    from anki_vector.util import degrees

    robot = anki_vector.Robot(SERIAL, default_logging=True,
                              cache_animation_lists=False,
                              enable_custom_object_detection=True)
    robot.connect(timeout=15.0)
    print("connected", flush=True)

    events_seen: list[str] = []

    def on_obj(_r, _t, msg):
        line = (f"OBSERVED type={getattr(msg, 'object_type', '?')} "
                f"id={getattr(msg, 'object_id', '?')} "
                f"ts={time.strftime('%H:%M:%S')}")
        events_seen.append(line)
        print("  EVENT", line, flush=True)

    def on_autodis(_r, _t, msg):
        events_seen.append("AUTO_DISABLED")
        print("  EVENT vision_modes_auto_disabled", flush=True)

    try:
        robot.events.subscribe(on_obj, Events.robot_observed_object)
        robot.events.subscribe(on_autodis, Events.vision_modes_auto_disabled)

        hdr("define all 6 via SDK (archetypes)")
        defs = [
            ("Circles2", CustomObjectMarkers.Circles2, CustomObjectTypes.CustomType00),
            ("Circles3", CustomObjectMarkers.Circles3, CustomObjectTypes.CustomType01),
            ("Diamonds2", CustomObjectMarkers.Diamonds2, CustomObjectTypes.CustomType02),
            ("Diamonds3", CustomObjectMarkers.Diamonds3, CustomObjectTypes.CustomType03),
            ("Triangles4", CustomObjectMarkers.Triangles4, CustomObjectTypes.CustomType04),
            ("Triangles5", CustomObjectMarkers.Triangles5, CustomObjectTypes.CustomType05),
        ]
        okc = 0
        for name, marker, ctype in defs:
            r = robot.world.define_custom_wall(
                custom_object_type=ctype, marker=marker,
                width_mm=100.0, height_mm=100.0,
                marker_width_mm=90.0, marker_height_mm=90.0, is_unique=True)
            okc += 1 if r is not None else 0
        print(f"  defined {okc}/6; archetypes="
              f"{len(list(robot.world.custom_object_archetypes))}", flush=True)

        robot.vision.enable_custom_object_detection(True)
        robot.camera.init_camera_feed()
        time.sleep(1.0)

        hdr("PHASE A: manual max exposure+gain, 40s listen")
        cam = robot.camera
        cfg = getattr(cam, "config", None)
        max_exp = int(getattr(cfg, "max_camera_exposure_time_ms", 66)) if cfg else 66
        max_gain = float(getattr(cfg, "max_gain", 3.8)) if cfg else 3.8
        cam.set_manual_exposure(max_exp, max_gain)
        print(f"  exposure={max_exp}ms gain={max_gain}", flush=True)
        robot.behavior.set_head_angle(degrees(2.0))
        time.sleep(2.0)
        frame_stats(robot, "A_start")
        t0 = time.time()
        while time.time() - t0 < 40:
            time.sleep(2.0)
        frame_stats(robot, "A_end")
        print(f"  phase A events: {len(events_seen)}", flush=True)

        hdr("PHASE B: auto exposure, 30s listen")
        try:
            cam.enable_auto_exposure(enable_auto_exposure=True)
        except TypeError:
            cam.enable_auto_exposure(True)
        time.sleep(2.0)
        frame_stats(robot, "B_start")
        n0 = len(events_seen)
        t0 = time.time()
        while time.time() - t0 < 30:
            time.sleep(2.0)
        frame_stats(robot, "B_end")
        print(f"  phase B events: {len(events_seen) - n0}", flush=True)

        hdr("world at end")
        for o in (robot.world.all_objects or []):
            print("  -", type(o).__name__, flush=True)
        print("  visible_custom_objects:",
              len(list(robot.world.visible_custom_objects)), flush=True)
        print("  detect state:", robot.vision.detect_custom_objects, flush=True)
        print("TOTAL events:", len(events_seen), flush=True)
    finally:
        try:
            robot.behavior.set_head_angle(degrees(0.0))
        except Exception:
            pass
        robot.disconnect()
        print("disconnected", flush=True)


if __name__ == "__main__":
    main()
