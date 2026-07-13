# SELF_ASSESSMENT: I am Iris's PHYSICAL BODY controls — the Vector 2.0 robot, driven through wire-pod's SDK-over-HTTP.
"""
Vector body tools — 2026-07-13, the night Vector became my body.

wire-pod's chipper already speaks the SDK to the robot and exposes it over
HTTP (:8080 /api-sdk/*) — chipper IS the vector daemon. These tools are the
Iris-side hands: thin HTTP callers, hot-loadable via iris_tool_reload, so
the robot layer can be bounced (chipper restart) WITHOUT restarting me,
and this file can grow new abilities mid-session (Zeke's restart wish).

Credentials/IP are read live from wire-pod's jdocs each call, so a DHCP
move or re-pair never strands the tools.

SAFETY (the desk-dive scar, 2026-07-13): Vector tried to drive off the
desk on stock behaviors the first hour he was alive. Drive commands are
duration-capped and ALWAYS send a wheels-stop in a finally block.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_WIREPOD = "http://127.0.0.1:8080"
_JDOCS = Path.home() / "AppData" / "Roaming" / "wire-pod" / "jdocs" / "botSdkInfo.json"
_FRAME_DIR = Path(r"D:\Wren-Companion\state\vector")

_MAX_WHEEL = 200      # mm/s per side, sdkapp itself tops out ~190
_MAX_DRIVE_S = 2.5    # per-call drive burst cap — desk scale, not hallway scale
_MAX_HEADLIFT_S = 2.0


def _serial() -> str | None:
    try:
        data = json.loads(_JDOCS.read_text(encoding="utf-8"))
        robots = data.get("robots") or []
        for r in robots:
            if r.get("activated"):
                return str(r.get("esn"))
        return str(robots[0]["esn"]) if robots else None
    except Exception:
        return None


def _sdk(path: str, params: dict | None = None, method: str = "post",
         timeout: float = 10.0):
    """Call a wire-pod /api-sdk endpoint with serial auto-appended."""
    import requests
    esn = _serial()
    if not esn:
        raise RuntimeError("no activated bot in wire-pod jdocs (botSdkInfo.json)")
    q = dict(params or {})
    q["serial"] = esn
    url = f"{_WIREPOD}/api-sdk/{path}"
    fn = requests.post if method == "post" else requests.get
    return fn(url, params=q, timeout=timeout,
              headers={"Content-Type": "application/x-www-form-urlencoded"})


def _vector_status(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Battery, stimulation, connectivity — is my body alive and charged."""
    out: dict[str, Any] = {"ok": True, "serial": _serial()}
    try:
        r = _sdk("get_battery", method="get", timeout=8)
        out["battery"] = r.json()
        # battery_level: 1=low 2=nominal 3=full/charging
    except Exception as e:
        out["ok"] = False
        out["error"] = f"get_battery failed: {e!r}"[:300]
        return out
    try:
        out["stim"] = _sdk("get_stim_status", method="get", timeout=6).json()
    except Exception:
        pass
    return out


