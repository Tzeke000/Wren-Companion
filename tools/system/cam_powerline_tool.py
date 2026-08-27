"""cam_powerline — read/set the camera's anti-flicker (powerline frequency) via WinRT.

Built 2026-08-27 during the 30fps hunt. Evidence: AE-chosen exposures quantize
to 48-50ms / 60-64ms — multiples of the 10ms 50Hz-flicker period. US mains is
60Hz (8.33ms steps: 33.3ms → 30fps). If the PIXY shipped in 50Hz anti-flicker
mode, AE can never pick 33ms and the camera is stuck at 20/16fps in artificial
light. Setting powerline to 60Hz (or auto) should free it.

Uses the SAME WinRT MediaCapture control-channel trick as the PTZ actuator
(control connection coexists with the runtime's DSHOW capture handle). Reuses
the actuator's cached controller when present so we don't disturb the PTZ scar
(eyes_reload → release_ptz etc.); otherwise opens one via the same class.

Actions:
  get                — report current powerline frequency
  set {value}        — 'disabled' | '50hz' | '60hz' | 'auto'
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool

_VALUES = {"disabled": 0, "50hz": 1, "60hz": 2, "auto": 3}
_NAMES = {v: k for k, v in _VALUES.items()}


def _get_vdc(g: dict[str, Any]):
    """The PTZ actuator's cached VideoDeviceController, or a fresh one via
    the exact same class machinery (shared cache, shared lock)."""
    from brain.visual_attention import WinRtPtzActuator
    if WinRtPtzActuator._ctl_vdc is not None:
        return WinRtPtzActuator._ctl_vdc
    act = WinRtPtzActuator()
    return act._controller()


def _cam_powerline(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    import winsdk.windows.media.capture as wmc  # PowerlineFrequency lives here

    try:
        vdc = _get_vdc(g)
    except Exception as e:
        return {"ok": False, "error": f"controller unavailable: {e!r}"}

    def read() -> dict[str, Any]:
        try:
            res = vdc.try_get_powerline_frequency()
            # winsdk maps the out-param overload to a tuple OR an object;
            # normalize defensively.
            if isinstance(res, tuple):
                ok, val = bool(res[0]), res[1]
            else:
                ok, val = bool(res), None
            iv = int(val) if val is not None else None
            return {"supported": ok, "value": iv,
                    "name": _NAMES.get(iv, str(iv)) if iv is not None else None}
        except Exception as e:
            return {"supported": False, "error": repr(e)}

    action = str(params.get("action") or "get")
    if action == "get":
        return {"ok": True, "powerline": read()}
    if action == "controls":
        # Survey the richer WinRT camera controls (support + ranges).
        out: dict[str, Any] = {}
        for name in ("exposure_control", "exposure_compensation_control",
                     "iso_speed_control", "flash_control", "white_balance_control",
                     "hdr_video_control", "low_light_fusion",
                     "desired_optimization", "advanced_photo_control"):
            try:
                c = getattr(vdc, name, None)
                if c is None:
                    out[name] = None
                    continue
                info: dict[str, Any] = {}
                for attr in ("supported", "min", "max", "step", "value", "auto",
                             "supported_modes", "mode"):
                    try:
                        v = getattr(c, attr)
                        if attr == "supported_modes":
                            v = [str(m) for m in v]
                        info[attr] = v if isinstance(v, (bool, int, float, str, list)) else str(v)
                    except Exception:
                        pass
                out[name] = info if info else str(c)
            except Exception as e:
                out[name] = f"err:{e!r}"
        return {"ok": True, "controls": out}
    if action == "optimize":
        # MediaCaptureOptimization: 0=Default 1=Quality 2=Latency 3=Power
        # 4=LatencyThenQuality 5=LatencyThenPower 6=PhotoSequence.
        # On some drivers Latency maps to UVC AE-priority (keep frame rate).
        import winsdk.windows.media.devices as _wmd
        val = int(params.get("value") if params.get("value") is not None else 2)
        try:
            vdc.desired_optimization = _wmd.MediaCaptureOptimization(val)
            return {"ok": True, "set": val,
                    "now": int(vdc.desired_optimization)}
        except Exception as e:
            return {"ok": False, "error": f"set failed: {e!r}"}
    if action == "evcomp":
        # Set exposure-compensation (EV bias). Negative = darker = shorter
        # exposures from AE = higher fps. Async setter, run synchronously.
        from brain.visual_attention import WinRtPtzActuator as _A
        value = float(params.get("value") or 0.0)
        cc = vdc.exposure_compensation_control
        if not getattr(cc, "supported", False):
            return {"ok": False, "error": "exposure_compensation not supported"}
        try:
            _A._run(cc.set_value_async(value))
            return {"ok": True, "set": value,
                    "now": {"value": float(cc.value),
                            "min": float(cc.min), "max": float(cc.max),
                            "step": float(cc.step)}}
        except Exception as e:
            return {"ok": False, "error": f"set failed: {e!r}"}
    if action == "set":
        want = str(params.get("value") or "").lower()
        if want not in _VALUES:
            return {"ok": False, "error": f"value must be one of {list(_VALUES)}"}
        try:
            pf = wmc.PowerlineFrequency(_VALUES[want])
            set_ok = bool(vdc.try_set_powerline_frequency(pf))
        except Exception as e:
            return {"ok": False, "error": f"set failed: {e!r}", "before": read()}
        return {"ok": True, "set_ok": set_ok, "wanted": want, "after": read()}
    return {"ok": False, "error": f"unknown action {action!r}"}


register_tool(
    "cam_powerline",
    "Read/set the camera's anti-flicker powerline frequency via the WinRT "
    "control channel (coexists with the live DSHOW capture — no restart). "
    "action='get' reads; action='set' with value '60hz'|'50hz'|'auto'|'disabled'. "
    "50Hz mode quantizes AE exposure to 50/60ms = 20/16fps; 60Hz allows 33ms = "
    "30fps. Re-measure with vision_fps_probe after set.",
    1,
    _cam_powerline,
)
