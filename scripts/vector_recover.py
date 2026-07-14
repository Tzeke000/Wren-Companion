"""vector_recover.py — get Vector safely back on the dock (2026-07-14).

The figure-8 script hung in drive_off_charger (likely because it init'd the
camera+nav_map feeds in the same session). This recovers him WITHOUT touching
those feeds: back off any obstacle, then drive_on_charger under a hard thread
timeout so a hang can't strand the recovery.
"""
from __future__ import annotations

import json
import sys
import threading
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


def run_bounded(fn, timeout, label):
    """Run a blocking behavior in a thread; return ('done'|'timeout', result)."""
    box = {}
    def work():
        try:
            box["r"] = fn()
        except Exception as e:
            box["e"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log(f"  {label}: TIMEOUT after {timeout}s (leaving thread; disconnect)")
        return "timeout", None
    if "e" in box:
        log(f"  {label}: EXC {box['e']!r}")
        return "error", None
    log(f"  {label}: {box.get('r')}")
    return "done", box.get("r")


def main():
    n0 = nerves()
    log(f"start nerves: on_charger={n0.get('on_charger')} prox_mm={n0.get('prox_mm')} "
        f"charger_dist={n0.get('charger_dist_mm')} bearing={n0.get('charger_bearing_deg')}")
    log("connecting (control, NO feeds)...")
    with va.control_session() as robot:
        p = robot.pose
        log(f"pose: x={p.position.x:.0f} y={p.position.y:.0f} th={p.rotation.angle_z.degrees:.0f}")
        # clear an obstacle right in front
        n = nerves()
        if isinstance(n.get("prox_mm"), int) and n["prox_mm"] < 90:
            log(f"obstacle {n['prox_mm']}mm ahead — backing off 100mm")
            run_bounded(lambda: va.drive_straight(robot, -100, 80), 15, "backoff")
        # go home under a hard timeout
        st, r = run_bounded(lambda: robot.behavior.drive_on_charger(), 55, "drive_on_charger")
        time.sleep(1.0)
        try:
            bs = robot.get_battery_state()
            log(f"after: volts={bs.battery_volts:.3f} on_charger={bs.is_on_charger_platform} "
                f"charging={bs.is_charging}")
        except Exception as e:
            log(f"battery read: {e!r}")
    n2 = nerves()
    log(f"end nerves: on_charger={n2.get('on_charger')} charger_dist={n2.get('charger_dist_mm')}")
    log("done.")


if __name__ == "__main__":
    main()
