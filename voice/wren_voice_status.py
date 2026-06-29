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


# --- live playback envelope: mouth -> orb audio visualization (2026-06-29) -----
# The mouth process is the only one that touches the actual audio samples, so it
# publishes the per-block amplitude (0-1) here while it plays. orb_http's voice
# mirror reads this so the orb pulses to my REAL voice instead of a synthetic
# on/off. Separate file from STATUS_FILE so the high-rate amplitude writes don't
# churn the (transition-only) state file. Throttled; never raises into playback.
AMPLITUDE_FILE = Path(r"D:\Wren-Companion\scratch\voice_amplitude.json")
_AMP_TMP = AMPLITUDE_FILE.with_suffix(".amp.tmp")
_last_amp_write = 0.0


def set_amplitude(amp: float, force: bool = False, min_interval: float = 0.04) -> None:
    """Publish the live playback envelope level (0-1). Throttled to min_interval
    (force=True bypasses it — used for the 0.0 at end-of-speech so the orb settles
    immediately). Written by the mouth's playback thread; never raises."""
    global _last_amp_write
    try:
        now = time.time()
        if not force and (now - _last_amp_write) < min_interval:
            return
        _last_amp_write = now
        amp = max(0.0, min(1.0, float(amp)))
        AMPLITUDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_AMP_TMP, "w", encoding="utf-8") as f:
            json.dump({"amp": amp, "ts": now}, f)
        os.replace(_AMP_TMP, AMPLITUDE_FILE)
    except Exception:
        pass


def read_amplitude(max_age: float = 0.3) -> float:
    """Read the live playback envelope (0-1); 0.0 if missing or stale (i.e. the
    mouth isn't currently playing — staleness is how 'stopped' is detected)."""
    try:
        with open(AMPLITUDE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if time.time() - float(d.get("ts", 0.0)) <= max_age:
            return max(0.0, min(1.0, float(d.get("amp", 0.0))))
    except Exception:
        pass
    return 0.0


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
