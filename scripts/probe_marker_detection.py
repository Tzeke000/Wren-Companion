# Marker-detection deep probe — 2026-07-17 (the "why do custom markers never
# fire on WireOS" mystery). Runs DOCKED, no wheel motion (head only).
#
# Measures, with raw RPC responses (not SDK sugar):
#   1. EnableMarkerDetection response body
#   2. DefineCustomObject raw response (success + status) for all 6 markers
#   3. vision component state before/after
#   4. 25s event listen: robot_observed_object / vision_modes_auto_disabled
#   5. one raw camera frame stats (what the engine roughly sees)
#
# Usage:  .venv\Scripts\python.exe scripts\probe_marker_detection.py
from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SERIAL = "0dd1cdaf"


def hdr(t: str) -> None:
    print(f"\n=== {t} ===", flush=True)


def main() -> None:
    import anki_vector  # noqa
    from anki_vector.events import Events
    from anki_vector.messaging import protocol
    from anki_vector.objects import CustomObjectMarkers, CustomObjectTypes

    hdr("connect (behavior control, enable_custom_object_detection=True at ctor)")
    robot = anki_vector.Robot(
        SERIAL,
        default_logging=True,          # we WANT the SDK's own error logs
        cache_animation_lists=False,   # avoid sleepy ListAnimations timeout
        enable_custom_object_detection=True,  # ctor path this time
    )
    t0 = time.time()
    robot.connect(timeout=15.0)
    print(f"  connected in {time.time()-t0:.1f}s", flush=True)

    try:
        hdr("vision state after connect")
        print("  _detect_custom_objects:", robot.vision.detect_custom_objects, flush=True)

        hdr("explicit EnableMarkerDetection — raw response")
        resp = robot.vision.enable_custom_object_detection(True)
        print("  type:", type(resp).__name__, flush=True)
        print("  repr:", repr(resp)[:400], flush=True)
        st = getattr(resp, "status", None)
        print("  status:", repr(st)[:200], flush=True)

        hdr("DefineCustomObject — raw grpc responses (all 6)")
        defs = [
            ("Circles2", CustomObjectMarkers.Circles2, CustomObjectTypes.CustomType00),
            ("Circles3", CustomObjectMarkers.Circles3, CustomObjectTypes.CustomType01),
            ("Diamonds2", CustomObjectMarkers.Diamonds2, CustomObjectTypes.CustomType02),
            ("Diamonds3", CustomObjectMarkers.Diamonds3, CustomObjectTypes.CustomType03),
            ("Triangles4", CustomObjectMarkers.Triangles4, CustomObjectTypes.CustomType04),
            ("Triangles5", CustomObjectMarkers.Triangles5, CustomObjectTypes.CustomType05),
        ]
        for name, marker, ctype in defs:
            definition = protocol.CustomWallDefinition(
                marker=marker.id, width_mm=100.0, height_mm=100.0,
                marker_width_mm=90.0, marker_height_mm=90.0)
            req = protocol.DefineCustomObjectRequest(
                custom_type=ctype.id, is_unique=True, custom_wall=definition)
            try:
                fut = robot.conn.run_coroutine(
                    robot.conn.grpc_interface.DefineCustomObject(req))
                raw = fut.result(10.0) if hasattr(fut, "result") else fut
                print(f"  {name}: success={getattr(raw, 'success', '?')} "
                      f"status={repr(getattr(raw, 'status', None))[:120]} "
                      f"raw={repr(raw)[:160]}", flush=True)
            except Exception as e:
                print(f"  {name}: RPC ERR {e!r}", flush=True)

        hdr("SDK-level define (archetype bookkeeping) — one marker")
        r = robot.world.define_custom_wall(
            custom_object_type=CustomObjectTypes.CustomType06,
            marker=CustomObjectMarkers.Hexagons2,
            width_mm=100.0, height_mm=100.0,
            marker_width_mm=90.0, marker_height_mm=90.0, is_unique=True)
        print("  define_custom_wall(Hexagons2) ->", repr(r)[:200], flush=True)
        arch = list(robot.world.custom_object_archetypes)
        print(f"  archetypes now: {len(arch)}", flush=True)
        for a in arch:
            print("   -", repr(a)[:120], flush=True)

        hdr("event listen 25s (robot_observed_object / auto-disable) + head sweep")
        seen: list[str] = []

        def on_obj(_r, _t, msg):
            line = (f"OBSERVED type={getattr(msg, 'object_type', '?')} "
                    f"id={getattr(msg, 'object_id', '?')}")
            seen.append(line)
            print("  EVENT", line, flush=True)

        def on_autodis(_r, _t, msg):
            seen.append("VISION_MODES_AUTO_DISABLED")
            print("  EVENT vision_modes_auto_disabled:", repr(msg)[:150], flush=True)

        robot.events.subscribe(on_obj, Events.robot_observed_object)
        robot.events.subscribe(on_autodis, Events.vision_modes_auto_disabled)

        # head-only sweep (docked-safe): look level, then up a bit
        for ang in (0.0, 10.0, 25.0, 7.0):
            try:
                robot.behavior.set_head_angle(anki_vector.util.degrees(ang))
            except Exception as e:
                print(f"  head {ang}: {e!r}", flush=True)
            time.sleep(5.5)
        time.sleep(3.0)
        print(f"  events captured: {len(seen)}", flush=True)
        print("  _detect_custom_objects at end:", robot.vision.detect_custom_objects, flush=True)

        hdr("raw camera frame stats (roughly what the engine sees)")
        try:
            robot.camera.init_camera_feed()
            time.sleep(1.5)
            img = robot.camera.latest_image
            pil = img.raw_image
            import numpy as np
            a = np.asarray(pil.convert("L"), dtype=float)
            print(f"  size={pil.size} mean={a.mean():.1f} p5={np.percentile(a,5):.0f} "
                  f"p95={np.percentile(a,95):.0f} clipped255={(a>=250).mean()*100:.1f}%",
                  flush=True)
            out = r"D:\Wren-Companion\state\vector\marker_probe_frame.jpg"
            pil.save(out)
            print("  saved:", out, flush=True)
        except Exception as e:
            print("  frame ERR", repr(e), flush=True)

        hdr("world custom objects right now")
        objs = [o for o in (robot.world.all_objects or [])]
        for o in objs:
            print("  -", type(o).__name__, repr(getattr(o, 'pose', None))[:80], flush=True)
        print(f"  total={len(objs)}", flush=True)

    finally:
        try:
            robot.behavior.set_head_angle(anki_vector.util.degrees(0.0))
        except Exception:
            pass
        robot.disconnect()
        print("\ndisconnected", flush=True)


if __name__ == "__main__":
    main()
