"""vision_fps_probe — measure the TRUE capture rate + perception worker state.

Built 2026-08-27 for the all-30fps verification (handoff_2026-08-27_all30fps_restart).
The HTTP live_frame age-sawtooth measures through HTTP+JPEG-encode overhead;
this reads g["_raw_frame_slot"] timestamps directly in-process, which is the
ground truth for what the capture loop actually produces.

Returns: measured fps, inter-frame interval stats, per-worker last-pass ms,
and which iris-*-worker threads are alive.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from tools.tool_registry import register_tool


def _vision_fps_probe(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    duration = float(params.get("duration_s") or 4.0)
    duration = max(1.0, min(duration, 10.0))

    slot = g.get("_raw_frame_slot")
    if not slot:
        return {"ok": False, "error": "no _raw_frame_slot — capture loop not running?"}

    stamps: list[float] = []
    last = 0.0
    t0 = time.time()
    while time.time() - t0 < duration:
        s = g.get("_raw_frame_slot")
        if s and s[0] > last:
            last = s[0]
            stamps.append(last)
        time.sleep(0.002)  # 500Hz sample — never undersamples 30fps

    n = len(stamps)
    out: dict[str, Any] = {"ok": True, "frames": n, "window_s": round(duration, 2),
                           "fps": round(n / duration, 1)}
    if n >= 3:
        iv = sorted((b - a) * 1000.0 for a, b in zip(stamps, stamps[1:]))
        out["interframe_ms"] = {
            "median": round(iv[len(iv) // 2], 1),
            "p10": round(iv[int(len(iv) * 0.10)], 1),
            "p90": round(iv[int(len(iv) * 0.90)], 1),
            "min": round(iv[0], 1), "max": round(iv[-1], 1),
        }
    out["workers_alive"] = sorted(
        t.name for t in threading.enumerate() if t.name.startswith("iris-") and t.name.endswith("-worker"))
    out["perc_last_ms"] = {k: g.get(f"_perc_{k}_last_ms") for k in ("face", "hands", "expr", "attn")}
    out["frame_age_s"] = round(time.time() - last, 3) if last else None
    out["cam_props"] = g.get("_cam_props")  # what the device granted at open (None pre-restart-#3)
    ins = g.get("_insight_face")
    ez = g.get("_eye_tracker")
    out["engines"] = {
        "insight_face": {"present": ins is not None,
                         "available": bool(getattr(ins, "available", False))},
        "eye_tracker": {"present": ez is not None,
                        "available": bool(getattr(ez, "available", False))},
    }
    return out


register_tool(
    "vision_fps_probe",
    "Measure the TRUE camera capture fps in-process (samples g['_raw_frame_slot'] "
    "at 500Hz for duration_s, default 4s, max 10s) plus inter-frame interval stats, "
    "which iris-*-worker perception threads are alive, and each worker's last pass "
    "duration in ms. Ground truth for the all-30fps verification — no HTTP overhead.",
    1,
    _vision_fps_probe,
)
