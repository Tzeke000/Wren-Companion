"""test_unified_body.py — can ONE SDK control session hold the LIVE camera feed
AND control motion, without jumping out or hanging? (2026-07-14, Zeke's directive:
"you need your body's live feed and you shouldn't have to jump out of your body
to control the body.")

Earlier a run hung, but it loaded camera feed + NAV-MAP feed + drive_off_charger
together — this isolates the real question: camera feed + set_head_angle
(behavior) + direct wheel motors, all in one held connection. Each step is
wrapped in a thread timeout so a hang is DETECTED, not fatal. Minimal movement
(stays ~docked)."""
from __future__ import annotations
import sys, threading, time
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FRAME = REPO / "state" / "vector"
SERIAL = "0dd1cdaf"


def log(m): print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def bounded(fn, timeout, label):
    box = {}
    def w():
        try: box["r"] = fn()
        except Exception as e: box["e"] = e
    t = threading.Thread(target=w, daemon=True); t.start(); t.join(timeout)
    if t.is_alive(): log(f"  {label}: *** HANG (>{timeout}s) ***"); return "HANG"
    if "e" in box: log(f"  {label}: EXC {box['e']!r}"); return "ERR"
    log(f"  {label}: OK -> {box.get('r')}"); return box.get("r")


def main():
    import anki_vector
    from anki_vector.util import degrees
    log("opening ONE control session with live camera feed...")
    with anki_vector.Robot(SERIAL, cache_animation_lists=False,
                           default_logging=False) as robot:
        log("connected (control). enabling camera feed...")
        bounded(lambda: robot.camera.init_camera_feed(), 10, "init_camera_feed")
        time.sleep(1.5)
        # 1) sample live frames rapidly (is the feed actually flowing?)
        for i in range(3):
            def grab():
                img = robot.camera.latest_image
                if img and img.raw_image:
                    p = FRAME / f"unified_{i}.jpg"; img.raw_image.save(str(p))
                    import numpy as np
                    return f"frame {i} mean {np.asarray(img.raw_image.convert('L')).mean():.0f}"
                return f"frame {i} none"
            bounded(grab, 8, f"latest_image[{i}]")
            time.sleep(0.4)
        # 2) THE CRUX: set_head_angle (a behavior) WITH the feed live — does it hang?
        bounded(lambda: str(robot.behavior.set_head_angle(degrees(-18)).result),
                12, "set_head_angle(-18) WITH feed")
        # 3) grab a frame AFTER the head move (feed still alive?)
        def grab2():
            img = robot.camera.latest_image
            if img and img.raw_image:
                img.raw_image.save(str(FRAME / "unified_afterhead.jpg")); return "saved"
            return "none"
        bounded(grab2, 8, "latest_image after head move")
        # 4) direct wheel motors (NOT a behavior) — brief spin, no head reset expected
        bounded(lambda: robot.motors.set_wheel_motors(35, -35), 6, "set_wheel_motors spin")
        time.sleep(0.5)
        bounded(lambda: robot.motors.set_wheel_motors(0, 0), 6, "set_wheel_motors stop")
        # 5) does the head stay put after motor use? (grab + report)
        bounded(grab2, 8, "latest_image after motors")
        # 6) precise behavior after motors — turn_in_place — hang?
        bounded(lambda: str(robot.behavior.turn_in_place(degrees(10)).result),
                12, "turn_in_place(10) after feed+motors")
        log("all steps attempted.")
    log("session closed cleanly.")


if __name__ == "__main__":
    main()
