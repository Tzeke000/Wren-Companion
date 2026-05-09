"""
Wake word detector — openWakeWord (ONNX) primary, polling fallback.

openWakeWord runs a tiny ONNX model on every 80ms of microphone input.
Total CPU is well under 1% on a desktop. MIT-licensed, no API keys, runs
fully local.

We use `hey_jarvis` as a proxy for "Hey Ava" until a custom `hey_ava`
ONNX model exists at `models/wake_words/hey_ava.onnx`. Phonetics are close
enough that "hey jarvis" / "hey ava" both fire reliably; the proxy stops
once the custom model lands.

Bootstrap: Ava records each activation hour to `state/wake_patterns.json`
so she can later notice when she's typically being talked to.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional


_DEFAULT_THRESHOLD = 0.5     # openWakeWord prediction confidence to trigger
_CHUNK_SAMPLES = 1280        # 80ms at 16kHz — required by openWakeWord
_SAMPLE_RATE = 16000
_TRIGGER_COOLDOWN_SEC = 1.5  # ignore back-to-back hits within this window


class WakeWordDetector:
    def __init__(
        self,
        g: dict[str, Any],
        on_wake: Optional[Callable[[], Any]] = None,
        keywords: tuple[str, ...] = ("hey ava", "ava"),
        base_dir: Optional[Path] = None,
    ):
        self._g = g
        self._on_wake = on_wake
        self._keywords = keywords
        self._base = Path(base_dir) if base_dir else Path(".")
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pattern_path = self._base / "state" / "wake_patterns.json"
        self._patterns: list[dict] = self._load_patterns()

        # Backend bookkeeping
        self._backend: str = "none"
        self._available: bool = False
        self._last_trigger_ts: float = 0.0

        # openWakeWord state
        self._oww_model: Any = None
        self._oww_keys: list[str] = []
        self._oww_threshold = _DEFAULT_THRESHOLD
        self._audio_q: "queue.Queue[Any]" = queue.Queue(maxsize=64)

    # ── persistence ────────────────────────────────────────────────────────────

    def _load_patterns(self) -> list[dict]:
        if not self._pattern_path.is_file():
            return []
        try:
            return json.loads(self._pattern_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _record_activation(self, source: str) -> None:
        hour = int(time.strftime("%H"))
        self._patterns.append({"ts": time.time(), "hour": hour, "source": source})
        if len(self._patterns) > 200:
            self._patterns = self._patterns[-200:]
        try:
            self._pattern_path.parent.mkdir(parents=True, exist_ok=True)
            self._pattern_path.write_text(
                json.dumps(self._patterns, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    # ── trigger ────────────────────────────────────────────────────────────────

    def _trigger_wake(self, source: str = "openwakeword") -> None:
        # Per-instance cooldown so model jitter near threshold doesn't spam.
        now = time.time()
        if (now - self._last_trigger_ts) < _TRIGGER_COOLDOWN_SEC:
            return
        self._last_trigger_ts = now

        self._g["_wake_word_detected"] = True
        self._g["_wake_word_ts"] = now
        # Mark wake source — wake_detector treats this as direct address and
        # skips classification (same path as clap-triggered wakes).
        self._g["_wake_source"] = source
        self._g["_wake_source_ts"] = now
        self._record_activation(source)
        print(f"[wake_word] wake triggered (source={source})")
        if callable(self._on_wake):
            try:
                self._on_wake()
            except Exception as e:
                print(f"[wake_word] on_wake handler error: {e!r}")

    # ── openWakeWord init ─────────────────────────────────────────────────────

    def _try_init_oww(self) -> bool:
        try:
            import openwakeword  # type: ignore
            from openwakeword.model import Model  # type: ignore
        except Exception as e:
            print(f"[wake_word] openWakeWord unavailable: {e!r}")
            return False
        try:
            # Idempotent — only downloads on first run.
            try:
                openwakeword.utils.download_models()
            except Exception as e:
                print(f"[wake_word] model download warning: {e!r}")

            # Priority: custom hey_ava model if present. hey_jarvis was
            # previously loaded as a phonetic proxy for "Hey Ava" but it
            # produced too many false positives in the lunch voice test
            # (2026-04-30) — the user said "hey ava" and openWakeWord
            # consistently caught it as hey_jarvis, which is technically
            # correct (the proxy fired) but the user wants the wake source
            # logged as hey_ava, not jarvis.
            #
            # Disable jarvis entirely. Wake now comes from:
            #   1. Clap detector (always on, separate audio path)
            #   2. Custom hey_ava.onnx — if user has trained one
            #   3. Transcript-wake via Whisper — voice_loop classifies the
            #      transcript text and matches "hey ava", "hi ava",
            #      "hello ava", "yo ava", "ok/okay ava", or bare "ava" at
            #      start of short utterance. See voice_loop._classify_
            #      transcript_wake (~line 94).
            #
            # Override with AVA_USE_HEY_JARVIS_PROXY=1 to re-enable jarvis
            # as a proxy. Default behavior is to disable.
            wake_models: list[str] = []
            custom = self._base / "models" / "wake_words" / "hey_ava.onnx"
            if custom.is_file():
                wake_models.append(str(custom))
                print(f"[wake_word] custom hey_ava model loaded: {custom}")
            allow_jarvis = os.environ.get("AVA_USE_HEY_JARVIS_PROXY", "0").strip() == "1"
            if allow_jarvis and not wake_models:
                wake_models.append("hey_jarvis")
                print("[wake_word] hey_jarvis proxy enabled (AVA_USE_HEY_JARVIS_PROXY=1)")
            elif not wake_models:
                # No custom model + jarvis disabled. Skip openWakeWord
                # entirely so the wake source falls through to clap +
                # transcript_wake (Whisper). The whisper-poll fallback
                # in _whisper_poll_loop() also activates without OWW.
                print("[wake_word] openWakeWord disabled — relying on clap + transcript_wake")
                self._oww_model = None
                return False

            self._oww_model = Model(
                wakeword_models=wake_models,
                inference_framework="onnx",
                enable_speex_noise_suppression=False,
            )
            self._oww_keys = list(self._oww_model.models.keys())
            using_custom = any("hey_ava" in k for k in self._oww_keys)
            using_jarvis = any("jarvis" in k for k in self._oww_keys)
            if using_custom and using_jarvis:
                tag = "custom hey_ava + hey_jarvis proxy"
            elif using_custom:
                tag = "custom hey_ava only"
            elif using_jarvis:
                tag = "hey_jarvis proxy (legacy)"
            else:
                tag = "no wake models loaded"
            print(f"[wake_word] openWakeWord ready models={self._oww_keys} ({tag})")
            return True
        except Exception as e:
            print(f"[wake_word] openWakeWord init failed: {e!r}")
            self._oww_model = None
            return False

    # ── openWakeWord loop ─────────────────────────────────────────────────────

    def _oww_loop(self) -> None:
        try:
            import numpy as np  # noqa: F401
            import sounddevice as sd  # type: ignore
        except Exception as e:
            print(f"[wake_word] sounddevice unavailable: {e!r}")
            self._running = False
            return

        def _audio_callback(indata, frames, _time_info, _status):
            if not self._running:
                return
            try:
                # openWakeWord expects int16 1-D PCM.
                self._audio_q.put_nowait(indata[:, 0].copy())
            except queue.Full:
                # Drop oldest if backlog grows.
                try:
                    self._audio_q.get_nowait()
                    self._audio_q.put_nowait(indata[:, 0].copy())
                except Exception:
                    pass

        try:
            with sd.InputStream(
                samplerate=_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=_CHUNK_SAMPLES,
                callback=_audio_callback,
            ):
                print("[wake_word] openWakeWord listening (always-on, ~1% CPU)")
                while self._running:
                    if self._g.get("input_muted"):
                        # Drain queue & sleep so we don't spin.
                        try:
                            while True:
                                self._audio_q.get_nowait()
                        except queue.Empty:
                            pass
                        time.sleep(0.5)
                        continue
                    try:
                        chunk = self._audio_q.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    try:
                        prediction = self._oww_model.predict(chunk)
                    except Exception as e:
                        print(f"[wake_word] predict error: {e!r}")
                        continue
                    fired = False
                    for name, score in prediction.items():
                        try:
                            s = float(score)
                        except Exception:
                            continue
                        if s >= self._oww_threshold:
                            print(f"[wake_word] {name} score={s:.2f}")
                            self._trigger_wake(source="openwakeword")
                            fired = True
                            break
                    if fired:
                        # Reset model state after a fire to clear scoring history.
                        try:
                            self._oww_model.reset()
                        except Exception:
                            pass
        except Exception as e:
            print(f"[wake_word] oww loop error: {e!r}")

    # ── Whisper-poll fallback (only if openWakeWord can't load) ──────────────

    # Whisper-poll thresholds tightened 2026-04-30 night session.
    # Tonight's hardware test had whisper_poll firing 2-4 times per 13s
    # cycle even with no human speech — Whisper was happily transcribing
    # ambient noise into something matching one of the wake keywords.
    # Three gates added before the transcribe step:
    #   1. RMS energy floor   (cheap, fast reject)
    #   2. Silero VAD         (require >0.6 confidence + >300ms speech)
    #   3. Self-listen guard  (already present from earlier commit)
    # Only audio passing all three reaches Whisper.
    _WHISPER_POLL_RMS_FLOOR = 0.02
    _WHISPER_POLL_VAD_THRESHOLD = 0.6
    _WHISPER_POLL_VAD_MIN_SPEECH_MS = 300

    def _whisper_poll_loop(self) -> None:
        self._backend = "whisper_poll"
        # ── AVA_TEST_MODE bypass (Option D from bugs/synthesized-voice-wake-detection.md) ──
        # When AVA_TEST_MODE=1 is set in the environment, the wake-keyword
        # check is skipped. Any audio that passes RMS + VAD gates triggers
        # wake with the full transcribed text used as the command. This
        # exists for synthesized-voice testing (Piper TTS over CABLE) where
        # the compressed timing of synthetic speech doesn't reliably land
        # the wake keyword inside the 1.5s rolling capture window.
        # Production (real human voice + GAIA HD) keeps the keyword check.
        # Also: in test mode, we widen the recording window (1.5s→3.0s)
        # and shorten the sleep cycle (3.0s→0.5s) so a synthesized
        # utterance is far more likely to be captured cleanly.
        _test_mode = os.environ.get("AVA_TEST_MODE", "0").strip() == "1"
        _record_seconds = 3.0 if _test_mode else 1.5
        _sleep_seconds = 0.5 if _test_mode else 3.0
        if _test_mode:
            print("[wake_word] AVA_TEST_MODE=1 — keyword check bypassed, "
                  f"record={_record_seconds}s sleep={_sleep_seconds}s")
        # Lazy import — load once, reuse forever.
        _vad_model = None
        try:
            from silero_vad import load_silero_vad  # type: ignore
            _vad_model = load_silero_vad()
        except Exception as _ve:
            print(f"[wake_word] silero_vad unavailable for whisper_poll gating: {_ve!r}")

        while self._running:
            try:
                if self._g.get("input_muted"):
                    time.sleep(1.0)
                    continue
                # Self-listen guard — same rationale as the one in
                # voice_loop._should_drop_self_listen(). When Ava is
                # speaking through the speakers, the mic picks up her
                # voice; Whisper transcribes "hey ava is" as "hey ava"
                # and we wake Ava on her own utterance. Skip recording
                # while TTS is mid-stream and for 200ms after.
                if bool(self._g.get("_tts_speaking")):
                    time.sleep(0.5)
                    continue
                last_speak = float(self._g.get("_last_speak_end_ts") or 0.0)
                if last_speak > 0 and (time.time() - last_speak) < 0.2:
                    time.sleep(0.2)
                    continue
                import numpy as np
                import sounddevice as sd  # type: ignore
                audio = sd.rec(int(_record_seconds * 16000), samplerate=16000, channels=1, dtype="float32")
                sd.wait()
                audio = np.squeeze(audio).astype(np.float32, copy=False)

                # ── Gate 1: RMS energy floor ─────────────────────────────
                # If the chunk is essentially silent, skip even Silero VAD
                # — it's a fast reject for ambient quiet.
                rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
                if rms < self._WHISPER_POLL_RMS_FLOOR:
                    continue

                # ── Gate 2: Silero VAD with 0.6 threshold + 300ms gate ───
                # If we have the model loaded, require both confidence and
                # duration to be significant before bothering Whisper.
                if _vad_model is not None:
                    try:
                        import torch  # type: ignore
                        from silero_vad import get_speech_timestamps  # type: ignore
                        tensor = torch.from_numpy(audio)
                        ts = get_speech_timestamps(
                            tensor,
                            _vad_model,
                            sampling_rate=16000,
                            min_speech_duration_ms=self._WHISPER_POLL_VAD_MIN_SPEECH_MS,
                            min_silence_duration_ms=200,
                            threshold=self._WHISPER_POLL_VAD_THRESHOLD,
                        )
                    except Exception as _ie:
                        print(f"[wake_word] silero VAD inference failed: {_ie!r}")
                        ts = None
                    if not ts:
                        # No segment passed the VAD bar — skip Whisper.
                        continue

                # ── Gate 3: Whisper transcription + keyword match ────────
                stt_engine = self._g.get("stt_engine")
                if stt_engine is None or not getattr(stt_engine, "is_available", lambda: False)():
                    time.sleep(3.0)
                    continue
                # Reuse the production STT — no separate tiny model.
                result = stt_engine._transcribe_array(audio, sample_rate=16000)
                text = str((result or {}).get("text") or "").lower().strip()
                # AVA_TEST_MODE: any non-empty transcript counts as wake.
                # Real text content varies by what the harness said, so
                # we still require >2 chars to avoid blank "you" / "."
                # hallucinations.
                _passes_keyword = (
                    (_test_mode and len(text) > 2)
                    or any(kw in text for kw in self._keywords)
                )
                if _passes_keyword:
                    # Use the transcript_wake prefix so voice_loop.py treats
                    # this as an explicit direct-address signal — the next
                    # listening transcript is the command and does NOT need
                    # to repeat "ava". Without this, voice_loop's wake-
                    # classifier rejects the follow-up command with
                    # reason="no_ava_token" (functional regression after
                    # openWakeWord was disabled in 2026-04-29 bench).
                    #
                    # Also stash the FULL transcribed text on globals so
                    # voice_loop can use it as a fallback if its listening
                    # capture comes up empty. This handles the timing race
                    # where the user says "Hey Ava, X" in one breath:
                    # Whisper-poll's 1.5s window catches the whole thing
                    # but listening's NEW recording (started post-wake) only
                    # captures end-of-utterance silence. Without this
                    # fallback, listening's `no speech detected` exit drops
                    # the command and Ava never responds. Vault: 2026-05
                    # work order Phase B Session A retry diagnosis.
                    self._g["_wake_transcript"] = text
                    self._g["_wake_transcript_ts"] = time.time()
                    _wake_source = (
                        "transcript_wake:test_mode"
                        if _test_mode and not any(kw in text for kw in self._keywords)
                        else "transcript_wake:whisper_poll"
                    )
                    self._trigger_wake(source=_wake_source)
            except Exception:
                pass
            time.sleep(_sleep_seconds)

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        use_oww = self._try_init_oww()
        if use_oww:
            self._backend = "openwakeword"
            self._available = True
            target = self._oww_loop
        else:
            self._backend = "whisper_poll"
            self._available = True  # fallback still functional
            target = self._whisper_poll_loop
        self._thread = threading.Thread(target=target, daemon=True, name="ava-wake-word")
        self._thread.start()
        print(f"[wake_word] started backend={self._backend}")

    def stop(self) -> None:
        self._running = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def backend(self) -> str:
        return self._backend

    def get_pattern_summary(self) -> dict[str, Any]:
        if not self._patterns:
            return {"peak_hour": None, "total_activations": 0, "backend": self._backend}
        hours = [int(p.get("hour") or 0) for p in self._patterns if isinstance(p, dict)]
        peak = Counter(hours).most_common(1)[0][0] if hours else None
        return {
            "peak_hour": peak,
            "total_activations": len(self._patterns),
            "backend": self._backend,
        }
