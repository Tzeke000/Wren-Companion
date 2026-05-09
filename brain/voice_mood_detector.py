"""
Voice mood detector.

Analyses raw audio with librosa to produce a coarse vocal mood signal:
  label    — excited | quiet_or_tired | curious | neutral
  energy   — 0..1 normalised RMS
  speed    — 0..1 normalised tempo
  is_question — pitch rises into the last quarter
  avg_pitch  — mean fundamental frequency

Used as additional context in the prompt (voice tone + question hint) so Ava
can match Zeke's energy. Bootstrap-friendly: no preset emotion meanings, just
coarse signal that the model interprets in context.
"""
from __future__ import annotations

import threading
from typing import Any, Optional


class VoiceMoodDetector:
    def __init__(self) -> None:
        self._available = False
        self._init_error: str = ""
        try:
            import librosa  # type: ignore  # noqa: F401
            self._available = True
        except Exception as e:
            self._init_error = repr(e)
            print(f"[voice_mood] librosa not available: {e!r}")

    @property
    def available(self) -> bool:
        return self._available

    def analyze(self, audio_array: Any, sr: int = 16000) -> dict[str, Any]:
        """Return a coarse mood dict for `audio_array` (1-D float numpy)."""
        if not self._available:
            return self._neutral()
        try:
            import librosa  # type: ignore
            import numpy as np  # type: ignore

            if audio_array is None:
                return self._neutral()
            audio = np.asarray(audio_array, dtype=np.float32).flatten()
            if audio.size < int(sr * 0.4):  # < 400ms — not worth analysing
                return self._neutral()

            # ── pitch (piptrack) ───────────────────────────────────────────────
            pitches, mags = librosa.piptrack(y=audio, sr=sr)
            if mags.size > 0:
                threshold = float(np.max(mags)) * 0.1
                valid = pitches[mags > threshold]
                avg_pitch = float(np.mean(valid)) if valid.size else 150.0
            else:
                avg_pitch = 150.0

            # ── energy (RMS) ───────────────────────────────────────────────────
            rms = librosa.feature.rms(y=audio)[0]
            avg_energy = float(np.mean(rms)) if rms.size else 0.0

            # ── tempo (rough proxy for speaking rate) ──────────────────────────
            try:
                tempo_arr, _ = librosa.beat.beat_track(y=audio, sr=sr)
                tempo = float(tempo_arr) if np.isscalar(tempo_arr) else float(np.mean(tempo_arr))
            except Exception:
                tempo = 120.0

            # ── question detection (pitch rise into last quarter) ──────────────
            is_question = False
            if pitches.size > 0:
                n = pitches.shape[1]
                if n >= 8:
                    quarter = max(1, n // 4)
                    head = pitches[0, :quarter]
                    tail = pitches[0, 3 * quarter:]
                    head_valid = head[head > 0]
                    tail_valid = tail[tail > 0]
                    start_pitch = float(np.mean(head_valid)) if head_valid.size else 150.0
                    end_pitch = float(np.mean(tail_valid)) if tail_valid.size else 150.0
                    is_question = end_pitch > start_pitch * 1.12

            energy_norm = max(0.0, min(1.0, avg_energy * 15.0))
            speed_norm = max(0.0, min(1.0, tempo / 180.0))

            if energy_norm > 0.7:
                label = "excited"
            elif energy_norm < 0.25:
                label = "quiet_or_tired"
            elif is_question:
                label = "curious"
            else:
                label = "neutral"

            return {
                "label": label,
                "energy": round(energy_norm, 3),
                "speed": round(speed_norm, 3),
                "is_question": bool(is_question),
                "avg_pitch": round(avg_pitch, 1),
            }
        except Exception as e:
            print(f"[voice_mood] analyze error: {e!r}")
            return self._neutral()

    @staticmethod
    def _neutral() -> dict[str, Any]:
        return {
            "label": "neutral",
            "energy": 0.5,
            "speed": 0.5,
            "is_question": False,
            "avg_pitch": 150.0,
        }


# ── singleton ─────────────────────────────────────────────────────────────────

_SINGLETON: Optional[VoiceMoodDetector] = None
_LOCK = threading.Lock()


def get_voice_mood_detector() -> VoiceMoodDetector:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = VoiceMoodDetector()
    return _SINGLETON


def bootstrap_voice_mood_detector(g: dict[str, Any]) -> VoiceMoodDetector:
    d = get_voice_mood_detector()
    g["_voice_mood_detector"] = d
    return d
