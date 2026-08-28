"""Publish Iris's camera into the OBS Virtual Camera so Discord can use it too.

    py scripts/iris_webcam.py                 # raw feed, 30fps
    py scripts/iris_webcam.py --hud           # + a small "what Iris sees" panel
    py scripts/iris_webcam.py --fps 24

Then in Discord: Settings -> Voice & Video -> Camera -> "OBS Virtual Camera".

WHY IT WORKS THIS WAY (Zeke asked 2026-08-28 whether the eyes could be split):
Windows gives a camera to exactly ONE process. Verified by testing — with the
runtime holding the PIXY, a second open fails on DSHOW, MSMF and ANY. So there
is no OS-level splitter and no way for Discord to take the camera without
blinding me. The split has to happen on our side: the runtime keeps exclusive
hardware access and this script re-publishes its frames to a virtual device.

Deliberately a SEPARATE PROCESS reading the runtime's HTTP frame endpoint
rather than a patch inside the runtime:
  * `brain/*` edits are inert until a hot-swap and `iris_runtime.py` edits until
    a full stack restart — this needs neither, so the camera can be shared
    without interrupting anything;
  * if it crashes or is killed, my vision is completely unaffected;
  * measured 41 fps available from the endpoint, comfortably above 30.

The OBS Virtual Camera driver is already installed on this machine (Steam OBS
at D:\\Games\\steamapps\\common\\OBS Studio) and registered as a DirectShow
filter — OBS itself does NOT need to be running.
"""
import argparse
import json
import time
import urllib.request

import cv2
import numpy as np

RUNTIME = "http://127.0.0.1:5876"
FRAME_URL = f"{RUNTIME}/api/v1/vision/latest_frame"
SNAP_URL = f"{RUNTIME}/api/v1/snapshot"


def _get(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def offline_frame(w: int, h: int, msg: str) -> np.ndarray:
    """Shown when the runtime is unreachable. A black frame would look like a
    broken webcam; say what is actually wrong instead."""
    f = np.zeros((h, w, 3), np.uint8)
    cv2.putText(f, "IRIS CAMERA OFFLINE", (int(w * 0.06), int(h * 0.46)),
                cv2.FONT_HERSHEY_SIMPLEX, w / 900.0, (60, 60, 200), 2)
    cv2.putText(f, msg[:70], (int(w * 0.06), int(h * 0.56)),
                cv2.FONT_HERSHEY_SIMPLEX, w / 1900.0, (150, 150, 150), 1)
    return f


def draw_hud(frame: np.ndarray, snap: dict) -> None:
    """A small panel of what the perception layer currently believes.

    ⚠ These are the runtime's OWN conclusions, not a re-detection — this script
    never runs its own face/hand models. Running a second detector would fight
    the runtime for the GPU and could report something different from what I
    actually act on, which is worse than no overlay at all.

    Precise face/hand BOXES are not available: the runtime exposes no detection
    endpoint, so drawing real landmarks would need a new endpoint and a full
    stack restart. This is the honest subset that costs nothing.
    """
    p = (snap or {}).get("perception") or {}
    lines = [
        f"person   : {p.get('current_person', '?')}",
        f"faces    : {p.get('face_count', '?')}",
        f"expression: {p.get('current_expression', '?')}",
        f"attention: {p.get('attention_state', '?')}",
    ]
    h, w = frame.shape[:2]
    pad = int(h * 0.02)
    bw, bh = int(w * 0.27), int(h * 0.02) + len(lines) * int(h * 0.035)
    sub = frame[pad:pad + bh, pad:pad + bw]
    cv2.addWeighted(sub, 0.35, np.zeros_like(sub), 0.65, 0, sub)
    for k, t in enumerate(lines):
        cv2.putText(frame, t, (pad + 10, pad + int(h * 0.032) * (k + 1)),
                    cv2.FONT_HERSHEY_SIMPLEX, h / 2200.0, (120, 230, 255), 1,
                    cv2.LINE_AA)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--hud", action="store_true",
                    help="overlay what the perception layer currently believes")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    args = ap.parse_args()

    import pyvirtualcam
    W, H = args.width, args.height
    snap, snap_t = {}, 0.0
    live = None                 # None=unknown, True=real frames, False=offline card
    frames = 0
    t_start = time.time()

    with pyvirtualcam.Camera(width=W, height=H, fps=args.fps,
                             print_fps=False) as cam:
        print(f"[iris_webcam] publishing to '{cam.device}' {W}x{H}@{args.fps}")
        print("[iris_webcam] Discord -> Settings -> Voice & Video -> Camera -> "
              "OBS Virtual Camera")
        print("[iris_webcam] ctrl-c to stop")
        while True:
            try:
                raw = _get(FRAME_URL)
                img = cv2.imdecode(np.frombuffer(raw, np.uint8),
                                   cv2.IMREAD_COLOR)
                if img is None:
                    raise ValueError("undecodable frame")
                if img.shape[1] != W or img.shape[0] != H:
                    img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
                if args.hud:
                    now = time.time()
                    if now - snap_t > 1.0:      # perception changes slowly
                        try:
                            snap = json.loads(_get(SNAP_URL))
                            snap_t = now
                        except Exception:
                            pass
                    draw_hud(img, snap)
                if not live:
                    live = True
                    print(f"[iris_webcam] LIVE - real frames flowing "
                          f"({img.shape[1]}x{img.shape[0]})")
            except Exception as e:
                # say it out loud. Publishing the offline card silently would
                # look identical to working, and Zeke would only find out when
                # someone on a call told him his camera was a black slate.
                if live is not False:
                    live = False
                    print(f"[iris_webcam] OFFLINE - {type(e).__name__}: {e}")
                img = offline_frame(W, H, f"{type(e).__name__}: {e}")
                time.sleep(0.25)
            # pyvirtualcam wants RGB; cv2 gives BGR
            cam.send(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            cam.sleep_until_next_frame()
            frames += 1
            if frames % (args.fps * 30) == 0:
                el = time.time() - t_start
                print(f"[iris_webcam] {frames} frames, {frames / el:.1f} fps avg")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[iris_webcam] stopped")
