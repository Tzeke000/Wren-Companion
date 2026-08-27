"""cam_live_ctl — reach the LIVE cv2.VideoCapture inside the running capture loop.

Built 2026-08-27 (Zeke: "can we try and get everything to 30FPS before we
restart you"). `cap` is a local variable in _iris_video_capture_loop, so no
tool can normally touch it — but sys._current_frames() exposes the running
thread's frame, and f_locals hands us the real VideoCapture object. That lets
us READ what the device granted (fps/fourcc/exposure) and SET props live,
without a restart and without stealing the device.

Race note: the capture thread is concurrently inside cap.read(). Property
gets/sets on another thread are brief C calls into the DSHOW graph — in
practice safe, but keep sets sparse and re-measure with vision_fps_probe.

Actions:
  props            — read the interesting props
  set {props:{...}} — set props by NAME (fps, fourcc, auto_exposure, exposure,
                      gain, brightness…). fourcc takes a 4-char string.
"""
from __future__ import annotations

import sys
import threading
from typing import Any, Optional

from tools.tool_registry import register_tool

_PROPS = {
    "fps": "CAP_PROP_FPS",
    "fourcc": "CAP_PROP_FOURCC",
    "width": "CAP_PROP_FRAME_WIDTH",
    "height": "CAP_PROP_FRAME_HEIGHT",
    "auto_exposure": "CAP_PROP_AUTO_EXPOSURE",
    "exposure": "CAP_PROP_EXPOSURE",
    "gain": "CAP_PROP_GAIN",
    "brightness": "CAP_PROP_BRIGHTNESS",
    "buffersize": "CAP_PROP_BUFFERSIZE",
}


def _find_cap() -> Optional[Any]:
    """Locate the live VideoCapture in the iris-video-capture thread's frame."""
    import cv2
    tid = None
    for t in threading.enumerate():
        if t.name == "iris-video-capture":
            tid = t.ident
            break
    if tid is None:
        return None
    frame = sys._current_frames().get(tid)
    while frame is not None:
        cap = frame.f_locals.get("cap")
        if isinstance(cap, cv2.VideoCapture):
            return cap
        frame = frame.f_back
    return None


def _fourcc_str(v: float) -> str:
    i = int(v)
    return "".join(chr((i >> 8 * k) & 0xFF) for k in range(4))


def _read_props(cap: Any) -> dict[str, Any]:
    import cv2
    out: dict[str, Any] = {}
    for name, const in _PROPS.items():
        try:
            v = cap.get(getattr(cv2, const))
            out[name] = _fourcc_str(v) if name == "fourcc" else round(float(v), 3)
        except Exception as e:
            out[name] = f"err:{e!r}"
    return out


def _cam_live_ctl(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    import cv2
    cap = _find_cap()
    if cap is None:
        return {"ok": False,
                "error": "live VideoCapture not found (capture thread down, or cap released/paused)"}
    action = str(params.get("action") or "props")
    if action == "props":
        return {"ok": True, "props": _read_props(cap)}
    if action == "set":
        wanted = params.get("props") or {}
        if not isinstance(wanted, dict) or not wanted:
            return {"ok": False, "error": "set needs props:{name:value}"}
        results: dict[str, Any] = {}
        for name, value in wanted.items():
            const = _PROPS.get(name)
            if const is None:
                results[name] = "unknown prop"
                continue
            try:
                if name == "fourcc" and isinstance(value, str):
                    value = cv2.VideoWriter_fourcc(*value)
                results[name] = bool(cap.set(getattr(cv2, const), float(value)))
            except Exception as e:
                results[name] = f"err:{e!r}"
        return {"ok": True, "set_results": results, "props_after": _read_props(cap)}
    return {"ok": False, "error": f"unknown action {action!r}"}


register_tool(
    "cam_live_ctl",
    "Read/set properties on the LIVE camera handle inside the running capture "
    "loop (found via thread-frame introspection — no restart, no device steal). "
    "action='props' reads fps/fourcc/exposure/etc as GRANTED by the device; "
    "action='set' with props={name:value} sets them (fourcc as 4-char string). "
    "Re-measure with vision_fps_probe after any set.",
    1,
    _cam_live_ctl,
)
