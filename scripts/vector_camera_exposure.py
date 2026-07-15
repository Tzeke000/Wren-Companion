"""vector_camera_exposure.py — diagnose/brighten Vector's dark camera (2026-07-14).

Zeke: the room is bright to him (3 lights) but Vector's cam looks really dim.
That points at EXPOSURE/GAIN settings, not real darkness. Read current camera
config, grab a baseline frame, then try (a) auto-exposure and (b) max manual
exposure+gain, saving a frame for each so I can see which fixes it. Camera-only,
NO behaviors (behaviors+feeds hang — but camera alone is fine)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FRAME = REPO / "state" / "vector"
from brain import vector_action as va


def log(m):
    print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def grab(robot, name):
    try:
        img = robot.camera.latest_image
        if img and img.raw_image:
            p = FRAME / f"expo_{name}.jpg"
            img.raw_image.save(str(p))
            # brightness stat
            import numpy as np
            arr = np.asarray(img.raw_image.convert("L"))
            log(f"  {name}: saved (mean brightness {arr.mean():.0f}/255)")
            return
    except Exception as e:
        log(f"  {name}: grab fail {e!r}")


def main():
    log("connecting (camera-only)...")
    with va.control_session() as robot:
        cam = robot.camera
        cam.init_camera_feed()
        time.sleep(1.5)
        # current settings
        try:
            log(f"auto_exposure_enabled={getattr(cam,'auto_exposure_enabled','?')} "
                f"exposure_ms={getattr(cam,'exposure_ms','?')} "
                f"gain={getattr(cam,'gain','?')}")
        except Exception as e:
            log(f"read settings: {e!r}")
        cfg = getattr(cam, "config", None)
        if cfg:
            log(f"config: exp_ms {getattr(cfg,'min_camera_exposure_time_ms','?')}"
                f"..{getattr(cfg,'max_camera_exposure_time_ms','?')} "
                f"gain {getattr(cfg,'min_gain','?')}..{getattr(cfg,'max_gain','?')}")
        grab(robot, "00_baseline")

        # (a) auto-exposure ON
        try:
            cam.enable_auto_exposure(enable_auto_exposure=True)
            log("enabled auto-exposure")
        except TypeError:
            try:
                cam.enable_auto_exposure(True)
                log("enabled auto-exposure (positional)")
            except Exception as e:
                log(f"auto-exposure failed: {e!r}")
        except Exception as e:
            log(f"auto-exposure failed: {e!r}")
        time.sleep(2.0)
        grab(robot, "01_auto")

        # (b) max manual exposure + high gain
        if cfg:
            try:
                max_exp = int(getattr(cfg, "max_camera_exposure_time_ms", 66))
                max_gain = float(getattr(cfg, "max_gain", 6.0))
                cam.set_manual_exposure(max_exp, max_gain)
                log(f"set manual exposure {max_exp}ms gain {max_gain}")
                time.sleep(2.0)
                grab(robot, "02_maxmanual")
                # and a mid setting
                cam.set_manual_exposure(max_exp, max(1.0, max_gain * 0.5))
                log(f"set manual exposure {max_exp}ms gain {max_gain*0.5:.1f}")
                time.sleep(2.0)
                grab(robot, "03_midmanual")
            except Exception as e:
                log(f"manual exposure failed: {e!r}")
    log("done.")


if __name__ == "__main__":
    main()
