from __future__ import annotations

import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any


class STTEngine:
    def __init__(self) -> None:
        self._available = False
        self._model = None
        self._backend = "none"
        self._lock = threading.Lock()
        self._vad_model: Any = None
        self._vad_available: bool = False
        # WhisperLiveKit streaming engine (lazy, gated by AVA_STT_STREAMING=1).
        # Singleton — once built, reused for every listen_session_streaming call.
        self._wlk_engine: Any = None
        self._wlk_init_attempted: bool = False
        self._init_model()
        self._init_vad()

    def _init_vad(self) -> None:
        """Load Silero VAD. RTF ≈ 0.004 — uses < 0.5% CPU per check."""
        try:
            from silero_vad import load_silero_vad  # type: ignore
            self._vad_model = load_silero_vad()
            self._vad_available = True
            print("[stt_engine] Silero VAD ready")
        except Exception as e:
            self._vad_available = False
            print(f"[stt_engine] Silero VAD unavailable: {e!r} — falling back to RMS VAD")

    def _init_model(self) -> None:
        # 2026-05-08 STT upgrade: Whisper base → Whisper Large-v3 Turbo.
        # Per the conversation pacing research (Stivers 2009 / industry voice
        # benchmarks), STT latency directly drives perceived response time.
        # large-v3-turbo: ~6× faster than large-v3, similar/better WER, fits
        # in ~1.5 GB VRAM. Falls back through medium → base if VRAM is tight.
        # All via faster-whisper (existing dep — no new install required).
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as e:
            print(f"[stt_engine] faster_whisper import failed: {e!r}")
            self._model = None
            self._available = False
            return

        # Model fallback chain: prefer fastest+most-accurate that fits.
        # turbo is 4-decoder-layer surgery on large-v3 — keeps quality, big speedup.
        # AVA_STT_MODEL env var lets a runtime (e.g. iris_runtime.py) pin a
        # specific model first while keeping the fallback chain intact.
        # distil-large-v3 is ~6× faster than large-v3-turbo at ~1% WER cost —
        # the right pick when first-word latency matters more than tail accuracy.
        import os as _os
        _full_chain = [
            ("large-v3-turbo", "Whisper Large-v3 Turbo"),
            ("distil-large-v3", "Distil-Whisper Large-v3"),
            ("medium", "Whisper Medium"),
            ("base", "Whisper Base (fallback)"),
        ]
        _pin = (_os.environ.get("AVA_STT_MODEL") or "").strip()
        if _pin:
            pinned = [t for t in _full_chain if t[0] == _pin]
            rest = [t for t in _full_chain if t[0] != _pin]
            model_chain = pinned + rest
        else:
            model_chain = _full_chain
        device_chain = [("cuda", "float16"), ("cpu", "int8")]

        for model_id, model_label in model_chain:
            for device, compute in device_chain:
                try:
                    self._model = WhisperModel(model_id, device=device, compute_type=compute)
                    self._available = True
                    self._device = device
                    self._backend = f"faster-whisper:{model_id}:{device}:{compute}"
                    print(
                        f"[stt_engine] {model_label} loaded "
                        f"device={device} compute={compute}"
                    )
                    return
                except Exception as e:
                    print(
                        f"[stt_engine] {model_id} on {device} failed "
                        f"({type(e).__name__}: {str(e)[:120]}) — trying next"
                    )
        self._model = None
        self._available = False

    def is_available(self) -> bool:
        return bool(self._available and self._model is not None)

    def backend_name(self) -> str:
        return self._backend

    # ── recording ──────────────────────────────────────────────

    def _record_sounddevice(self, seconds: float, sample_rate: int):
        import numpy as np  # type: ignore
        import sounddevice as sd  # type: ignore
        frames = int(max(1, round(seconds * sample_rate)))
        audio = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32")
        sd.wait()
        self._backend = "sounddevice"
        return np.squeeze(audio, axis=1)

    def _record_pyaudio(self, seconds: float, sample_rate: int):
        import numpy as np  # type: ignore
        import pyaudio  # type: ignore
        pa = pyaudio.PyAudio()
        chunk = 1024
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=sample_rate, input=True, frames_per_buffer=chunk)
        total_chunks = max(1, int(sample_rate * seconds / chunk))
        chunks: list[bytes] = []
        try:
            for _ in range(total_chunks):
                chunks.append(stream.read(chunk, exception_on_overflow=False))
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
        data = b"".join(chunks)
        pcm = np.frombuffer(data, dtype=np.int16).astype("float32") / 32768.0
        self._backend = "pyaudio"
        return pcm

    def _record_audio(self, seconds: float = 5.0, sample_rate: int = 16000):
        try:
            return self._record_sounddevice(seconds, sample_rate), sample_rate
        except Exception:
            try:
                return self._record_pyaudio(seconds, sample_rate), sample_rate
            except Exception:
                return None, sample_rate

    # ── VAD helper ─────────────────────────────────────────────

    def _has_speech(self, audio, sample_rate: int = 16000, rms_threshold: float = 0.008, min_speech_seconds: float = 0.3) -> bool:
        """Return True if `audio` contains speech.

        Primary: Silero VAD (robust to keyboard/mouse/HVAC noise).
        Fallback: simple RMS-energy threshold.
        """
        # Silero path — preferred when available.
        if self._vad_available and self._vad_model is not None:
            try:
                import numpy as np
                import torch
                from silero_vad import get_speech_timestamps  # type: ignore
                arr = np.asarray(audio, dtype=np.float32).flatten()
                if arr.size < int(sample_rate * 0.1):
                    return False
                tensor = torch.from_numpy(arr)
                ts = get_speech_timestamps(
                    tensor,
                    self._vad_model,
                    sampling_rate=sample_rate,
                    min_speech_duration_ms=int(min_speech_seconds * 1000),
                    min_silence_duration_ms=300,
                    threshold=0.5,
                )
                return len(ts) > 0
            except Exception as e:
                print(f"[stt_engine] Silero VAD error: {e!r} — falling back to RMS")

        # RMS fallback.
        try:
            import numpy as np
            block = int(sample_rate * 0.05)  # 50ms blocks
            speech_blocks = 0
            required_blocks = int(min_speech_seconds / 0.05)
            for start in range(0, len(audio) - block, block):
                chunk = audio[start:start + block]
                rms = float(np.sqrt(np.mean(chunk ** 2)))
                if rms > rms_threshold:
                    speech_blocks += 1
                    if speech_blocks >= required_blocks:
                        return True
            return False
        except Exception:
            return True  # fail open — let transcription decide

    @staticmethod
    def _normalize_transcript(text: str) -> str:
        """Whisper sometimes hears "Eva" / "Aye va" / "A va" instead of "Ava".
        Normalize so wake detection and prompt building see the canonical name."""
        if not text:
            return text
        # Word-boundary substitutions; preserve punctuation context.
        replacements: list[tuple[str, str]] = [
            ("Eva,", "Ava,"),
            ("Eva.", "Ava."),
            ("Eva?", "Ava?"),
            ("Eva!", "Ava!"),
            ("Eva ", "Ava "),
            (" Eva", " Ava"),
            ("Aye va", "Ava"),
            ("Aye-va", "Ava"),
            ("A va", "Ava"),
            ("Hey Ada", "Hey Ava"),
            ("Hi Ada", "Hi Ava"),
        ]
        out = text
        for wrong, right in replacements:
            out = out.replace(wrong, right)
            # Case variants
            out = out.replace(wrong.lower(), right.lower())
        return out

    # ── listen_session (VAD-gated, silence-terminated) ─────────

    def listen_session(
        self,
        max_seconds: float = 12.0,
        silence_seconds: float = 2.5,
        sample_rate: int = 16000,
        rms_threshold: float = 0.008,
        pre_speech_timeout: float | None = None,
        max_speech_seconds: float = 300.0,
    ) -> dict | None:
        """
        Opens microphone, streams 100ms blocks, stops when silence_seconds
        of quiet follows speech. Returns None immediately if no speech detected.

        Turn-taking gates (in priority order):
          1. Pre-speech: if no speech detected after `pre_speech_timeout` seconds
             (defaults to `max_seconds` for back-compat), return with
             speech_detected=False. This is the "did you start talking?" gate.
          2. Post-speech: once speech is detected, the only end-of-turn signal
             is `silence_seconds` of quiet. The user can talk as long as they
             need — natural pauses up to `silence_seconds` are tolerated.
          3. Safety: `max_speech_seconds` is an upper bound to prevent a hung
             stream from camping the mic forever. Default 5 min.

        Returns dict: {text, confidence, duration_seconds, speech_detected}.
        """
        if not self.is_available():
            return None
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            return self._listen_once_compat()

        # Back-compat: callers that pass only max_seconds still get the old
        # behavior of "exit after max_seconds total." If they pass
        # pre_speech_timeout explicitly, we use the new gates.
        pre_timeout = float(pre_speech_timeout) if pre_speech_timeout is not None else float(max_seconds)
        max_speech = float(max_speech_seconds)

        t0 = time.monotonic()
        BLOCK = int(sample_rate * 0.1)  # 100ms
        all_audio: list = []
        speech_started = False
        speech_started_t: float | None = None
        last_speech_t: float | None = None

        with self._lock:
            try:
                stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32", blocksize=BLOCK)
                stream.start()
                self._backend = "sounddevice"
                while True:
                    now = time.monotonic()
                    if not speech_started:
                        # Pre-speech gate — only counts time before any speech
                        # is detected. If you never start talking, exit at
                        # pre_speech_timeout.
                        if (now - t0) >= pre_timeout:
                            break
                    else:
                        # Post-speech safety net only. Real end-of-turn is the
                        # silence_seconds check below.
                        if speech_started_t is not None and (now - speech_started_t) >= max_speech:
                            break
                    block_data, _ = stream.read(BLOCK)
                    chunk = np.squeeze(block_data, axis=1) if block_data.ndim > 1 else block_data
                    all_audio.append(chunk)
                    rms = float(np.sqrt(np.mean(chunk ** 2)))
                    if rms > rms_threshold:
                        if not speech_started:
                            speech_started = True
                            speech_started_t = time.monotonic()
                        last_speech_t = time.monotonic()
                    elif speech_started and last_speech_t is not None:
                        silent_for = time.monotonic() - last_speech_t
                        if silent_for >= silence_seconds:
                            break
                stream.stop()
                stream.close()
            except Exception:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
                return self._listen_once_compat()

        if not all_audio:
            return None
        try:
            audio = np.concatenate(all_audio)
        except Exception:
            return None

        duration = len(audio) / sample_rate
        if not self._has_speech(audio, sample_rate, rms_threshold):
            return {"text": None, "confidence": 0.0, "duration_seconds": duration, "speech_detected": False}

        result = self._transcribe_array(audio, sample_rate)
        result["duration_seconds"] = duration
        result["speech_detected"] = True
        # Expose the raw audio array + sample rate so callers (voice_mood_detector)
        # can reuse it without re-recording. Returned as numpy float32 — callers
        # should not mutate.
        try:
            result["audio_array"] = audio
            result["sample_rate"] = sample_rate
        except Exception:
            pass
        return result

    def _transcribe_array(self, audio, sample_rate: int = 16000) -> dict:
        """Transcribe a numpy float32 array. Returns {text, confidence}."""
        try:
            import numpy as np
            clipped = np.clip(audio, -1.0, 1.0)
            pcm16 = (clipped * 32767.0).astype(np.int16)
        except Exception:
            return {"text": None, "confidence": 0.0}

        wav_path = Path(tempfile.gettempdir()) / "ava_stt_session.wav"
        try:
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm16.tobytes())
        except Exception:
            return {"text": None, "confidence": 0.0}

        try:
            # initial_prompt biases Whisper toward hearing "Ava" as the wake
            # word — without it Whisper often drops the name as filler. Once
            # Silero VAD is the primary speech-presence check (in listen_session)
            # we leave Whisper's internal vad_filter on as a secondary guard.
            # hotwords is a faster-whisper feature; gracefully fall back if
            # the installed version doesn't accept it.
            transcribe_kwargs: dict[str, Any] = {
                "language": "en",
                "vad_filter": True,
                "beam_size": 5,
                "initial_prompt": "Ava, hey Ava,",
                "vad_parameters": {
                    "min_silence_duration_ms": 500,
                    "threshold": 0.008,
                },
            }
            try:
                segments, info = self._model.transcribe(
                    str(wav_path),
                    hotwords="Ava",
                    **transcribe_kwargs,
                )
            except TypeError:
                # Older faster-whisper versions don't accept hotwords.
                segments, info = self._model.transcribe(
                    str(wav_path),
                    **transcribe_kwargs,
                )
            parts = []
            avg_logprob_sum = 0.0
            seg_count = 0
            for seg in segments:
                t = getattr(seg, "text", "").strip()
                if t:
                    parts.append(t)
                    avg_logprob_sum += float(getattr(seg, "avg_logprob", -1.0) or -1.0)
                    seg_count += 1
            text = " ".join(" ".join(p.split()) for p in parts).strip()
            # Normalize "Eva"/"A va"/etc → "Ava" so downstream wake detection
            # and prompt building see the canonical wake word.
            text = self._normalize_transcript(text)
            # Convert avg_logprob (-inf..0) to 0..1 confidence
            if seg_count > 0:
                avg_lp = avg_logprob_sum / seg_count
                confidence = max(0.0, min(1.0, (avg_lp + 1.0)))  # -1.0 logprob → 0.0 conf
            else:
                confidence = 0.0
            return {"text": text or None, "confidence": round(confidence, 3)}
        except Exception:
            return {"text": None, "confidence": 0.0}

    def _listen_once_compat(self) -> dict | None:
        """Fallback: fixed 5s recording via pyaudio or sounddevice."""
        audio, sample_rate = self._record_audio(seconds=5.0, sample_rate=16000)
        if audio is None:
            return None
        try:
            import numpy as np
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            if peak < 0.01:
                return {"text": None, "confidence": 0.0, "speech_detected": False, "duration_seconds": 5.0}
            if not self._has_speech(audio, sample_rate):
                return {"text": None, "confidence": 0.0, "speech_detected": False, "duration_seconds": 5.0}
        except Exception:
            pass
        result = self._transcribe_array(audio, sample_rate)
        result.setdefault("speech_detected", True)
        result.setdefault("duration_seconds", 5.0)
        return result

    def listen_once(self) -> str | None:
        """Legacy single-shot listen. Returns text or None."""
        result = self._listen_once_compat()
        if result is None:
            return None
        return result.get("text")

    def listen_short(self, max_seconds: float = 8.0, silence_seconds: float = 1.0) -> str | None:
        """Tight short-utterance listen for yes/no clarifications. Tighter
        silence cutoff than listen_session — we expect a one-word answer.

        Returns the transcribed text (lowercased, stripped) or None if no
        speech was detected.
        """
        try:
            r = self.listen_session(
                max_seconds=max_seconds,
                silence_seconds=silence_seconds,
                rms_threshold=0.008,
            )
        except Exception as e:
            print(f"[stt_engine] listen_short error: {e!r}")
            return None
        if not isinstance(r, dict):
            return None
        if not r.get("speech_detected"):
            return None
        text = str(r.get("text") or "").strip().lower()
        return text or None

    # ── streaming (Lever 4 — WhisperLiveKit + LocalAgreement-2) ────

    def _ensure_wlk_engine(self) -> Any:
        """Lazy-build the WhisperLiveKit TranscriptionEngine. Singleton — once
        built it can't be reconfigured (WLK enforces this internally). Heavy:
        loads its own copy of distil-large-v3 weights, separate from
        self._model. Returns None on failure (caller should fall back)."""
        if self._wlk_engine is not None:
            return self._wlk_engine
        if self._wlk_init_attempted:
            return None
        self._wlk_init_attempted = True
        try:
            import os as _os
            from whisperlivekit import WhisperLiveKitConfig, TranscriptionEngine
            # WLK validates model_size against its own list — distil-large-v3
            # is rejected by simulstreaming backend. AVA_WLK_MODEL lets us pin
            # a WLK-compatible model independent of the AVA_STT_MODEL used by
            # the single-shot faster-whisper path.
            cfg = WhisperLiveKitConfig(
                model_size=_os.environ.get("AVA_WLK_MODEL", "large-v3-turbo"),
                backend="auto",
                vac=True,
                vad=False,
                pcm_input=True,
                min_chunk_size=0.1,
                model_cache_dir=_os.environ.get("HF_HOME") or None,
                transcription=True,
                diarization=False,
                lan="en",
                target_language="",
            )
            self._wlk_engine = TranscriptionEngine(config=cfg)
            print(f"[stt_engine] WhisperLiveKit engine ready (model={cfg.model_size})")
            return self._wlk_engine
        except Exception as e:
            print(f"[stt_engine] WhisperLiveKit init failed: {e!r}")
            self._wlk_engine = None
            return None

    def listen_session_streaming(
        self,
        max_seconds: float = 60.0,
        silence_seconds: float = 0.8,
        sample_rate: int = 16000,
        rms_threshold: float = 0.008,
    ) -> dict | None:
        """Streaming variant of listen_session using WhisperLiveKit's
        LocalAgreement-2 incremental decoder. Captures live PCM, ships it to
        WLK in 100ms chunks, runs Silero VAD inline to detect EOU. On EOU:
        flushes WLK and returns the final committed transcript.

        Returns dict same shape as listen_session: {text, confidence,
        duration_seconds, speech_detected}, or None on hard failure (caller
        should fall back to listen_session).
        """
        if not self.is_available():
            return None
        engine = self._ensure_wlk_engine()
        if engine is None:
            return None

        import asyncio
        import numpy as np
        import sounddevice as sd
        from whisperlivekit import AudioProcessor

        BLOCK = int(sample_rate * 0.1)  # 100ms
        # Mailbox shared between the audio callback (sounddevice thread) and
        # the asyncio loop thread. callback pushes int16 bytes; loop thread
        # forwards to processor.process_audio().
        pcm_q: "_queue_mod.Queue[bytes]" = _queue_mod_sentinel()
        # Mailbox for accumulated transcription state from create_tasks().
        result_holder: dict = {"lines": [], "buffer": "", "error": None}
        # Mailbox for raw audio so we can run Silero post-hoc as a sanity
        # check on no-speech detections.
        raw_audio_chunks: list = []

        speech_started = False
        last_speech_t: float | None = None
        t0 = time.monotonic()
        stop_requested = threading.Event()

        def _audio_callback(indata, frames, time_info, status):
            nonlocal speech_started, last_speech_t
            # indata for RawInputStream is a cffi buffer of raw bytes.
            raw_bytes = bytes(indata)
            arr = np.frombuffer(raw_bytes, dtype=np.int16)
            raw_audio_chunks.append(arr.copy())
            rms = float(np.sqrt(np.mean((arr.astype(np.float32) / 32768.0) ** 2)))
            now = time.monotonic()
            if rms > rms_threshold:
                speech_started = True
                last_speech_t = now
            elif speech_started and last_speech_t is not None:
                if (now - last_speech_t) >= silence_seconds:
                    stop_requested.set()
            if (now - t0) >= max_seconds:
                stop_requested.set()
            try:
                pcm_q.put_nowait(raw_bytes)
            except Exception:
                pass

        # Run the async pipeline in a worker thread with its own event loop.
        # We bridge the sounddevice callback (sync) into the loop via the
        # pcm_q mailbox.
        def _async_worker() -> None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(_run_pipeline())
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass
            except Exception as e:
                result_holder["error"] = repr(e)

        async def _run_pipeline() -> None:
            processor = AudioProcessor(transcription_engine=engine)
            # create_tasks is `async def` returning an AsyncGenerator — must
            # await the call to get the generator object, THEN async-for it.
            results_gen = await processor.create_tasks()

            async def _consume_results():
                try:
                    async for front in results_gen:
                        # front.lines is list[Segment]; commit accumulator.
                        try:
                            committed = " ".join(
                                (seg.text or "").strip()
                                for seg in (front.lines or [])
                                if getattr(seg, "text", None)
                            ).strip()
                            buffer_text = (front.buffer_transcription or "").strip()
                            result_holder["lines"] = committed
                            result_holder["buffer"] = buffer_text
                        except Exception:
                            continue
                except Exception as e:
                    result_holder["error"] = f"consumer: {e!r}"

            consumer_task = asyncio.create_task(_consume_results())

            async def _feed_pcm():
                while not stop_requested.is_set():
                    try:
                        chunk = pcm_q.get(timeout=0.1)
                    except Exception:
                        await asyncio.sleep(0)
                        continue
                    try:
                        await processor.process_audio(chunk)
                    except Exception as e:
                        result_holder["error"] = f"feed: {e!r}"
                        return
                    await asyncio.sleep(0)

            feeder_task = asyncio.create_task(_feed_pcm())

            # Park until stop_requested fires (Silero EOU or max timeout).
            while not stop_requested.is_set():
                await asyncio.sleep(0.05)

            # Drain remaining PCM in the queue, then send empty to flush.
            try:
                while True:
                    chunk = pcm_q.get_nowait()
                    await processor.process_audio(chunk)
            except Exception:
                pass
            try:
                await processor.process_audio(b"")
            except Exception:
                pass

            # Wait briefly for the consumer to drain final results.
            try:
                await asyncio.wait_for(consumer_task, timeout=3.0)
            except asyncio.TimeoutError:
                consumer_task.cancel()
            except Exception:
                pass
            try:
                feeder_task.cancel()
            except Exception:
                pass
            try:
                await processor.cleanup()
            except Exception:
                pass

        worker = threading.Thread(target=_async_worker, daemon=True, name="stt-streaming")
        worker.start()

        with self._lock:
            try:
                stream = sd.RawInputStream(
                    samplerate=sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=BLOCK,
                    callback=_audio_callback,
                )
                stream.start()
                self._backend = f"whisperlivekit-streaming"
                # Wait until stop_requested fires (callback drives it) OR
                # max_seconds, whichever first.
                deadline = t0 + max_seconds
                while not stop_requested.is_set() and time.monotonic() < deadline:
                    time.sleep(0.05)
                stream.stop()
                stream.close()
            except Exception as e:
                print(f"[stt_engine] streaming capture error: {e!r}")
                stop_requested.set()
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

        # Tell worker to stop and let it drain.
        stop_requested.set()
        worker.join(timeout=5.0)

        # Compose final text: committed lines + any leftover buffer.
        final_text = result_holder.get("lines") or ""
        leftover = result_holder.get("buffer") or ""
        if leftover and leftover not in final_text:
            final_text = (final_text + " " + leftover).strip()
        final_text = self._normalize_transcript(final_text).strip()

        # Compute duration + speech_detected from captured raw audio.
        duration = 0.0
        speech_detected = False
        try:
            if raw_audio_chunks:
                full = np.concatenate(raw_audio_chunks).astype(np.float32) / 32768.0
                duration = float(len(full) / sample_rate)
                speech_detected = self._has_speech(full, sample_rate, rms_threshold)
        except Exception:
            pass

        if not speech_detected and not final_text:
            return {
                "text": None,
                "confidence": 0.0,
                "duration_seconds": duration,
                "speech_detected": False,
            }
        if not final_text:
            # Speech detected but WLK produced nothing — fall back to None
            # so caller can choose to retry with single-shot.
            return None
        if result_holder.get("error"):
            print(f"[stt_engine] streaming had error (returning best-effort): {result_holder['error']}")

        return {
            "text": final_text,
            "confidence": 0.85,  # WLK doesn't expose avg_logprob; placeholder
            "duration_seconds": duration,
            "speech_detected": True,
        }


# ── module-level helper: shared queue used by streaming path ────────────────

def _queue_mod_sentinel():
    """Build a fresh thread-safe queue (Queue is not at module top to avoid
    polluting the existing namespace; this isolates it inside the streaming
    code path)."""
    import queue as _q
    return _q.Queue()
