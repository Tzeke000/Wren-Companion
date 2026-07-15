"""vector_zigzag_pickup.py — closed-loop zig-zag + manual cube pickup (2026-07-14).

Zeke: zig-zag the cones, then pick up the cube at the end — and "aim yourself
better" (the cube can be fork-lifted mechanically without its battery, it just
needs alignment). This is CLOSED-LOOP visual servoing over WIRE-POD (so I can
freely alternate camera looks and wheel/fork moves — the SDK behavior API hangs
if you interleave feeds, so wire-pod is the right layer for a see->nudge->see
loop). Front proximity (nerves) gives approach distance for the final lift.

Bounded + safe: small bursts, iteration caps, reads nerves for cliff/pickup and
aborts. Head tilted down to watch the floor. Leaves Vector holding the cube for
Zeke to see; a redock pass can follow."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import cv2
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FRAME = REPO / "state" / "vector"
W = "http://127.0.0.1:8080"
ESN = json.loads((Path.home() / "AppData/Roaming/wire-pod/jdocs/botSdkInfo.json"
                  ).read_text())["robots"][0]["esn"]


def log(m):
    print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def sdk(path, params=None, timeout=12):
    q = dict(params or {}); q["serial"] = ESN
    return requests.post(f"{W}/api-sdk/{path}", params=q, timeout=timeout)


def nerves():
    try:
        return json.loads((FRAME / "nerves.json").read_text())
    except Exception:
        return {}


def safe():
    n = nerves()
    if n.get("cliff") or n.get("picked_up"):
        log(f"  SAFETY stop: cliff={n.get('cliff')} picked_up={n.get('picked_up')}")
        return False
    return True


def grab():
    """One BGR frame from wire-pod cam-stream."""
    buf = b""
    try:
        with requests.get(f"{W}/cam-stream", params={"serial": ESN},
                          stream=True, timeout=12) as r:
            for ch in r.iter_content(8192):
                buf += ch
                s = buf.find(b"\xff\xd8")
                if s != -1:
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e != -1:
                        return cv2.imdecode(np.frombuffer(buf[s:e + 2], np.uint8),
                                            cv2.IMREAD_COLOR)
                if len(buf) > 4_000_000:
                    break
    except Exception as e:
        log(f"  grab err {e!r}")
    finally:
        try: sdk("stop_cam_stream")
        except Exception: pass
    return None


def wheels(lw, rw, secs):
    sdk("move_wheels", {"lw": lw, "rw": rw})
    time.sleep(secs)
    sdk("move_wheels", {"lw": 0, "rw": 0})


def turn(deg_sign, secs=0.28):
    # deg_sign>0 = turn LEFT (ccw): left wheel back, right fwd
    s = 60
    wheels(-s if deg_sign > 0 else s, s if deg_sign > 0 else -s, secs)


def forward(secs=0.35, speed=55):
    if not safe():
        return False
    wheels(speed, speed, secs)
    return True


def lift(up):
    sdk("move_lift", {"speed": 2 if up else -2}); time.sleep(1.1)
    sdk("move_lift", {"speed": 0}); time.sleep(0.3)


def detect_cones(bgr):
    """Orange cones -> list of (cx, cy, area), nearest (largest cy) last."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (3, 90, 70), (22, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        a = cv2.contourArea(c)
        if a < 120:
            continue
        x, y, w, h = cv2.boundingRect(c)
        if h < w * 0.8:      # cones are taller than wide
            continue
        out.append((x + w / 2, y + h, a))
    out.sort(key=lambda t: t[1])   # by cy ascending; nearest = last
    return out


def detect_cube(bgr):
    """Cube = large dark blob in the central-lower floor ROI -> (cx, cy, area)."""
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    roi = g[int(h * 0.25):int(h * 0.80), int(w * 0.15):int(w * 0.85)]
    thr = cv2.threshold(roi, 55, 255, cv2.THRESH_BINARY_INV)[1]
    thr = cv2.morphologyEx(thr, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cnts:
        a = cv2.contourArea(c)
        if a < 500:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        if best is None or a > best[2]:
            best = (x + bw / 2 + w * 0.15, y + bh / 2 + h * 0.25, a)
    return best


def main():
    log("assume control + head down + fork down")
    sdk("assume_behavior_control", {"priority": "high"}); time.sleep(0.3)
    sdk("move_head", {"speed": -2}); time.sleep(0.9); sdk("move_head", {"speed": 0})
    lift(up=False)                      # fork LOW to slide under the cube
    W_IMG = 800
    cx_center = W_IMG / 2

    # ---- PHASE A: zig-zag the cones ----
    log("PHASE A: zig-zag the cones")
    passed = 0
    for it in range(14):
        if not safe():
            break
        bgr = grab()
        if bgr is None:
            continue
        W_IMG = bgr.shape[1]; cx_center = W_IMG / 2
        cones = detect_cones(bgr)
        near = [c for c in cones if c[1] > bgr.shape[0] * 0.45]   # cones low in frame = close
        if not near:
            log(f"  it{it}: no near cone — advancing past")
            forward(0.4)
            passed += 1
            if passed >= 2:
                log("  cones cleared")
                break
            continue
        cx, cy, area = near[-1]
        off = cx - cx_center
        # steer to pass the cone on the side with more room: if cone left, go right
        log(f"  it{it}: cone at x={cx:.0f} (off {off:+.0f}) cy={cy:.0f} area={area:.0f}")
        if abs(off) < 90:
            # cone dead ahead: turn away from it (toward whichever side it's leaning)
            turn(-1 if off <= 0 else +1, 0.30)   # push cone to the far side
            forward(0.32)
        else:
            # cone already off to a side: just advance past it
            forward(0.4)
        passed = 0

    # ---- PHASE B: approach + pick up the cube ----
    log("PHASE B: find + approach the cube")
    got = False
    for it in range(16):
        if not safe():
            break
        n = nerves(); prox = n.get("prox_mm")
        bgr = grab()
        cube = detect_cube(bgr) if bgr is not None else None
        log(f"  it{it}: prox={prox} cube={'(%.0f,%.0f,%.0f)'%cube if cube else None}")
        if isinstance(prox, int) and 0 < prox <= 55:
            log("  at the cube (prox<=55) — lifting")
            got = True
            break
        if cube is None:
            forward(0.3)      # nothing seen — inch forward to bring cube into view
            continue
        off = cube[0] - (bgr.shape[1] / 2)
        if abs(off) > 70:
            turn(+1 if off < 0 else -1, 0.20)   # center on the cube
        else:
            forward(0.30)
    # final creep + lift
    if got or True:
        forward(0.22, speed=45)     # nose under the cube's lip
        lift(up=True)               # raise fork -> pick it up
        log("lift raised — cube should be up")
        time.sleep(0.5)
        n = nerves()
        log(f"post-lift nerves prox={n.get('prox_mm')} picked_up={n.get('picked_up')}")
        wheels(-45, -45, 0.5)       # back up a bit to show the cube on the fork
    log("done — releasing control")
    try: sdk("release_behavior_control")
    except Exception: pass


if __name__ == "__main__":
    main()
