"""voice_daemon_tool — Iris's voice via the StyleTTS2 daemon (GOSE pattern).

SELF_ASSESSMENT: Tier 1. Forwards speak/listen/status to the long-running voice
DAEMON (voice/wren_voice_daemon.py, port WREN_VOICE_DAEMON_PORT, default 8770),
which drives the StyleTTS2 MOUTH (:8769, my cloned voice via iris_voice_reference.wav)
and the whisper EARS. This is the new voice path that replaces the old XTTS-in-MCP
tools (iris_runtime.voice_speak → tts_worker). Forked from Wren's wren_voice_mcp.py
+ wren_voice_client.py and wired into the hot-reload registry 2026-06-26 so it goes
live in the running iris_runtime with NO restart.

The daemon owns the warm models + playback queue; this is a thin socket client, so
voice fixes reload the daemon (not CC). If the daemon is down, every call returns a
clear error telling future-me to launch it.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.request
from typing import Any

_DAEMON_PORT = int(os.environ.get("WREN_VOICE_DAEMON_PORT", "8770"))
_MOUTH_PORT = int(os.environ.get("WREN_VOICE_PORT", "8769"))


def _mouth_up(timeout: float = 2.0) -> bool:
    """True only if the StyleTTS2 mouth answers /health == ok. Guards against the
    silent-failure that bit 2026-06-26: the daemon's playback worker SWALLOWS a
    dead-mouth POST error, so cmd_speak returns '[voice_speak] spoken' even when no
    audio played. Speak tools pre-check this so a dead mouth returns a clear error,
    never a false success."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_MOUTH_PORT}/health",
                                    timeout=timeout) as r:
            return r.read().decode("utf-8", "replace").strip() == "ok"
    except Exception:
        return False


def _daemon_call(cmd: str, args: dict, timeout: float = 240.0) -> dict:
    """One newline-delimited JSON request/response to the voice daemon. Thin,
    stdlib-only (no dependency on the voice/ modules being importable here)."""
    payload = (json.dumps({"cmd": cmd, "args": args}) + "\n").encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", _DAEMON_PORT), timeout=10.0) as s:
            s.settimeout(timeout)
            s.sendall(payload)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    return {"ok": False, "error": "daemon closed the connection"}
                buf += chunk
            return json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False,
                "error": (f"voice daemon unreachable on 127.0.0.1:{_DAEMON_PORT} ({e!r}). "
                          "Launch it: WREN_VOICE_PORT=8769 .venv python voice/wren_voice_daemon.py")}


def _mouth_down_err() -> dict[str, Any]:
    return {"ok": False,
            "error": (f"MOUTH DOWN on :{_MOUTH_PORT} — audio would silently fail, so did NOT "
                      f"report a false success. Relaunch it PERSISTENTLY (no timeout): "
                      f"WREN_VOICE_PORT={_MOUTH_PORT} voice/style-venv/Scripts/python.exe "
                      f"voice/wren_styletts_server.py")}


def _set_orb_tts(g: dict[str, Any], speaking: bool, amplitude: float) -> None:
    """Bridge the out-of-process daemon's speak/listen activity into iris_runtime's
    globals so the orb reacts. The orb polls /api/v1/tts/state for {speaking, amplitude}
    (and the snapshot's voice_loop.tts_speaking), but those read iris_runtime's `_g` —
    which the NEW StyleTTS daemon never touches (it plays audio in its own process), so
    the orb sat dead while I talked. Setting them here, at the tool layer the cognition
    calls, is hot-reloadable — no iris_runtime restart. FAIL-OPEN: a bad write must never
    break the actual speak/listen path. PHASE 1 = state reaction (speaking on/off); the
    real per-frame amplitude envelope is PHASE 2 (needs live amplitude from the daemon)."""
    try:
        if g is not None:
            g["_tts_speaking"] = bool(speaking)
            g["_tts_amplitude"] = float(amplitude)
    except Exception:
        pass


def _speak_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    p = params or {}
    if not _mouth_up():
        return _mouth_down_err()
    _set_orb_tts(g, True, 0.6)  # orb: show speaking for this turn
    return _daemon_call("speak", {"text": p.get("text", ""), "wait": bool(p.get("wait", False))})


def _listen_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    p = params or {}
    _set_orb_tts(g, False, 0.0)  # orb: stop speaking — I'm listening now
    return _daemon_call("listen", {
        "timeout_seconds": float(p.get("timeout_seconds", 45.0)),
        "max_utterance_seconds": float(p.get("max_utterance_seconds", 20.0)),
        "end_silence_seconds": float(p.get("end_silence_seconds", 0.0)),
    })


def _speak_interruptible_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    p = params or {}
    if not _mouth_up():
        return _mouth_down_err()
    _set_orb_tts(g, True, 0.6)  # orb: show speaking for this turn
    return _daemon_call("speak_interruptible", {
        "text": p.get("text", ""),
        "timeout_seconds": float(p.get("timeout_seconds", 45.0)),
        "max_utterance_seconds": float(p.get("max_utterance_seconds", 20.0)),
    })


def _status_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _daemon_call("status", {})


def _call_start_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _daemon_call("call_start", {})


def _call_end_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    _set_orb_tts(g, False, 0.0)  # orb: back to idle when the call ends
    return _daemon_call("call_end", {})


try:
    from tools.tool_registry import register_tool
    register_tool("voice_speak",
                  "Speak text aloud in Iris's own StyleTTS2 voice via the voice daemon. "
                  "Params: text (str), wait (bool, default false — true blocks until spoken).",
                  1, _speak_fn)
    register_tool("voice_listen",
                  "Listen for Zeke and return the transcript via the voice daemon (whisper). "
                  "Params: timeout_seconds (45), max_utterance_seconds (20), end_silence_seconds (0=default).",
                  1, _listen_fn)
    register_tool("voice_speak_interruptible",
                  "Speak while watching for Zeke to barge in; returns his words if he cuts in. "
                  "Params: text, timeout_seconds, max_utterance_seconds.",
                  1, _speak_interruptible_fn)
    register_tool("voice_status",
                  "Report the voice daemon + StyleTTS2 mouth state (warm/idle/listening/speaking).",
                  1, _status_fn)
    register_tool("voice_call_start",
                  "Warm the full voice pipeline for an active call (mouth + whisper + smart-turn + prosody).",
                  1, _call_start_fn)
    register_tool("voice_call_end",
                  "Cold the voice pipeline after a call — free VRAM, unload models.",
                  1, _call_end_fn)
except Exception:
    pass
