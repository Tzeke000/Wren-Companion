"""
Phase 62 — Clap detection backup wake activation.
Phase 77 — Auto-calibration against ambient noise level.

Listens for two claps within 1 second as alternate wake trigger.
Calibrates threshold on startup: ambient_rms * 3.0 so it works in any environment.
Falls back gracefully if sounddevice unavailable.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional


_DEFAULT_THRESHOLD = 0.4
_CALIBRATION_MULTIPLIER = 5.0
_MIN_THRESHOLD_FLOOR = 0.35  # bumped from 0.15 — keyboard/mouse never crosses this
_TRIGGER_COOLDOWN_SEC = 4.0  # bumped from 3.0
_DOUBLE_WINDOW_SEC = 0.6     # both claps must land within 0.6s of each other (was 0.8)
_MIN_CLAP_SEPARATION_SEC = 0.1  # the two claps must be at least 0.1s apart — prevents one loud sound triggering twice


def _calibration_path(g: dict[str, Any]) -> Path:
    base = Path(g.get("BASE_DIR") or ".")
    return base / "state" / "clap_calibration.json"


def load_calibrated_threshold(g: dict[str, Any]) -> float:
    path = _calibration_path(g)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            t = float(data.get("threshold") or _DEFAULT_THRESHOLD)
            return max(_MIN_THRESHOLD_FLOOR, min(1.0, t))
        except Exception:
            pass
    return _DEFAULT_THRESHOLD


def calibrate_clap_threshold(g: dict[str, Any], duration_seconds: float = 2.0) -> dict[str, Any]:
    """
    Record ambient noise for duration_seconds and set threshold to ambient_rms * multiplier.
    Stores result in state/clap_calibration.json.
    Returns {threshold, ambient_rms, ok}.
    """
    try:
        import numpy as np
        import sounddevice as sd

        SAMPLE_RATE = 16000
        frames = int(SAMPLE_RATE * duration_seconds)
        audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        sd.wait()
        audio = audio.squeeze()
        ambient_rms = float(np.sqrt(np.mean(audio ** 2)))
        threshold = max(_MIN_THRESHOLD_FLOOR, min(0.9, ambient_rms * _CALIBRATION_MULTIPLIER))
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "threshold": _DEFAULT_THRESHOLD}

    data = {
        "threshold": round(threshold, 4),
        "ambient_rms": round(ambient_rms, 4),
        "calibrated_at": time.time(),
        "multiplier": _CALIBRATION_MULTIPLIER,
    }
    path = _calibration_path(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[clap_detect] calibrated threshold={threshold:.4f} ambient={ambient_rms:.4f}")
    return {"ok": True, **data}


class ClapDetector:
    def __init__(self, g: dict[str, Any], on_clap: Optional[Callable] = None):
        self._g = g
        self._on_clap = on_clap
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._clap_times: list[float] = []
        self._threshold: float = _DEFAULT_THRESHOLD
        self._last_trigger_ts: float = 0.0  # cooldown gate

    def start(self) -> bool:
        try:
            import sounddevice as sd  # noqa: F401
            import numpy as np  # noqa: F401
        except ImportError:
            return False

        # Phase 77: load calibrated threshold, then calibrate ambient if no calibration exists
        self._threshold = load_calibrated_threshold(self._g)
        path = _calibration_path(self._g)
        if not path.is_file():
            # Auto-calibrate in background so startup isn't blocked
            threading.Thread(
                target=self._auto_calibrate,
                daemon=True,
                name="ava-clap-calibrate",
            ).start()

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ava-clap-detect")
        self._thread.start()
        return True

    def _auto_calibrate(self) -> None:
        time.sleep(3.0)  # Wait for startup to settle
        result = calibrate_clap_threshold(self._g, duration_seconds=2.0)
        if result.get("ok"):
            self._threshold = float(result.get("threshold") or _DEFAULT_THRESHOLD)

    def stop(self) -> None:
        self._running = False

    def recalibrate(self) -> dict[str, Any]:
        result = calibrate_clap_threshold(self._g, duration_seconds=2.0)
        if result.get("ok"):
            self._threshold = float(result.get("threshold") or _DEFAULT_THRESHOLD)
        return result

    def _loop(self) -> None:
        try:
            import sounddevice as sd
            import numpy as np

            SAMPLE_RATE = 16000
            BLOCK_SIZE = 1600  # 100ms blocks

            def _audio_callback(indata, frames, _time, status):
                if not self._running:
                    return
                if self._g.get("input_muted"):
                    return
                now = time.monotonic()
                # Cooldown: after a trigger, ignore all clap candidates for N seconds
                if now - self._last_trigger_ts < _TRIGGER_COOLDOWN_SEC:
                    return
                rms = float(np.sqrt(np.mean(indata ** 2)))
                if rms > self._threshold:
                    # Reject if this clap is within _MIN_CLAP_SEPARATION_SEC of
                    # the previous one — that's a single loud event, not two.
                    if self._clap_times and (now - self._clap_times[-1]) < _MIN_CLAP_SEPARATION_SEC:
                        return
                    self._clap_times.append(now)
                    self._clap_times = [t for t in self._clap_times if now - t < 3.0]
                    # Tighter window: both claps must land within 0.6s.
                    recent = [t for t in self._clap_times if now - t < _DOUBLE_WINDOW_SEC]
                    if len(recent) >= 2 and (recent[-1] - recent[0]) >= _MIN_CLAP_SEPARATION_SEC:
                        self._clap_times.clear()
                        self._last_trigger_ts = now
                        self._trigger()

            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                blocksize=BLOCK_SIZE,
                callback=_audio_callback,
            ):
                while self._running:
                    time.sleep(0.5)
        except Exception:
            self._running = False

    def _trigger(self) -> None:
        self._g["_wake_word_detected"] = True
        self._g["_wake_word_ts"] = time.time()
        # Clap = explicit wake. wake_detector will see "_wake_source"=="clap"
        # and short-circuit to direct address — no classification needed.
        self._g["_wake_source"] = "clap"
        self._g["_wake_source_ts"] = time.time()
        print(f"[clap_detect] double clap detected (threshold={self._threshold:.3f}) — wake triggered")
        if callable(self._on_clap):
            try:
                self._on_clap()
            except Exception:
                pass
