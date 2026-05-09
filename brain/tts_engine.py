from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    import winsound  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    winsound = None


def _cleanup_spoken_text(text: str) -> str:
    t = str(text or "")
    t = re.sub(r"\[TOOL:[^\]]*\]", " ", t, flags=re.IGNORECASE)
    cleaned_lines: list[str] = []
    for line in t.splitlines():
        low = line.strip().lower()
        if not low:
            continue
        if low.startswith("camera:") or low.startswith("face status:") or low.startswith("recognition:"):
            continue
        if low.startswith("expression:") or low.startswith("vision status:"):
            continue
        cleaned_lines.append(line)
    t = "\n".join(cleaned_lines)
    t = re.sub(r"[#*_`~]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _bridge_script() -> str:
    return r"""
import argparse
import os
import sys


def _safe_fail(msg: str, code: int = 2):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _stub_japanese_deps():
    class _DummyMeCab:
        def __init__(self, *args, **kwargs):
            pass

        def parse(self, text):
            return text

    import types
    sys.modules.setdefault("MeCab", types.SimpleNamespace(Tagger=_DummyMeCab))


def _ensure_nltk_perceptron_tagger():
    # MeloTTS pulls in NLTK and needs averaged_perceptron_tagger_eng for
    # English POS tagging. On a fresh machine the resource isn't bundled
    # and MeloTTS errors with "Resource averaged_perceptron_tagger_eng
    # not found." Download once if missing; idempotent on later calls.
    # Silent on success; logs failure but doesn't raise so the rest of
    # the bridge can still try (possibly producing the original
    # LookupError for the caller to handle).
    try:
        import nltk
        try:
            nltk.data.find('taggers/averaged_perceptron_tagger_eng')
        except LookupError:
            try:
                nltk.download('averaged_perceptron_tagger_eng', quiet=True)
            except Exception as e:
                print(f"[melo_bridge] NLTK download failed: {e!r}")
            try:
                nltk.data.find('taggers/averaged_perceptron_tagger')
            except LookupError:
                try:
                    nltk.download('averaged_perceptron_tagger', quiet=True)
                except Exception:
                    pass
    except Exception as e:
        print(f"[melo_bridge] nltk unavailable: {e!r}")


def _load_melo_tts(language: str = "EN"):
    _stub_japanese_deps()
    _ensure_nltk_perceptron_tagger()
    last_err = None
    for mod_name in ("melo.api", "MeloTTS.melo.api"):
        try:
            mod = __import__(mod_name, fromlist=["TTS"])
            TTS = getattr(mod, "TTS")
            return TTS(language=language)
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        raise last_err
    raise RuntimeError("MeloTTS TTS import failed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["check", "synth"], required=True)
    ap.add_argument("--text", default="")
    ap.add_argument("--wav", default="")
    ap.add_argument("--speaker", default="0")
    args = ap.parse_args()

    try:
        model = _load_melo_tts(language="EN")
        if args.mode == "check":
            print("ok")
            return
        if not args.wav:
            _safe_fail("missing --wav", 3)
        text = (args.text or "").strip()
        if not text:
            _safe_fail("missing text", 4)
        speaker = int(args.speaker or 0)
        model.tts_to_file(text, speaker, args.wav)
        print("ok")
        return
    except Exception as e:
        _safe_fail(f"melo_bridge_error: {e}", 5)


if __name__ == "__main__":
    main()
""".strip()


_VOICE_STYLE_PATH = Path(__file__).resolve().parent.parent / "state" / "voice_style.json"

_DEFAULT_VOICE_STYLE = {
    "rate": 175,
    "volume": 0.9,
    "pause_frequency": 0.5,
    "turns_logged": 0,
    "positive_signal_count": 0,
    "last_updated": None,
}


def _load_voice_style() -> dict[str, Any]:
    if _VOICE_STYLE_PATH.is_file():
        try:
            data = json.loads(_VOICE_STYLE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                style = dict(_DEFAULT_VOICE_STYLE)
                style.update(data)
                return style
        except Exception:
            pass
    return dict(_DEFAULT_VOICE_STYLE)


def _save_voice_style(style: dict[str, Any]) -> None:
    _VOICE_STYLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _VOICE_STYLE_PATH.write_text(json.dumps(style, indent=2, ensure_ascii=False), encoding="utf-8")


def voice_style_adapt(positive_signal: bool, g: dict[str, Any] | None = None) -> None:
    """
    Called after each TTS turn. Slowly adjusts voice style.
    positive_signal: True if conversation continued positively.
    Adjustments are 1-2% per session so change is gradual.
    Stays within rate 140-210, volume 0.7-1.0.
    """
    style = _load_voice_style()
    style["turns_logged"] = int(style.get("turns_logged") or 0) + 1
    if positive_signal:
        style["positive_signal_count"] = int(style.get("positive_signal_count") or 0) + 1

    # Every 10 turns, gently drift rate toward 175 with slight variation
    turns = int(style["turns_logged"])
    if turns > 0 and turns % 10 == 0:
        current_rate = float(style.get("rate") or 175)
        pos_ratio = float(style.get("positive_signal_count") or 0) / max(1, turns)
        # Positive responses → slightly faster and more expressive
        if pos_ratio > 0.7:
            delta = 1.5
        elif pos_ratio < 0.3:
            delta = -1.5
        else:
            delta = 0.0
        new_rate = max(140, min(210, current_rate + delta))
        style["rate"] = round(new_rate, 1)

        new_vol = float(style.get("volume") or 0.9)
        new_vol = max(0.7, min(1.0, new_vol + (0.005 if pos_ratio > 0.6 else -0.005)))
        style["volume"] = round(new_vol, 3)
        style["last_updated"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")

    _save_voice_style(style)


class TTSEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._player_thread: threading.Thread | None = None
        self._last_wav: str | None = None
        self._engine_name = "none"
        self._available = False
        self._speaker_id = 0
        self._bridge_path = self._ensure_bridge_script()
        self._pyttsx3 = None
        self._voice_name = "unknown"
        self._current_amplitude: float = 0.0
        self._voice_style = _load_voice_style()
        self._init_engine()

    def _log(self, message: str) -> None:
        try:
            print(f"[tts] {message}")
        except Exception:
            pass

    def _ensure_bridge_script(self) -> Path:
        state_dir = Path(__file__).resolve().parent.parent / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "melo_tts_bridge.py"
        try:
            path.write_text(_bridge_script(), encoding="utf-8")
        except Exception:
            pass
        return path

    def _run_py311(self, args: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
        cmd = ["py", "-3.11"] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _init_engine(self) -> None:
        # Route through TTSWorker. The worker prefers Kokoro neural TTS and
        # falls back to pyttsx3 (COM-isolated) automatically.
        try:
            from brain.tts_worker import get_tts_worker
            worker = get_tts_worker()
            if worker.is_available():
                self._pyttsx3 = None  # we never call pyttsx3 directly anymore
                self._voice_name = worker.voice_name()
                self._engine_name = worker.engine_name()  # "kokoro" or "pyttsx3"
                self._available = True
                self._log(f"Using {self._engine_name} via TTSWorker (voice={self._voice_name}).")
                return
        except Exception as e:
            self._log(f"TTSWorker init failed: {e!r}")

        melo_err = ""
        try:
            res = self._run_py311([str(self._bridge_path), "--mode", "check"], timeout=30.0)
            if res.returncode == 0:
                self._engine_name = "melotts"
                self._available = True
                self._log("MeloTTS available via Python 3.11 bridge.")
                return
            melo_err = (res.stderr or res.stdout or "").strip() or f"bridge returned code {res.returncode}"
            self._log(f"MeloTTS check failed: {melo_err}")
        except Exception:
            melo_err = "bridge invocation exception"
            self._log("MeloTTS check failed: bridge invocation exception.")
        self._engine_name = "none"
        self._available = False
        if melo_err:
            self._log(f"TTS unavailable. Melo failure: {melo_err}")

    def is_available(self) -> bool:
        return bool(self._available)

    def engine_name(self) -> str:
        return self._engine_name

    def voice_name(self) -> str:
        return self._voice_name

    def _play_wav_blocking(self, wav_path: str) -> None:
        if winsound is None:
            return
        try:
            winsound.PlaySound(wav_path, winsound.SND_FILENAME)
        except Exception:
            pass

    def _play_wav(self, wav_path: str, blocking: bool) -> None:
        if winsound is None:
            return
        # NOTE: do NOT self.stop() here. Stopping playback every time we start
        # a new WAV cuts Ava off mid-sentence whenever a fresh utterance
        # arrives. The TTS worker queue already serialises requests.
        if blocking:
            self._play_wav_blocking(wav_path)
            return
        t = threading.Thread(target=self._play_wav_blocking, args=(wav_path,), daemon=True, name="ava-tts-playback")
        self._player_thread = t
        t.start()

    def _apply_voice_style(self) -> None:
        """Apply current voice_style settings to pyttsx3 engine."""
        if self._pyttsx3 is None:
            return
        # Reload style occasionally for live adaptation
        self._voice_style = _load_voice_style()
        try:
            rate = int(self._voice_style.get("rate") or 175)
            vol = float(self._voice_style.get("volume") or 0.9)
            self._pyttsx3.setProperty("rate", max(140, min(210, rate)))
            self._pyttsx3.setProperty("volume", max(0.7, min(1.0, vol)))
        except Exception:
            pass

    def _speak_pyttsx3(self, text: str, blocking: bool) -> None:
        # Route through TTSWorker. The worker uses Kokoro when available, falls
        # back to pyttsx3 otherwise. Either way, emotion + intensity drive the
        # actual voice characteristics; rate/volume from voice_style are kept
        # as a hint for the pyttsx3 path only.
        try:
            from brain.tts_worker import get_tts_worker
            worker = get_tts_worker()
            if not worker.is_available():
                self._log("tts worker not available")
                return
            try:
                self._voice_style = _load_voice_style()
                rate = int(self._voice_style.get("rate") or 175)
                vol = float(self._voice_style.get("volume") or 0.9)
                worker.apply_style(rate=rate, volume=vol)
            except Exception:
                pass
            # Default to neutral — most callers should hit the operator endpoint
            # or worker.speak_with_emotion directly when they want emotion.
            worker.speak(text, emotion="neutral", intensity=0.5, blocking=blocking)
        except Exception as e:
            self._log(f"_speak_pyttsx3 routing error: {e!r}")

    @staticmethod
    def _estimate_amplitude(text: str) -> float:
        """Estimate speaking energy from text characteristics."""
        n = len(text)
        base = min(0.9, 0.3 + n / 500)
        exclaim = min(0.15, text.count("!") * 0.05)
        question = min(0.10, text.count("?") * 0.04)
        return min(1.0, base + exclaim + question)

    @property
    def speaking(self) -> bool:
        # When using pyttsx3, ask the worker (it knows true speech state).
        if self._engine_name == "pyttsx3":
            try:
                from brain.tts_worker import get_tts_worker
                return get_tts_worker().is_speaking()
            except Exception:
                pass
        t = self._player_thread
        return bool(t is not None and t.is_alive())

    @property
    def amplitude(self) -> float:
        return float(self._current_amplitude) if self.speaking else 0.0

    def speak(self, text: str, blocking: bool = False) -> None:
        if not self._available:
            return
        clean = _cleanup_spoken_text(text)
        if not clean:
            return
        self._current_amplitude = self._estimate_amplitude(clean)
        with self._lock:
            if self._engine_name == "melotts":
                wav_path = Path(tempfile.gettempdir()) / f"ava_tts_{int(time.time()*1000)}.wav"
                try:
                    res = self._run_py311(
                        [
                            str(self._bridge_path),
                            "--mode",
                            "synth",
                            "--text",
                            clean,
                            "--wav",
                            str(wav_path),
                            "--speaker",
                            str(self._speaker_id),
                        ],
                        timeout=45.0,
                    )
                    if res.returncode != 0 or not wav_path.is_file():
                        # Melo failed mid-session; pivot to pyttsx3 when available.
                        err_text = (res.stderr or res.stdout or "").strip()
                        self._log(
                            f"MeloTTS synthesis failed (code={res.returncode}, wav_exists={wav_path.is_file()}): {err_text or 'no error output'}"
                        )
                        # Pivot to TTSWorker-managed pyttsx3 instead of direct init
                        try:
                            from brain.tts_worker import get_tts_worker
                            worker = get_tts_worker()
                            if not worker.is_available():
                                self._available = False
                                self._engine_name = "none"
                                self._log("pyttsx3 fallback unavailable after MeloTTS synthesis error.")
                                return
                            self._engine_name = "pyttsx3"
                            self._available = True
                            self._voice_name = worker.voice_name()
                            self._log("Switched active TTS engine to pyttsx3 (via worker) fallback.")
                        except Exception:
                            self._available = False
                            self._engine_name = "none"
                            self._log("pyttsx3 worker import failed after MeloTTS synthesis error.")
                            return
                        self._speak_pyttsx3(clean, blocking)
                        return
                    self._last_wav = str(wav_path)
                    self._play_wav(str(wav_path), blocking=blocking)
                    return
                except Exception as e:
                    self._log(f"MeloTTS runtime exception: {e}")
                    if self._pyttsx3 is not None:
                        self._engine_name = "pyttsx3"
                        self._available = True
                        self._log("Switched active TTS engine to pyttsx3 fallback after runtime exception.")
                        self._speak_pyttsx3(clean, blocking)
                    return
            if self._engine_name == "pyttsx3":
                self._speak_pyttsx3(clean, blocking)

    def stop(self) -> None:
        try:
            if winsound is not None:
                winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        # Stop pyttsx3 via worker (which owns the engine thread)
        try:
            from brain.tts_worker import get_tts_worker
            get_tts_worker().stop()
        except Exception:
            pass
        try:
            if self._pyttsx3 is not None:
                self._pyttsx3.stop()
        except Exception:
            pass
