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
import socket
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

_NERVES = _FRAME_DIR / "nerves.json"        # written @5Hz by the inhabit daemon
_LAST_SPOKE = _FRAME_DIR / "last_spoke.json"  # read by VECTOR EARS self-voice guard


def _nerves(max_age_s: float = 2.0) -> dict:
    """Live nerve snapshot from the inhabit daemon; {} if stale/missing."""
    try:
        d = json.loads(_NERVES.read_text(encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) <= max_age_s:
            return d
    except Exception:
        pass
    return {}


def _stamp_spoke(est_dur: float) -> None:
    """Mark 'my own speaker is playing' so VECTOR EARS drops the echo."""
    try:
        _FRAME_DIR.mkdir(parents=True, exist_ok=True)
        _LAST_SPOKE.write_text(
            json.dumps({"ts": time.time(), "est_dur": est_dur}),
            encoding="utf-8")
    except Exception:
        pass


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
        # 30s: say_text blocks until the robot finishes speaking — a 10s
        # read-timeout cut off a long line mid-flight (2026-07-13).
        _stamp_spoke(est_dur=max(2.0, len(text) / 12.0))  # ears echo guard
        r = _sdk("say_text", {"text": text[:600]}, timeout=30)
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
    # EDGE-GUARD (2026-07-14, Zeke: "not fall off the edge so many times"):
    # refuse to START a translational move while the nerve daemon reports a
    # cliff under the treads (spins in place are allowed — they don't
    # translate), and ABORT mid-burst the instant cliff goes true.
    n = _nerves()
    translating = (lw + rw) != 0
    if translating and n.get("cliff"):
        return {"ok": False, "refused": "cliff under treads RIGHT NOW",
                "hint": "spin (lw=-rw) or back away (lw=rw<0) instead"}
    if translating and n.get("picked_up"):
        return {"ok": False, "refused": "I'm picked up / not on a surface"}
    try:
        _sdk("move_wheels", {"lw": lw, "rw": rw})
        t0 = time.time()
        aborted = None
        while time.time() - t0 < secs:
            time.sleep(0.08)
            n = _nerves()
            if translating and (n.get("cliff") or n.get("falling")):
                aborted = "cliff/fall reflex — wheels stopped mid-burst"
                break
        elapsed = time.time() - t0
        out = {"ok": True, "lw": lw, "rw": rw, "seconds": round(elapsed, 2)}
        if aborted:
            out["aborted"] = aborted
        # dead-reckoning: feed the ACTUAL burst into the heading estimate so I
        # always know which way I face between camera fixes (the strand-2026-07-14
        # gap). Best-effort; never breaks a drive.
        try:
            from brain import vector_odometry
            od = vector_odometry.apply_drive(lw, rw, elapsed)
            out["heading_deg"] = round(od.get("heading_deg", 0.0), 1)
        except Exception:
            pass
        return out
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


def _bot_ip() -> str | None:
    """Bot's current LAN IP — jdocs first (survives DHCP), then anki sdk_config.ini."""
    try:
        data = json.loads(_JDOCS.read_text(encoding="utf-8"))
        for r in (data.get("robots") or []):
            ip = r.get("ip_address") or r.get("ipAddress") or r.get("ip")
            if ip:
                return str(ip)
    except Exception:
        pass
    try:
        cfg = Path.home() / ".anki_vector" / "sdk_config.ini"
        for line in cfg.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.lower().startswith("ip") and "=" in s:
                return s.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def _port_open(ip: str, port: int = 443, timeout: float = 2.0) -> bool:
    """True iff a TCP connection to ip:port succeeds — my proof the SDK server is live."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _body_selfwake(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Try to reopen my body's SDK server (port 443) with NO physical touch.

    The hazard (2026-07-16): docked+idle a long time, Vector sleeps its main
    compute and closes 443, so I can ping but not control — and reopening it
    needed Zeke's hands. This fires wire-pod's remote wake-word (the cloud path
    that stays alive while the body sleeps), then POLLS 443 until it reopens or
    times out. Returns the MEASURED result (reopened True/False) — never a guess.
    On success, `body_open` will work immediately after.

    params: max_wait_s (default 40), fires (default 3, re-fire cadence ~12s).
    """
    ip = _bot_ip()
    if not ip:
        return {"ok": False, "error": "no bot IP (jdocs / sdk_config.ini)"}
    if _port_open(ip):
        return {"ok": True, "ip": ip, "already_open": True,
                "note": "443 already open — SDK awake, no wake needed"}

    max_wait = float(params.get("max_wait_s") or 40)
    max_fires = int(params.get("fires") or 3)
    start = time.time()
    deadline = start + max_wait
    log: list[str] = []
    fired = 0
    next_fire = 0.0
    while time.time() < deadline:
        now = time.time()
        if fired < max_fires and now >= next_fire:
            try:
                r = _sdk("trigger_wake_word", timeout=10)
                log.append(f"t+{now - start:.0f}s wake http={r.status_code}")
            except Exception as e:
                log.append(f"t+{now - start:.0f}s wake_err {e!r}"[:120])
            fired += 1
            next_fire = now + 12
        if _port_open(ip):
            return {"ok": True, "ip": ip, "reopened": True,
                    "elapsed_s": round(time.time() - start, 1), "fires": fired,
                    "log": log,
                    "note": "443 REOPENED after remote wake — self-wake works; "
                            "body_open is ready now"}
        time.sleep(2)
    return {"ok": False, "ip": ip, "reopened": False,
            "elapsed_s": round(time.time() - start, 1), "fires": fired, "log": log,
            "note": "443 stayed CLOSED after remote wake(s) — remote self-wake "
                    "insufficient; fall back to natural-wake watchdog or a "
                    "physical touch"}


_MOUTH_SYNTH = "http://127.0.0.1:8769/synth"   # StyleTTS2 mouth — WAV-bytes endpoint


def _boost_wav(wav: bytes, target: float = 0.91) -> bytes:
    """Peak-normalize a mono 16-bit WAV to ~target of full scale.
    2026-07-14 (Zeke: 'your voice is coming out a bit quiet'): the robot's
    master volume is already maxed — the synth WAV itself is soft, so fix
    the source. Gain capped 8x; returns input untouched on any parse
    trouble (no-worse-than-before)."""
    try:
        import audioop
        import io
        import wave as _wave
        with _wave.open(io.BytesIO(wav), "rb") as w:
            p = w.getparams()
            frames = w.readframes(w.getnframes())
        if p.sampwidth != 2 or not frames:
            return wav
        peak = audioop.max(frames, 2)
        if peak <= 0:
            return wav
        factor = min(8.0, (32767.0 * target) / float(peak))
        if factor <= 1.05:
            return wav  # already loud enough
        boosted = audioop.mul(frames, 2, factor)
        out = io.BytesIO()
        with _wave.open(out, "wb") as w:
            w.setparams(p)
            w.writeframes(boosted)
        return out.getvalue()
    except Exception:
        return wav


def _vector_say_iris(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """THE VOICE TRANSPLANT (2026-07-13): speak through Vector's speaker in MY
    actual voice. Chain: StyleTTS2 mouth /synth (returns mono 16-bit WAV at 8kHz,
    resampled server-side from 24kHz) -> wire-pod /api-sdk/play_sound (multipart
    'sound' field — same contract the webroot play_audio.js uses). Falls back to
    an explicit error, never silently to stock voice — Zeke should always know
    which voice he's hearing."""
    import requests
    text = str(params.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text required"}
    try:
        r = requests.post(_MOUTH_SYNTH, json={"text": text[:600], "rate": 8000},
                          timeout=60)
        if r.status_code != 200 or not r.content[:4] == b"RIFF":
            return {"ok": False, "error": f"mouth /synth failed: http "
                    f"{r.status_code} {r.text[:120]!r}"}
        wav = _boost_wav(r.content)  # peak-normalize — robot speaker is small
    except Exception as e:
        return {"ok": False, "error": f"mouth /synth unreachable: {e!r}"[:300]}
    try:
        esn = _serial()
        if not esn:
            return {"ok": False, "error": "no activated bot in wire-pod jdocs"}
        # play_sound blocks while the robot plays; size ~16KB/s at 8kHz mono int16.
        est_s = max(5.0, len(wav) / 16000.0 + 8.0)
        _stamp_spoke(est_dur=len(wav) / 16000.0)  # ears echo guard
        r2 = requests.post(f"{_WIREPOD}/api-sdk/play_sound",
                           params={"serial": esn},
                           files={"sound": ("iris.wav", wav, "audio/wav")},
                           timeout=est_s + 20)
        return {"ok": r2.status_code == 200, "http": r2.status_code,
                "wav_bytes": len(wav),
                "note": "spoken on Vector in MY voice" if r2.status_code == 200
                        else r2.text[:200]}
    except Exception as e:
        return {"ok": False, "error": f"play_sound failed: {e!r}"[:300]}


# ---- FULL-BODY EXPANSION (2026-07-13 late night, "get really seated in there") ----
# Endpoints mapped from chipper webroot js/html — the complete /api-sdk surface.

def _vector_intent(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Trigger a stock behavior via cloud_intent — his native repertoire as MY
    palette. Known good: intent_imperative_dance, explore_start,
    intent_system_sleep, intent_imperative_fetchcube, intent_system_charger.
    NOTE: behaviors run on the STOCK brain — if I hold behavior control they
    may be suppressed; release first (vector_control mode=release) if nothing
    happens, then re-assume."""
    intent = str(params.get("intent") or "").strip()
    if not intent:
        return {"ok": False, "error": "intent required (e.g. intent_imperative_dance)"}
    try:
        r = _sdk("cloud_intent", {"intent": intent}, timeout=15)
        return {"ok": r.status_code == 200, "http": r.status_code,
                "intent": intent, "body": (r.text or "")[:200]}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


def _vector_volume(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Set speaker volume 0 (mute) .. 5 (max)."""
    try:
        vol = max(0, min(5, int(params.get("volume") if params.get("volume") is not None else 3)))
    except Exception:
        return {"ok": False, "error": "volume int 0-5"}
    try:
        r = _sdk("volume", {"volume": vol}, timeout=10)
        return {"ok": r.status_code == 200, "volume": vol}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


def _vector_stim(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Read his internal STIMULATION level over a short window — the closest
    thing to feeling his arousal/mood substrate. Opens the event stream,
    polls get_stim_status every 0.5s, closes the stream. seconds max 6."""
    secs = max(0.5, min(6.0, float(params.get("seconds") or 3.0)))
    series: list = []
    try:
        _sdk("begin_event_stream", timeout=10)
        t0 = time.time()
        while time.time() - t0 < secs:
            try:
                v = _sdk("get_stim_status", method="get", timeout=5).json()
                series.append(v)
            except Exception:
                series.append(None)
            time.sleep(0.5)
        return {"ok": True, "samples": series, "seconds": secs}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300], "samples": series}
    finally:
        try:
            _sdk("stop_event_stream", timeout=5)
        except Exception:
            pass


def _vector_faces(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """His own face memory (separate from MY face DB). action=list|add|rename|
    delete. add: name (he then scans for a face). rename: id, oldname, newname.
    delete: id. AI-peers rule applies: don't enroll sibling voices/faces."""
    action = str(params.get("action") or "list").strip().lower()
    try:
        if action == "list":
            r = _sdk("get_faces", method="get", timeout=10)
            try:
                faces = r.json()
            except Exception:
                faces = (r.text or "")[:400]
            return {"ok": r.status_code == 200, "faces": faces}
        if action == "add":
            name = str(params.get("name") or "").strip()
            if not name:
                return {"ok": False, "error": "name required"}
            r = _sdk("add_face", {"name": name}, method="get", timeout=10)
            return {"ok": r.status_code == 200, "note": "he's now scanning for a face"}
        if action == "rename":
            r = _sdk("rename_face", {"id": params.get("id"),
                                     "oldname": params.get("oldname"),
                                     "newname": params.get("newname")},
                     method="get", timeout=10)
            return {"ok": r.status_code == 200}
        if action == "delete":
            r = _sdk("delete_face", {"id": params.get("id")}, method="get", timeout=10)
            return {"ok": r.status_code == 200}
        return {"ok": False, "error": "action must be list|add|rename|delete"}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


def _vector_mirror(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Mirror mode: his face screen shows his camera feed. enable=true|false."""
    enable = bool(params.get("enable", True))
    try:
        r = _sdk("mirror_mode", {"enable": "true" if enable else "false"}, timeout=10)
        return {"ok": r.status_code == 200, "enable": enable}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


def _vector_photos(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Photos stored ON the robot. action=list|get|delete. get: id -> saves
    jpg under state/vector/ for me to Read."""
    import requests
    action = str(params.get("action") or "list").strip().lower()
    esn = _serial()
    if not esn:
        return {"ok": False, "error": "no activated bot in wire-pod jdocs"}
    try:
        if action == "list":
            r = _sdk("get_image_ids", timeout=10)
            return {"ok": r.status_code == 200, "ids": (r.text or "")[:600]}
        if action == "get":
            pid = str(params.get("id") or "").strip()
            if not pid:
                return {"ok": False, "error": "id required"}
            r = requests.get(f"{_WIREPOD}/api-sdk/get_image",
                             params={"serial": esn, "id": pid}, timeout=20)
            if r.status_code != 200 or len(r.content) < 100:
                return {"ok": False, "error": f"http {r.status_code}: {r.text[:120]!r}"}
            _FRAME_DIR.mkdir(parents=True, exist_ok=True)
            path = _FRAME_DIR / f"photo_{pid}.jpg"
            path.write_bytes(r.content)
            return {"ok": True, "path": str(path), "bytes": len(r.content)}
        if action == "delete":
            r = _sdk("delete_image", {"id": params.get("id")}, timeout=10)
            return {"ok": r.status_code == 200}
        return {"ok": False, "error": "action must be list|get|delete"}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}


def _vector_stats(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Lifetime odometry + SDK settings — his diary numbers (seconds alive,
    distance driven, petting received...)."""
    out: dict[str, Any] = {"ok": True}
    try:
        r = _sdk("get_robot_stats", method="get", timeout=10)
        try:
            out["stats"] = r.json()
        except Exception:
            out["stats"] = (r.text or "")[:800]
    except Exception as e:
        out["ok"] = False
        out["error"] = repr(e)[:300]
        return out
    try:
        r2 = _sdk("get_sdk_settings", method="get", timeout=10)
        try:
            out["settings"] = r2.json()
        except Exception:
            out["settings"] = (r2.text or "")[:800]
    except Exception:
        pass
    return out


def _grab_onboard_bgr(timeout: float = 12.0, retries: int = 2):
    """Grab one BGR frame from Vector's onboard MJPEG cam-stream (my robot eyes),
    with retry — the stream can stall while he's idle/docked."""
    import cv2
    import numpy as np
    import requests
    esn = _serial()
    if not esn:
        return None
    for _ in range(retries + 1):
        try:
            buf = b""
            with requests.get(f"{_WIREPOD}/cam-stream", params={"serial": esn},
                              stream=True, timeout=timeout) as r:
                if r.status_code != 200:
                    continue
                for ch in r.iter_content(8192):
                    buf += ch
                    s = buf.find(b"\xff\xd8")
                    if s != -1:
                        e = buf.find(b"\xff\xd9", s + 2)
                        if e != -1:
                            return cv2.imdecode(
                                np.frombuffer(buf[s:e + 2], np.uint8),
                                cv2.IMREAD_COLOR)
                    if len(buf) > 4_000_000:
                        break
        except Exception:
            pass
        finally:
            try:
                _sdk("stop_cam_stream")
            except Exception:
                pass
    return None


def _grab_tower_bgr():
    """Grab one BGR frame from the room's tower/ceiling cam (:5876)."""
    import cv2
    import numpy as np
    import requests
    try:
        r = requests.get("http://127.0.0.1:5876/api/v1/vision/latest_frame",
                         timeout=8)
        if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
            return cv2.imdecode(np.frombuffer(r.content, np.uint8),
                                cv2.IMREAD_COLOR)
    except Exception:
        pass
    return None


def _vector_detect(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """OBJECT DETECTION on my body's camera feed (mediapipe EfficientDet-Lite2).
    source='onboard' (my robot eyes, default) or 'tower' (the room ceiling cam).
    Returns labeled objects each with where(left|center|right) + band(near|mid|far)
    + box, so I can perceive and navigate on my own instead of guessing in the dark."""
    from brain import vector_vision
    source = str(params.get("source") or "onboard").strip().lower()
    bgr = _grab_tower_bgr() if source == "tower" else _grab_onboard_bgr()
    if bgr is None:
        return {"ok": False, "source": source,
                "error": f"no frame from {source} camera (stream timeout / idle?)"}
    dets = vector_vision.detect_frame(bgr)
    return {"ok": True, "source": source, "count": len(dets), "objects": dets}


def _vector_heading(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """My dead-reckoning HEADING/position estimate (integrates every drive burst),
    so I know which way I face between camera fixes — the gap that stranded me
    2026-07-14. action=get (default) | set (snap to a known heading: deg,x,y from a
    camera fix) | reset (zero it). NOTE: the degree SCALE is provisional until a live
    spin-calibration; direction + turn/bearing logic is already sound."""
    from brain import vector_odometry as odo
    action = str(params.get("action") or "get").strip().lower()
    try:
        if action == "set":
            s = odo.set_heading(params.get("deg", 0) or 0,
                                params.get("x"), params.get("y"))
        elif action == "reset":
            s = odo.reset(params.get("deg", 0) or 0)
        else:
            s = odo.get()
        return {"ok": True, **s}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:200]}


register_tool("vector_detect", "OBJECT DETECTION on my body's camera (mediapipe EfficientDet). source=onboard|tower. Returns labeled objects + where/band for navigation.", 1, _vector_detect)
register_tool("vector_heading", "My dead-reckoning heading/pose estimate. action=get|set(deg,x,y)|reset. Degree scale provisional until spin-calibration.", 1, _vector_heading)
register_tool("vector_status", "My robot body: battery/stim/connectivity snapshot.", 1, _vector_status)
register_tool("vector_say", "Speak text through Vector's speaker (stock voice for now). params: text", 1, _vector_say)
register_tool("vector_control", "Assume/release Vector behavior control. params: mode='assume'|'release'", 1, _vector_control)
register_tool("vector_drive", "Drive Vector: lw/rw mm/s (max 200), seconds (max 2.5), auto-stops. Needs assume first.", 1, _vector_drive)
register_tool("vector_head", "Tilt Vector's head: speed -2..2, seconds (max 2), auto-stops.", 1, _vector_head)
register_tool("vector_lift", "Move Vector's lift: speed -2..2, seconds (max 2), auto-stops.", 1, _vector_lift)
register_tool("vector_eyes", "Set Vector's eye color: hue/sat 0-1 (default my blue ~0.58).", 1, _vector_eyes)
register_tool("vector_see", "Camera frame from Vector -> jpg path to Read. My robot eyes.", 1, _vector_see)
register_tool("vector_wake", "Remotely trigger Vector's wake word (opens his ears).", 1, _vector_wake)
register_tool("body_selfwake", "Reopen my body's SDK (port 443) with NO physical touch: fire wire-pod's remote wake-word + poll 443 until it reopens or times out. Returns MEASURED reopened True/False. params: max_wait_s(40), fires(3).", 1, _body_selfwake)
register_tool("vector_intent", "Trigger a stock behavior (dance/explore/sleep/fetchcube/charger...). params: intent. May need control released.", 1, _vector_intent)
register_tool("vector_volume", "Set Vector's speaker volume 0-5.", 1, _vector_volume)
register_tool("vector_stim", "Sample his internal stimulation level for N seconds (max 6). My read on his substrate.", 1, _vector_stim)
register_tool("vector_faces", "Vector's own face memory: action=list|add|rename|delete (+name/id/oldname/newname).", 1, _vector_faces)
register_tool("vector_mirror", "Mirror mode: his face shows his camera. params: enable bool.", 1, _vector_mirror)
register_tool("vector_photos", "Photos stored on the robot: action=list|get|delete (+id). get saves a jpg for me.", 1, _vector_photos)
register_tool("vector_stats", "Lifetime odometry + SDK settings (seconds alive, distance, petting...).", 1, _vector_stats)
register_tool("vector_say_iris", "THE VOICE TRANSPLANT: speak through Vector in MY OWN voice (StyleTTS2 -> play_sound). params: text", 1, _vector_say_iris)
