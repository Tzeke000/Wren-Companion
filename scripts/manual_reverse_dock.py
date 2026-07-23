"""Manual reverse dock via wire-pod REST + her own nervous-system feed.
No SDK session needed (2026-07-23: ListAnimations dead post-reboot, body_park
wedges). Closed loop: turn 180 by feed heading, reverse onto contacts, stop
the instant on_charger flips. Prints one line per phase."""
import json
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
SENSES = REPO / "state" / "vector" / "senses_live.json"
WP = "http://127.0.0.1:8080/api-sdk"
SERIAL = "0dd1cdaf"


def sense():
    try:
        d = json.loads(SENSES.read_text(encoding="utf-8"))
        if time.time() - float(d.get("ts") or 0) > 3.0:
            return {}
        return d.get("latest") or {}
    except Exception:
        return {}


def wheels(lw, rw):
    try:
        requests.post(f"{WP}/move_wheels",
                      params={"serial": SERIAL, "lw": lw, "rw": rw}, timeout=4)
    except Exception:
        pass


def stop():
    wheels(0, 0)


def norm(a):
    return (a + 180.0) % 360.0 - 180.0


def main():
    s = sense()
    if not s or "heading" not in s:
        print("NO-FEED abort")
        return 2
    h0 = s["heading"]
    target = norm(h0 + 180.0)
    print(f"phase1 TURN: h0={h0:.0f} target={target:.0f}")
    for i in range(60):
        s = sense()
        if not s:
            stop(); print("feed lost mid-turn"); return 2
        if s.get("picked_up") or s.get("falling"):
            stop(); print("SAFETY stop (pickup/fall)"); return 2
        err = norm(target - s.get("heading", h0))
        if abs(err) < 6.0:
            stop(); print(f"turned: heading={s.get('heading'):.0f} err={err:.0f}")
            break
        spd = 80 if abs(err) > 30 else 45
        # err>0 = need LEFT turn (heading increases): left turn = lw neg, rw pos
        if err > 0:
            wheels(-spd, spd)
        else:
            wheels(spd, -spd)
        time.sleep(0.25)
        stop()
        time.sleep(0.35)   # let heading settle in feed
    else:
        stop(); print("turn never converged"); return 2
    time.sleep(0.6)
    print("phase2 REVERSE onto dock")
    for i in range(45):
        s = sense()
        if not s:
            stop(); print("feed lost mid-reverse"); return 2
        if s.get("on_charger"):
            stop(); print(f"ON CHARGER after {i} bursts"); break
        if s.get("picked_up") or s.get("falling"):
            stop(); print("SAFETY stop (pickup/fall)"); return 2
        wheels(-42, -42)
        time.sleep(0.35)
        stop()
        time.sleep(0.25)
    else:
        stop(); print("reverse exhausted, NOT on charger"); return 1
    stop()
    time.sleep(2.0)
    s = sense()
    print(f"FINAL: on_charger={s.get('on_charger')} charging={s.get('charging')}")
    return 0 if s.get("on_charger") else 1


if __name__ == "__main__":
    sys.exit(main())