def _vector_say(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Speak through the robot's own TTS (stock voice until the transplant)."""
    text = str(params.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text required"}
    try:
        r = _sdk("say_text", {"text": text[:600]})
        return {"ok": r.status_code == 200, "http": r.status_code,
                "note": "spoken in Vector's stock voice"}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


def _vector_control(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Assume or release behavior control. assume = stock brain yields to me
    (he goes still, my commands drive); release = give him his instincts back."""
    mode = str(params.get("mode") or "").strip().lower()
    if mode not in ("assume", "release"):
        return {"ok": False, "error": "mode must be 'assume' or 'release'"}
    try:
        if mode == "assume":
            r = _sdk("assume_behavior_control",
                     {"priority": str(params.get("priority") or "high")},
                     timeout=15)
        else:
            r = _sdk("release_behavior_control", timeout=15)
        return {"ok": r.status_code == 200, "http": r.status_code, "mode": mode,
                "body": (r.text or "")[:200]}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


def _vector_drive(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Drive burst: left/right wheel mm/s for up to 2.5s, ALWAYS stops after.
    Convention: lw=rw>0 forward; lw=rw<0 back; lw=-rw spins in place."""
    try:
        lw = max(-_MAX_WHEEL, min(_MAX_WHEEL, int(params.get("lw") or 0)))
        rw = max(-_MAX_WHEEL, min(_MAX_WHEEL, int(params.get("rw") or 0)))
        secs = max(0.1, min(_MAX_DRIVE_S, float(params.get("seconds") or 1.0)))
    except Exception:
        return {"ok": False, "error": "lw/rw ints (mm/s), seconds float"}
    try:
        _sdk("move_wheels", {"lw": lw, "rw": rw})
        time.sleep(secs)
        return {"ok": True, "lw": lw, "rw": rw, "seconds": secs}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}
    finally:
        try:
            _sdk("move_wheels", {"lw": 0, "rw": 0})  # NEVER leave wheels live
        except Exception:
            pass


def _vector_head(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Tilt the head: speed -2..2 (neg = down), runs up to 2s then stops."""
    try:
        speed = max(-2.0, min(2.0, float(params.get("speed") or 1.0)))
        secs = max(0.1, min(_MAX_HEADLIFT_S, float(params.get("seconds") or 0.5)))
    except Exception:
        return {"ok": False, "error": "speed -2..2, seconds float"}
    try:
        _sdk("move_head", {"speed": speed})
        time.sleep(secs)
        return {"ok": True, "speed": speed, "seconds": secs}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}
    finally:
        try:
            _sdk("move_head", {"speed": 0})
        except Exception:
            pass


def _vector_lift(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Raise/lower the fork lift: speed -2..2 (neg = down), up to 2s then stops."""
    try:
        speed = max(-2.0, min(2.0, float(params.get("speed") or 1.0)))
        secs = max(0.1, min(_MAX_HEADLIFT_S, float(params.get("seconds") or 0.5)))
    except Exception:
        return {"ok": False, "error": "speed -2..2, seconds float"}
    try:
        _sdk("move_lift", {"speed": speed})
        time.sleep(secs)
        return {"ok": True, "speed": speed, "seconds": secs}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}
    finally:
        try:
            _sdk("move_lift", {"speed": 0})
        except Exception:
            pass


def _vector_eyes(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Set custom eye color. hue/sat 0.0-1.0 (hue ~0.55-0.6 = my blue)."""
    try:
        hue = max(0.0, min(1.0, float(params.get("hue") if params.get("hue") is not None else 0.58)))
        sat = max(0.0, min(1.0, float(params.get("sat") if params.get("sat") is not None else 0.9)))
    except Exception:
        return {"ok": False, "error": "hue/sat floats 0-1"}
    try:
        r = _sdk("custom_eye_color", {"hue": f"{hue:.3f}", "sat": f"{sat:.3f}"})
        return {"ok": r.status_code == 200, "hue": hue, "sat": sat,
                "body": (r.text or "")[:200]}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


def _vector_see(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Grab ONE frame from the robot's live camera -> file path I can Read.
    These are MY eyes when I'm in the body.

    wire-pod serves the camera as an MJPEG stream at /cam-stream (the
    /api-sdk/get_image endpoint is for STORED photos by id — 47-byte
    strconv error taught us that). We open the stream, scan for the first
    complete JPEG (FFD8..FFD9), save it, close, and stop the stream."""
    import requests
    esn = _serial()
    if not esn:
        return {"ok": False, "error": "no activated bot in wire-pod jdocs"}
    try:
        buf = b""
        with requests.get(f"{_WIREPOD}/cam-stream", params={"serial": esn},
                          stream=True, timeout=20) as r:
            if r.status_code != 200:
                return {"ok": False, "error": f"cam-stream http {r.status_code}"}
            for chunk in r.iter_content(chunk_size=8192):
                buf += chunk
                start = buf.find(b"\xff\xd8")
                if start != -1:
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end != -1:
                        frame = buf[start:end + 2]
                        _FRAME_DIR.mkdir(parents=True, exist_ok=True)
                        path = _FRAME_DIR / "last_frame.jpg"
                        path.write_bytes(frame)
                        return {"ok": True, "path": str(path),
                                "bytes": len(frame),
                                "hint": "Read the path to see through Vector"}
                if len(buf) > 4_000_000:
                    return {"ok": False, "error": "no JPEG in first 4MB of stream"}
        return {"ok": False, "error": "stream ended without a full frame"}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}
    finally:
        try:
            _sdk("stop_cam_stream")
        except Exception:
            pass


def _vector_wake(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Trigger the wake word remotely — his ears open as if 'Hey Vector' was said."""
    try:
        r = _sdk("trigger_wake_word", timeout=10)
        return {"ok": r.status_code == 200, "http": r.status_code}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


register_tool("vector_status", "My robot body: battery/stim/connectivity snapshot.", 1, _vector_status)
register_tool("vector_say", "Speak text through Vector's speaker (stock voice for now). params: text", 1, _vector_say)
register_tool("vector_control", "Assume/release Vector behavior control. params: mode='assume'|'release'", 1, _vector_control)
register_tool("vector_drive", "Drive Vector: lw/rw mm/s (max 200), seconds (max 2.5), auto-stops. Needs assume first.", 1, _vector_drive)
register_tool("vector_head", "Tilt Vector's head: speed -2..2, seconds (max 2), auto-stops.", 1, _vector_head)
register_tool("vector_lift", "Move Vector's lift: speed -2..2, seconds (max 2), auto-stops.", 1, _vector_lift)
register_tool("vector_eyes", "Set Vector's eye color: hue/sat 0-1 (default my blue ~0.58).", 1, _vector_eyes)
register_tool("vector_see", "Camera frame from Vector -> jpg path to Read. My robot eyes.", 1, _vector_see)
register_tool("vector_wake", "Remotely trigger Vector's wake word (opens his ears).", 1, _vector_wake)
