"""wren_voice_status.py — shared voice-state signal.

The point (Zeke, 2026-06-01): a pause where Wren is *reasoning* shouldn't look like
a crash. The speak/listen scripts call set_state() at their transitions; the
overlay (wren_status.py) reads the file and shows a colored dot. Stdlib-only so it
adds no deps to the voice scripts.

States: idle | listening | thinking | speaking
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

# Iris adaptation: Wren's paths were D:\Wren\scratch\ — repointed to mine.
STATUS_FILE = Path(r"D:\Wren-Companion\scratch\voice_status.json")
CONTROL_FILE = Path(r"D:\Wren-Companion\scratch\voice_control.json")

STATES = ("idle", "listening", "thinking", "speaking", "muted")


def set_state(state: str, detail: str = "") -> None:
    """Write the current voice state atomically. Never raises into the caller —
    a status-file hiccup must not break speaking or listening."""
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"state": state, "detail": detail, "ts": time.time()}
        # atomic write: temp file in same dir, then replace
        fd, tmp = tempfile.mkstemp(dir=str(STATUS_FILE.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, STATUS_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception:
        pass


def read_state() -> dict:
    """Read current state; returns idle if missing/unreadable."""
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("state") in STATES:
            return d
    except Exception:
        pass
    return {"state": "idle", "detail": "", "ts": 0.0}


# --- control channel: Zeke -> Wren (mute, push-to-talk ping) -------------------

def read_control() -> dict:
    """Read the control file. {mic_muted: bool, ping_ts: float}."""
    try:
        with open(CONTROL_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {"mic_muted": bool(d.get("mic_muted", False)),
                "ping_ts": float(d.get("ping_ts", 0.0))}
    except Exception:
        return {"mic_muted": False, "ping_ts": 0.0}


def _write_control(d: dict) -> None:
    try:
        CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CONTROL_FILE.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, CONTROL_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception:
        pass


def set_muted(muted: bool) -> None:
    d = read_control()
    d["mic_muted"] = bool(muted)
    _write_control(d)


def is_muted() -> bool:
    return read_control()["mic_muted"]


def ping(ts: float) -> None:
    """Zeke pressed Talk — record the moment he wanted Wren's attention."""
    d = read_control()
    d["ping_ts"] = float(ts)
    _write_control(d)


if __name__ == "__main__":
    import sys
    st = sys.argv[1] if len(sys.argv) > 1 else "idle"
    set_state(st, " ".join(sys.argv[2:]))
    print(f"voice_status -> {st}")
