"""
Thread-safe TTS worker — Kokoro neural TTS with pyttsx3 fallback.

Kokoro is a local neural TTS model (82M params) that produces genuinely
human-quality speech. When it's available we use it. When it isn't (or
its dependencies are missing), we fall back to pyttsx3 + Zira on Windows.

Both engines run on a dedicated thread:

    Kokoro      — uses sounddevice for playback (no COM needed). The audio
                  is generated as a torch tensor; we convert to numpy,
                  chunk it, push live RMS amplitude into a module-level
                  variable so the orb can react to actual speech, and play
                  via sd.play()/sd.wait().

    pyttsx3     — uses SAPI5 via COM. COM has a single-threaded apartment
                  model; we call pythoncom.CoInitialize() on the worker
                  thread before pyttsx3.init() so the COM apartment is
                  consistent for the engine's lifetime.

The worker exposes a single-queue API:

    speak(text, emotion="neutral", intensity=0.5, blocking=False)
    speak_with_emotion(text, emotion, intensity, blocking=False)

Emotion + intensity drive voice selection (Kokoro) and rate/volume
modulation (both engines). Bootstrap-friendly: no preferences are baked
in beyond a small label→tone map.

Live amplitude (Kokoro only): while audio plays, the worker pushes the
current RMS amplitude (0..1) into module-level state. Read it via
`get_live_amplitude()` from anywhere — operator_server publishes it into
the snapshot so the orb can react in real time.
"""
from __future__ import annotations

import math
import os
import queue
import threading
import time
from typing import Any, Optional


# ── Emotion → voice / rate / speed mapping ────────────────────────────────────
# Bootstrap-friendly: shared between both engines.

_NEUTRAL_RATE = 155
_NEUTRAL_VOLUME = 0.85

_RATE_BY_EMOTION: dict[str, int] = {
    "calm": 145, "calmness": 145,
    "excitement": 185, "excited": 185,
    "joy": 175, "happy": 175, "happiness": 175,
    "sadness": 130, "sad": 130, "melancholy": 132,
    "curiosity": 165, "interest": 160,
    "boredom": 138, "bored": 138,
    "anxiety": 172, "anxious": 172, "worry": 168,
    "anger": 178, "frustration": 172,
    "fear": 174, "surprise": 180,
    "love": 158, "tenderness": 150,
    "pride": 170, "shame": 138,
    "awe": 162, "hope": 168, "gratitude": 160,
    "neutral": _NEUTRAL_RATE,
}

_VOLUME_BY_EMOTION: dict[str, float] = {
    "calm": 0.80, "calmness": 0.80,
    "excitement": 1.00, "excited": 1.00,
    "joy": 0.95, "happy": 0.95, "happiness": 0.95,
    "sadness": 0.72, "sad": 0.72, "melancholy": 0.74,
    "curiosity": 0.85, "interest": 0.85,
    "boredom": 0.75, "bored": 0.75,
    "anxiety": 0.85, "anxious": 0.85, "worry": 0.82,
    "anger": 0.92, "frustration": 0.88,
    "fear": 0.78, "surprise": 0.92,
    "love": 0.82, "tenderness": 0.78,
    "pride": 0.90, "shame": 0.74,
    "awe": 0.84, "hope": 0.88, "gratitude": 0.82,
    "neutral": _NEUTRAL_VOLUME,
}

# Kokoro speed: 1.0 = normal, range ~0.7..1.3
_KOKORO_BASE_SPEED = 1.18  # Conversational human pace per Zeke 2026-05-08;
                            # was 1.0 (slow). Range 0.7-1.3, natural is 1.15-1.25.
_KOKORO_SPEED_BY_EMOTION: dict[str, float] = {
    "excitement": 1.28, "excited": 1.28,
    "joy": 1.24, "happy": 1.24, "happiness": 1.24, "amusement": 1.22,
    "calm": 1.10, "calmness": 1.10,
    "sadness": 1.05, "sad": 1.05, "melancholy": 1.06,
    "curiosity": 1.22, "interest": 1.20,
    "boredom": 1.08, "bored": 1.08,
    "anxiety": 1.24, "anxious": 1.24,
    "anger": 1.25, "frustration": 1.22, "annoyance": 1.20, "distress": 1.15,
    "fear": 1.22, "surprise": 1.26,
    "love": 1.14, "tenderness": 1.12,
    "pride": 1.18, "shame": 1.08,
    "awe": 1.12, "hope": 1.20, "gratitude": 1.18,
    "neutral": _KOKORO_BASE_SPEED,
}

# Kokoro voice profiles (all 28 work). We pick a small set for emotion mapping
# so Ava's voice has a consistent identity but shifts subtly with mood.
_KOKORO_VOICE_DEFAULT = "af_heart"     # warm, default
_KOKORO_VOICE_EXPRESSIVE = "af_bella"  # more expressive, used for high intensity
_KOKORO_VOICE_SOFT = "af_nicole"        # softer alternative for sadness/shame
_KOKORO_VOICE_BRIGHT = "af_sky"         # brighter for joy/excitement


def _emotion_to_rate_volume(emotion: str, intensity: float) -> tuple[int, float]:
    """Scale (rate, volume) from neutral toward the emotion's target by intensity."""
    e = (emotion or "neutral").lower().strip()
    target_rate = _RATE_BY_EMOTION.get(e, _NEUTRAL_RATE)
    target_vol = _VOLUME_BY_EMOTION.get(e, _NEUTRAL_VOLUME)
    intensity = max(0.0, min(1.0, float(intensity)))
    rate = int(round(_NEUTRAL_RATE + (target_rate - _NEUTRAL_RATE) * intensity))
    volume = round(_NEUTRAL_VOLUME + (target_vol - _NEUTRAL_VOLUME) * intensity, 3)
    rate = max(120, min(220, rate))
    volume = max(0.6, min(1.0, volume))
    return rate, volume


def _emotion_to_kokoro(emotion: str, intensity: float) -> tuple[str, float]:
    """Pick (voice, speed) for Kokoro based on emotion + intensity."""
    e = (emotion or "neutral").lower().strip()
    intensity = max(0.0, min(1.0, float(intensity)))

    bright = {"joy", "happy", "happiness", "excitement", "excited", "love", "amusement", "enthusiasm", "pride", "hope"}
    soft = {"sadness", "sad", "melancholy", "shame", "guilt", "loneliness"}
    expressive = {"excitement", "anger", "surprise", "awe", "love", "joy"}

    if e in expressive and intensity > 0.55:
        voice = _KOKORO_VOICE_EXPRESSIVE
    elif e in soft and intensity > 0.4:
        voice = _KOKORO_VOICE_SOFT
    elif e in bright and intensity > 0.5:
        voice = _KOKORO_VOICE_BRIGHT
    else:
        voice = _KOKORO_VOICE_DEFAULT

    target_speed = _KOKORO_SPEED_BY_EMOTION.get(e, 1.0)
    # Scale toward 1.0 by inverse intensity so subtle moods produce subtle changes.
    speed = round(1.0 + (target_speed - 1.0) * intensity, 3)
    speed = max(0.7, min(1.3, speed))
    return voice, speed


# ── Live amplitude (module-level, read by operator_server) ───────────────────
_LIVE_AMPLITUDE: float = 0.0
_AMP_LOCK = threading.Lock()

# ── Live speech progress (module-level, read by operator_server) ─────────────
# Approximated word-level streaming: Kokoro generates the whole phrase at once,
# so we estimate per-word cadence by walking the audio buffer in lockstep with
# playback. The orb-text UI reads these to render Ava's speech word by word.
_SPEECH_LOCK = threading.Lock()
_SPEECH_FULL_REPLY: str = ""
_SPEECH_SPOKEN_SO_FAR: str = ""
_SPEECH_CURRENT_WORD: str = ""


def _speech_set(full: str = "", spoken: str = "", current: str = "") -> None:
    global _SPEECH_FULL_REPLY, _SPEECH_SPOKEN_SO_FAR, _SPEECH_CURRENT_WORD
    with _SPEECH_LOCK:
        _SPEECH_FULL_REPLY = full
        _SPEECH_SPOKEN_SO_FAR = spoken
        _SPEECH_CURRENT_WORD = current


def _speech_clear() -> None:
    _speech_set("", "", "")


def get_speech_state() -> tuple[str, str, str]:
    """Return (full_reply, spoken_so_far, current_word). Empty strings when idle."""
    with _SPEECH_LOCK:
        return (_SPEECH_FULL_REPLY, _SPEECH_SPOKEN_SO_FAR, _SPEECH_CURRENT_WORD)


def _trace(label: str) -> None:  # TRACE-PHASE1
    """Timestamped diagnostic trace for the TTS path. Removed/gated in Phase 3."""  # TRACE-PHASE1
    ts = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"  # TRACE-PHASE1
    print(f"[trace] {ts} {label}")  # TRACE-PHASE1


def get_live_amplitude() -> float:
    """Current speech amplitude (0..1). 0 when not speaking."""
    with _AMP_LOCK:
        return float(_LIVE_AMPLITUDE)


def _set_live_amplitude(v: float) -> None:
    global _LIVE_AMPLITUDE
    with _AMP_LOCK:
        _LIVE_AMPLITUDE = max(0.0, min(1.0, float(v)))


# ── Queue item: (text, emotion, intensity, optional done-event) ──────────────
_QueueItem = Optional[tuple[str, str, float, Optional[threading.Event]]]


class TTSWorker:
    """Single-threaded TTS owner. Prefers Kokoro, falls back to pyttsx3."""

    def __init__(self, g: Optional[dict[str, Any]] = None) -> None:
        self._queue: "queue.Queue[_QueueItem]" = queue.Queue()
        self._stop_evt = threading.Event()
        self._g = g  # globals — used to publish _tts_speaking / _tts_amplitude

        # Engine state
        self._engine_type: str = "none"   # "kokoro" | "pyttsx3" | "none"
        self._available: bool = False
        self._init_error: str = ""
        self._currently_speaking = threading.Event()

        # Kokoro-specific
        self._kokoro_pipeline: Any = None
        self._sd: Any = None
        self._np: Any = None

        # pyttsx3-specific
        self._pyttsx3: Any = None
        self._voice_name: str = "unknown"

        self._init_done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ava-tts-worker")
        self._thread.start()
        # Kokoro can take ~5-8s to load; allow generous init window.
        self._init_done.wait(timeout=20.0)

    def attach_globals(self, g: dict[str, Any]) -> None:
        """Late-bind globals if the worker was constructed before startup
        finished setting them up. Safe to call multiple times."""
        self._g = g

    # ── globals helpers (publish UI-facing state) ──────────────────────────────

    def _set_speaking_state(self, speaking: bool, amp: float = 0.0) -> None:
        if self._g is None:
            return
        try:
            self._g["_tts_speaking"] = bool(speaking)
            self._g["_tts_amplitude"] = max(0.0, min(1.0, float(amp)))
        except Exception:
            pass

    def _set_speaking_amplitude(self, amp: float) -> None:
        if self._g is None:
            return
        try:
            self._g["_tts_amplitude"] = max(0.0, min(1.0, float(amp)))
        except Exception:
            pass

    def _muted(self) -> bool:
        if self._g is None:
            return False
        try:
            return bool(self._g.get("_tts_muted"))
        except Exception:
            return False

    @staticmethod
    def _raise_thread_priority() -> None:
        """Bump this thread to THREAD_PRIORITY_HIGHEST so audio playback
        never gets starved by other foreground work."""
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            THREAD_PRIORITY_HIGHEST = 2
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)
        except Exception:
            pass

    # ── thread body ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        # Bump priority once for the lifetime of the worker thread. Audio
        # playback should never get starved by other foreground work.
        self._raise_thread_priority()
        try:
            # Engine selection per AVA_TTS_ENGINE env var (default piper).
            # 2026-05-08: Piper made primary because Kokoro on CPU torch
            # takes ~30s per first chunk synth, killing TTFA. Piper is
            # ~1-2s/sentence on CPU — ugly mid-quality, conversational
            # responsiveness. Once CUDA torch + Kokoro work cleanly we
            # can flip back via AVA_TTS_ENGINE=kokoro.
            engine_pref = (os.environ.get("AVA_TTS_ENGINE") or "piper").strip().lower()
            inited = False
            if engine_pref == "kokoro":
                inited = self._try_init_kokoro() or self._try_init_piper()
            elif engine_pref == "piper":
                inited = self._try_init_piper() or self._try_init_kokoro()
            elif engine_pref == "pyttsx3":
                inited = self._try_init_pyttsx3()
            else:
                inited = self._try_init_piper() or self._try_init_kokoro()
            if not inited:
                self._try_init_pyttsx3()

            self._init_done.set()
            if not self._available:
                return

            while not self._stop_evt.is_set():
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                text, emotion, intensity, done = item
                if not text:
                    if done:
                        done.set()
                    continue
                try:
                    self._currently_speaking.set()
                    self._set_speaking_state(True, 0.5)
                    # Any playback Ava starts is conversational (the only
                    # callers are run_ava replies, voice_commands, the
                    # question engine, proactive triggers, face greetings —
                    # all directed at the user). Mark conversation active
                    # so background subsystems defer.
                    if self._g is not None:
                        try:
                            self._g["_conversation_active"] = True
                        except Exception:
                            pass
                    if self._engine_type == "kokoro":
                        self._speak_kokoro(text, emotion, intensity)
                    elif self._engine_type == "piper":
                        self._speak_piper(text, emotion, intensity)
                    else:
                        self._speak_pyttsx3(text, emotion, intensity)
                except Exception as e:
                    print(f"[tts_worker] speak error ({self._engine_type}): {e!r}")
                finally:
                    _set_live_amplitude(0.0)
                    self._set_speaking_state(False, 0.0)
                    # Stamp the global last-speak-end so voice_loop drops into
                    # attentive after question_engine / proactive / greeting
                    # speech, not just after run_ava replies.
                    if self._g is not None:
                        try:
                            self._g["_last_speak_end_ts"] = time.time()
                        except Exception:
                            pass
                    self._currently_speaking.clear()
                    if done:
                        done.set()
        finally:
            # Ensure amplitude resets if the loop dies
            _set_live_amplitude(0.0)
            self._set_speaking_state(False, 0.0)

    # ── engine init ────────────────────────────────────────────────────────────

    def _try_init_kokoro(self) -> bool:
        try:
            from kokoro import KPipeline  # type: ignore
            import sounddevice as sd      # type: ignore
            import numpy as np            # type: ignore
            print("[tts_worker] loading Kokoro pipeline (this takes ~5s on first run)...")
            # Force CUDA if available — Kokoro defaults to CPU which makes
            # first-chunk synth ~25-50s on this hardware. CUDA brings it
            # under 5s. Per Zeke 2026-05-08: "the problem is how long it
            # takes her to finally say her first words."
            _kokoro_device = "cpu"
            try:
                import torch  # type: ignore
                if torch.cuda.is_available():
                    _kokoro_device = "cuda"
            except Exception:
                pass
            print(f"[tts_worker] Kokoro device={_kokoro_device}")
            pipeline = KPipeline(
                lang_code="a", repo_id="hexgrad/Kokoro-82M",
                device=_kokoro_device,
            )
            self._kokoro_pipeline = pipeline
            # Warmup synth: run dummy generations covering the typical
            # range of reply lengths. Kokoro on CUDA does cudnn algorithm
            # tuning per input shape (~30s the first time it sees a new
            # length), so warming with one short phrase only helps for that
            # shape. Diverse warmup → most real replies hit pre-tuned
            # algorithms and synth in <2s. Per Zeke 2026-05-08 TTFA
            # diagnosis: warm benchmark showed 0.5-2s per chunk after
            # multi-shape warmup, vs 27-40s on first call.
            try:
                import time as _wt
                _warmup_texts = [
                    "Hi.",
                    "How are you doing?",
                    "I am here and I am listening to you, take your time.",
                    "Let me think about that for a moment before I respond.",
                ]
                _t0 = _wt.time()
                for _wtext in _warmup_texts:
                    for _ in pipeline(_wtext, voice=_KOKORO_VOICE_DEFAULT, speed=_KOKORO_BASE_SPEED):
                        pass
                print(f"[tts_worker] Kokoro multi-shape warmup done in {_wt.time()-_t0:.2f}s ({len(_warmup_texts)} shapes)")
            except Exception as _we:
                print(f"[tts_worker] Kokoro warmup failed (non-fatal): {_we!r}")
            self._sd = sd
            self._np = np
            self._engine_type = "kokoro"
            self._available = True
            self._voice_name = _KOKORO_VOICE_DEFAULT
            # Publish health flag for /api/v1/debug/full subsystem_health.kokoro_loaded.
            # Without this, the snapshot reports False even after Kokoro fully loaded
            # because nothing else writes _kokoro_ready on g.
            if self._g is not None:
                try:
                    self._g["_kokoro_ready"] = True
                except Exception:
                    pass
            print(f"[tts_worker] Kokoro ready (default voice={_KOKORO_VOICE_DEFAULT})")
            return True
        except Exception as e:
            self._init_error = f"kokoro: {e!r}"
            print(f"[tts_worker] Kokoro init failed: {e!r}")
            return False

    def _try_init_piper(self) -> bool:
        """Initialize Piper TTS as Ava's voice. Faster than Kokoro on CPU
        (~1-2s/sentence vs ~30s for Kokoro first chunk). Voice locked to
        en_US-lessac-high — different from Wren (en_US-amy-medium) so the
        listener can tell them apart in voice. Per Zeke 2026-05-08."""
        try:
            from piper import PiperVoice  # type: ignore
            import sounddevice as sd      # type: ignore
            import numpy as np            # type: ignore
            import time as _t
            from pathlib import Path as _P
            print("[tts_worker] loading Piper TTS (Ava voice = lessac-high)...")
            # Voice models are at <repo>/models/piper/. Lessac is reserved
            # for Ava; amy is reserved for Wren. Don't swap without checking.
            _models_dir = _P("models/piper")
            _ava_voice_path = _models_dir / "en_US-lessac-high.onnx"
            if not _ava_voice_path.is_file():
                # Fall back to whatever .onnx is there if lessac missing.
                _candidates = list(_models_dir.glob("*.onnx")) if _models_dir.exists() else []
                if not _candidates:
                    print("[tts_worker] Piper init failed: no voice model in models/piper/")
                    return False
                _ava_voice_path = _candidates[0]
                print(f"[tts_worker] Piper using fallback voice: {_ava_voice_path.name}")
            _t0 = _t.time()
            self._piper_voice = PiperVoice.load(str(_ava_voice_path))
            self._piper_voice_name = _ava_voice_path.stem
            self._piper_sample_rate = int(getattr(self._piper_voice.config, "sample_rate", 22050))
            self._sd = sd
            self._np = np
            self._engine_type = "piper"
            self._available = True
            self._voice_name = self._piper_voice_name
            print(f"[tts_worker] Piper ready (voice={self._piper_voice_name} "
                  f"sr={self._piper_sample_rate}Hz init_ms={int((_t.time()-_t0)*1000)})")
            # Publish health flag for snapshot consumers.
            if self._g is not None:
                try:
                    self._g["_piper_ready"] = True
                except Exception:
                    pass
            return True
        except Exception as e:
            self._init_error = f"piper: {e!r}"
            print(f"[tts_worker] Piper init failed: {e!r}")
            return False

    def _speak_piper(self, text: str, emotion: str, intensity: float) -> None:
        """Synthesize via Piper and stream chunks straight to playback."""
        _synth_t0 = time.time()
        _trace(f"tts.synth_start chars={len(text)}")
        sample_rate = self._piper_sample_rate
        _speech_set(full=text, spoken="", current="")
        _first_chunk_played = False
        _total_samples = 0
        _chunks_played = 0
        _playback_t0 = None
        try:
            for chunk in self._piper_voice.synthesize(text):
                # Piper yields AudioChunk per sentence. audio_int16_bytes is
                # the canonical PCM payload; convert to float32 [-1, 1] for
                # _play_with_amplitude (matches Kokoro path).
                pcm_bytes = getattr(chunk, "audio_int16_bytes", None)
                if pcm_bytes is None or len(pcm_bytes) == 0:
                    continue
                audio_int16 = self._np.frombuffer(pcm_bytes, dtype=self._np.int16)
                audio_np = (audio_int16.astype(self._np.float32) / 32768.0)
                if audio_np.size == 0:
                    continue
                _chunks_played += 1
                _total_samples += int(audio_np.shape[0])
                if not _first_chunk_played:
                    _first_chunk_played = True
                    _trace(f"tts.first_chunk_ready ms={int((time.time()-_synth_t0)*1000)} samples={int(audio_np.shape[0])}")
                    _playback_t0 = time.time()
                self._play_with_amplitude(audio_np, sample_rate, words=text.split())
        except Exception as _pe:
            print(f"[tts_worker] Piper synth/playback error: {_pe!r}")

        if _chunks_played == 0:
            print("[tts_worker] Piper produced no audio")
            return

        _trace(f"tts.synth_done ms={int((time.time()-_synth_t0)*1000)} samples={_total_samples}")
        if _playback_t0 is not None:
            _trace(f"tts.playback_done ms={int((time.time()-_playback_t0)*1000)}")
        _speech_set(full=text, spoken=text, current="")
        preview = text[:60].replace("\n", " ")
        print(f"[tts_worker] piper spoke voice={self._piper_voice_name} chars={len(text)} chunks={_chunks_played}: {preview!r}")

    def _try_init_pyttsx3(self) -> bool:
        com_initialized = False
        try:
            try:
                import pythoncom  # type: ignore
                pythoncom.CoInitialize()
                com_initialized = True
            except Exception as e:
                print(f"[tts_worker] CoInitialize failed (non-fatal): {e!r}")

            import pyttsx3  # type: ignore
            engine = pyttsx3.init()
            selected_name = "default"
            voices = engine.getProperty("voices") or []
            for v in voices:
                vname = str(getattr(v, "name", "") or "")
                if any(x in vname.lower() for x in ("zira", "hazel", "female")):
                    engine.setProperty("voice", str(getattr(v, "id", "") or ""))
                    selected_name = vname
                    break
            engine.setProperty("rate", _NEUTRAL_RATE)
            engine.setProperty("volume", _NEUTRAL_VOLUME)
            self._pyttsx3 = engine
            self._voice_name = selected_name
            self._engine_type = "pyttsx3"
            self._available = True
            print(f"[tts_worker] pyttsx3 ready (voice={selected_name}) — Kokoro unavailable")
            return True
        except Exception as e:
            if not self._init_error:
                self._init_error = f"pyttsx3: {e!r}"
            else:
                self._init_error += f" / pyttsx3: {e!r}"
            self._available = False
            self._engine_type = "none"
            print(f"[tts_worker] pyttsx3 init failed: {e!r}")
            if com_initialized:
                try:
                    import pythoncom  # type: ignore
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
            return False

    # ── speak implementations ──────────────────────────────────────────────────

    def _speak_kokoro(self, text: str, emotion: str, intensity: float) -> None:
        """Generate audio via Kokoro and stream chunks to playback as they arrive.

        Streaming refactor (2026-05-08): instead of collecting ALL Kokoro
        chunks then concatenating then playing, play each chunk immediately
        as the generator yields it. On CPU torch this drops TTFA from
        ~all-chunks-synth-time (~120s for a 4-chunk reply) to single-chunk-
        synth-time (~30s), so Ava starts speaking far sooner. Total wall-
        clock is the same; the listener-perceived latency is what changes.
        """
        _synth_t0 = time.time()  # TRACE-PHASE1
        _trace(f"tts.synth_start chars={len(text)}")  # TRACE-PHASE1
        voice, speed = _emotion_to_kokoro(emotion, intensity)
        try:
            # Force split on punctuation in addition to newlines so long
            # replies become many small chunks. Reasons: (1) Kokoro on CUDA
            # does cudnn algorithm tuning per input shape — a 228-char
            # one-chunk synth pays ~42s tuning cost the first time. Smaller
            # chunks fit pre-tuned shapes from the multi-shape warmup at
            # startup. (2) Smaller chunks = first-audio sooner via the
            # streaming playback path. Per Zeke 2026-05-08 latency work.
            _split = r'(?<=[.!?])\s+|\n+'
            generator = self._kokoro_pipeline(text, voice=voice, speed=speed, split_pattern=_split)
        except Exception as e:
            print(f"[tts_worker] Kokoro generator failed for voice={voice}: {e!r}")
            # Try default voice once
            generator = self._kokoro_pipeline(text, voice=_KOKORO_VOICE_DEFAULT, speed=speed, split_pattern=_split)
            voice = _KOKORO_VOICE_DEFAULT

        sample_rate = 24000
        words = text.split()
        _speech_set(full=text, spoken="", current="")
        _first_chunk_played = False
        _total_samples = 0
        _chunks_played = 0
        _playback_t0 = None
        _spoken_so_far = ""

        try:
            for _gs, _ps, audio in generator:
                try:
                    audio_np = audio.detach().cpu().numpy() if hasattr(audio, "detach") else self._np.asarray(audio)
                except Exception:
                    audio_np = self._np.asarray(audio)
                if audio_np is None or audio_np.size == 0:
                    continue
                audio_np = audio_np.astype(self._np.float32, copy=False)
                _chunks_played += 1
                _total_samples += int(audio_np.shape[0])
                if not _first_chunk_played:
                    _first_chunk_played = True
                    _trace(f"tts.first_chunk_ready ms={int((time.time()-_synth_t0)*1000)} samples={int(audio_np.shape[0])}")
                    _playback_t0 = time.time()
                # Estimate which words this chunk represents — proportional to
                # samples vs total expected length. For chunked playback we use
                # the chunk text fragment if available (`_gs` is the grapheme
                # source for this chunk).
                _chunk_text = (_gs or "").strip()
                _chunk_words = _chunk_text.split() if _chunk_text else []
                self._play_with_amplitude(audio_np, sample_rate, words=_chunk_words)
                if _chunk_text:
                    _spoken_so_far = (_spoken_so_far + " " + _chunk_text).strip()
                    _speech_set(full=text, spoken=_spoken_so_far, current="")
        except Exception as _ge:
            print(f"[tts_worker] Kokoro stream error mid-playback: {_ge!r}")

        if _chunks_played == 0:
            print("[tts_worker] Kokoro produced no audio")
            return

        _trace(f"tts.synth_done ms={int((time.time()-_synth_t0)*1000)} samples={_total_samples}")  # TRACE-PHASE1
        if _playback_t0 is not None:
            _trace(f"tts.playback_done ms={int((time.time()-_playback_t0)*1000)}")  # TRACE-PHASE1
        # Final state: full reply marked as spoken, no current word.
        _speech_set(full=text, spoken=text, current="")
        preview = text[:60].replace("\n", " ")
        print(f"[tts_worker] kokoro spoke voice={voice} speed={speed:.2f} chars={len(text)} chunks={_chunks_played}: {preview!r}")

    def _resolve_tts_devices(self, sample_rate: int) -> list[dict[str, Any]]:
        """Return one descriptor per output device TTS should play to.

        Configuration via env var AVA_TTS_DEVICES (comma-separated):
          - "speakers,cable" (default) — laptop speakers + VB-CABLE Input
          - "speakers"                 — laptop speakers only
          - "cable"                    — VB-CABLE only (silent locally)
          - "auto"                     — Windows system default only
          - explicit name substrings   — match against sd device names

        Bug 0.1 fix (2026-05-02): default Windows output device on this
        machine is CABLE Input, so the user couldn't hear Ava — TTS played
        only to the virtual cable that Claude was capturing. Speakers got
        nothing. Default is now BOTH so the user hears her AND the cable
        keeps working.
        """
        sd = self._sd
        spec = (os.environ.get("AVA_TTS_DEVICES") or "speakers,cable").strip().lower()
        if spec == "auto":
            return [{"device": None, "label": "system_default"}]

        targets = [t.strip() for t in spec.split(",") if t.strip()]
        # Map shortcut keywords to substring patterns. Order matters — e.g. we
        # want a real "Speakers (Realtek)" before "CABLE In" even if both exist.
        keyword_patterns = {
            "speakers": ("speakers (realtek", "speakers (", "realtek", "speakers"),
            "cable": ("cable input (vb-audio", "cable input"),
            "headphones": ("headphones",),
        }

        devices: list[dict[str, Any]] = []
        seen_idx: set[int] = set()
        try:
            all_devs = list(sd.query_devices())
        except Exception as e:
            print(f"[tts_worker] query_devices failed: {e!r}")
            return [{"device": None, "label": "system_default"}]

        for target in targets:
            patterns = keyword_patterns.get(target, (target,))
            picked: int | None = None
            for pat in patterns:
                for i, d in enumerate(all_devs):
                    if i in seen_idx:
                        continue
                    if int(d.get("max_output_channels") or 0) <= 0:
                        continue
                    name_lower = str(d.get("name") or "").lower()
                    if pat in name_lower:
                        # Avoid the cable when looking for speakers (some Realtek
                        # rows precede CABLE rows but the keyword set already
                        # protects this — this is belt-and-suspenders).
                        if target == "speakers" and "cable" in name_lower:
                            continue
                        picked = i
                        break
                if picked is not None:
                    break
            if picked is not None:
                seen_idx.add(picked)
                devices.append({
                    "device": picked,
                    "label": f"{target}={all_devs[picked].get('name', picked)!r}",
                })
            else:
                print(f"[tts_worker] no device matched '{target}' — skipping")

        if not devices:
            print("[tts_worker] no TTS devices resolved — falling back to system default")
            return [{"device": None, "label": "system_default"}]
        return devices


    def _play_with_amplitude(self, audio_np: Any, sample_rate: int, words: Optional[list[str]] = None) -> None:
        """Play audio via protected sd.OutputStream(s) that cannot be
        interrupted by window focus changes or other UI events.

        Uses stream.write() with a chunked feed so amplitude updates land in
        real time. By default plays to BOTH the laptop speakers and VB-CABLE
        simultaneously (Bug 0.1 fix, 2026-05-02) — see _resolve_tts_devices
        for the AVA_TTS_DEVICES env var.

        The ONLY conditions that abort playback mid-stream:
          - the user has explicitly muted (g["_tts_muted"] = True)
          - the worker is shutting down (self._stop_evt set)
        Window focus changes, mouse clicks, other apps grabbing the mic — all
        are ignored. Audio plays through to completion.

        Per-stream failures are tolerated: if cable opens but speakers don't
        (or vice versa), the working stream still plays. Only if ALL streams
        fail do we fall back to sd.play().

        If `words` is provided, advances current_word / spoken_so_far in
        lockstep with playback (Option 1 estimated cadence: words evenly
        distributed across audio duration).
        """
        np = self._np
        sd = self._sd

        # Force float32 for the stream.
        if audio_np.dtype != np.float32:
            audio_np = audio_np.astype(np.float32)

        chunk_size = 2048  # ~85ms @ 24kHz — small enough for snappy amplitude
        n_samples = int(audio_np.shape[0])
        full_text = " ".join(words) if words else ""
        n_words = len(words) if words else 0
        last_word_idx = -1

        # Stamp playback-dropped flag — cleared at start of each play, set
        # if we abort early. Read by /api/v1/debug/full and the snapshot so
        # external observers (tests, the UI) can detect dropped TTS without
        # parsing trace lines. The lunch voice test (2026-04-30) had a
        # second-turn TTS go silent and the only evidence was the absence
        # of a tts.playback_done trace line — this gives us a positive
        # signal instead.
        if self._g is not None:
            try:
                self._g["_tts_last_playback_dropped"] = False
            except Exception:
                pass
        played_full = False
        idx = 0  # initialized here so the finally's drop-stamp can read it

        # Open one stream per configured destination. Per-stream failures are
        # tolerated — we only fall back to sd.play() if EVERY stream fails.
        device_descs = self._resolve_tts_devices(sample_rate)
        streams: list[Any] = []
        opened_labels: list[str] = []
        for desc in device_descs:
            try:
                kwargs = {
                    "samplerate": sample_rate,
                    "channels": 1,
                    "dtype": "float32",
                    "blocksize": chunk_size,
                    "latency": "low",
                }
                if desc["device"] is not None:
                    kwargs["device"] = desc["device"]
                s = sd.OutputStream(**kwargs)
                s.start()
                streams.append(s)
                opened_labels.append(desc["label"])
            except Exception as e:
                print(f"[tts_worker] OutputStream open {desc['label']} failed: {e!r}")
        if streams:
            print(f"[tts_worker] playing to: {', '.join(opened_labels)}")

        try:
            if not streams:
                raise RuntimeError("no TTS output streams opened")
            try:
                idx = 0
                while idx < n_samples:
                    # Explicit mute is the only mid-stream abort.
                    if self._muted() or self._stop_evt.is_set():
                        break
                    end = min(idx + chunk_size, n_samples)
                    chunk = audio_np[idx:end]
                    if len(chunk) < chunk_size:
                        chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
                    # Real RMS amplitude for the orb pulse.
                    rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                    amp = min(1.0, rms * 8.0)  # gain so the orb pulses meaningfully
                    _set_live_amplitude(amp)
                    self._set_speaking_amplitude(amp)

                    # Advance the word pointer in lockstep with playback.
                    if n_words > 0:
                        progress = (idx + chunk_size) / max(1, n_samples)
                        progress = max(0.0, min(1.0, progress))
                        # Floor so we only "say" a word once we've passed its
                        # start boundary; round up at the end so the final word
                        # latches.
                        word_idx = int(progress * n_words)
                        if word_idx > last_word_idx:
                            last_word_idx = word_idx
                            spoken_words = words[:max(0, word_idx)]
                            current = words[word_idx - 1] if word_idx > 0 else ""
                            _speech_set(
                                full=full_text,
                                spoken=" ".join(spoken_words),
                                current=current,
                            )

                    chunk_2d = chunk.reshape(-1, 1)
                    # Write to every open stream. Drop a stream on error so
                    # one bad device doesn't tank the whole playback. The
                    # remaining streams keep playing audio that is still
                    # synchronized chunk-by-chunk.
                    failed: list[Any] = []
                    for s in streams:
                        try:
                            s.write(chunk_2d)
                        except Exception as e:
                            print(f"[tts_worker] stream.write failed: {e!r} — dropping stream")
                            failed.append(s)
                    for s in failed:
                        try:
                            s.close()
                        except Exception:
                            pass
                        streams.remove(s)
                    if not streams:
                        raise RuntimeError("all TTS output streams dropped mid-playback")
                    idx += chunk_size
                # Set played_full only when the full sample buffer was fed
                # to all surviving streams.
                if idx >= n_samples:
                    played_full = True
            finally:
                for s in streams:
                    try:
                        s.stop()
                        s.close()
                    except Exception:
                        pass
                streams = []
        except Exception as e:
            print(f"[tts_worker] OutputStream failed ({e!r}) — using sd.play fallback")
            try:
                if not self._muted():
                    sd.play(audio_np, samplerate=sample_rate)
                    sd.wait()
                    played_full = True
            except Exception as e2:
                print(f"[tts_worker] sd.play fallback failed: {e2!r}")
        finally:
            _set_live_amplitude(0.0)
            self._set_speaking_amplitude(0.0)
            # Stamp the dropped-playback flag for external observers.
            if self._g is not None and not played_full:
                try:
                    self._g["_tts_last_playback_dropped"] = True
                    self._g["_tts_last_playback_dropped_ts"] = time.time()
                    self._g["_tts_last_playback_dropped_chars"] = int(n_samples)
                    print(
                        f"[tts_worker] WARNING: playback dropped "
                        f"(played {idx}/{n_samples} samples; "
                        f"muted={self._muted()} shutdown={self._stop_evt.is_set()})"
                    )
                except Exception:
                    pass

    def _speak_pyttsx3(self, text: str, emotion: str, intensity: float) -> None:
        rate, volume = _emotion_to_rate_volume(emotion, intensity)
        try:
            self._pyttsx3.setProperty("rate", rate)
            self._pyttsx3.setProperty("volume", volume)
        except Exception:
            pass
        # No live amplitude with SAPI5 — use a coarse estimate based on text length.
        est = min(0.85, 0.30 + len(text) / 500.0)
        _set_live_amplitude(est)
        # SAPI5 doesn't expose word callbacks here; show the full reply as
        # spoken_so_far so the UI still gets text on this fallback path.
        _speech_set(full=text, spoken=text, current="")
        try:
            self._pyttsx3.say(text)
            self._pyttsx3.runAndWait()
            print(f"[tts_worker] pyttsx3 spoke ({rate}rate {volume:.2f}vol): {text[:60]!r}")
        finally:
            _set_live_amplitude(0.0)

    # ── public API ─────────────────────────────────────────────────────────────

    def speak(
        self,
        text: str,
        emotion: str = "neutral",
        intensity: float = 0.5,
        blocking: bool = False,
        rate: int | None = None,    # accepted for backwards compat (pyttsx3 path)
        volume: float | None = None,  # accepted for backwards compat
    ) -> None:
        """Queue text for speaking. emotion/intensity drive Kokoro voice + speed
        and pyttsx3 rate/volume. rate/volume kwargs override only on pyttsx3.
        """
        if not self._available or not text or not text.strip():
            return
        # When caller passes rate/volume explicitly (legacy callers), translate to
        # an emotion that produces approximately that rate. Otherwise use the
        # emotion/intensity we were given.
        if rate is not None or volume is not None:
            # Heuristic: just store and apply rate/volume directly via pyttsx3 path
            # by labelling emotion="neutral" and intensity=0; pyttsx3 path then
            # overrides with the legacy values.
            pass
        _trace(f"tts.enqueue chars={len(text.strip())} blocking={blocking}")  # TRACE-PHASE1
        item = (text.strip(), emotion or "neutral", float(intensity), None)
        if blocking:
            done = threading.Event()
            self._queue.put((item[0], item[1], item[2], done))
            done.wait(timeout=60.0)
        else:
            self._queue.put(item)

    def speak_with_emotion(
        self,
        text: str,
        emotion: str = "neutral",
        intensity: float = 0.5,
        blocking: bool = False,
    ) -> None:
        self.speak(text, emotion=emotion, intensity=intensity, blocking=blocking)

    def apply_style(self, rate: int | None = None, volume: float | None = None) -> None:
        """Compatibility shim used by the legacy tts_engine.py voice_style path.
        With Kokoro we ignore rate/volume — emotion+intensity drive everything.
        With pyttsx3 we update the engine's defaults so the next say() uses them.
        """
        if self._engine_type != "pyttsx3" or self._pyttsx3 is None:
            return
        try:
            if rate is not None:
                self._pyttsx3.setProperty("rate", max(120, min(220, int(rate))))
            if volume is not None:
                self._pyttsx3.setProperty("volume", max(0.6, min(1.0, float(volume))))
        except Exception:
            pass

    def stop(self) -> None:
        """Stop current speech immediately — but ONLY if mute is the cause.

        The only legitimate reasons to interrupt Ava mid-sentence are:
          1. User said "mute" / "stop talking" → g["_tts_muted"] = True
          2. Process shutdown via shutdown()

        Any other caller invoking stop() is a bug — we no-op so a stray
        focus-change handler can't cut Ava off mid-word.
        """
        if not self._muted() and not self._stop_evt.is_set():
            print("[tts_worker] stop() called without mute — ignoring (audio protected)")
            return
        try:
            if self._engine_type == "kokoro" and self._sd is not None:
                self._sd.stop()
            elif self._engine_type == "pyttsx3" and self._pyttsx3 is not None:
                self._pyttsx3.stop()
        except Exception:
            pass
        _set_live_amplitude(0.0)
        self._set_speaking_state(False, 0.0)

    def shutdown(self) -> None:
        self._stop_evt.set()
        try:
            self._queue.put(None)
        except Exception:
            pass

    # ── status ─────────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        return self._available

    @property
    def available(self) -> bool:
        return self._available

    def is_speaking(self) -> bool:
        return self._currently_speaking.is_set()

    def is_busy(self) -> bool:
        """True if currently speaking OR queue has pending items.

        Used by voice_loop's drain-wait when run_ava streamed the reply
        directly via per-chunk speak() calls — we need to wait for ALL
        queued chunks to finish, not just the currently-playing one.
        """
        if self._currently_speaking.is_set():
            return True
        try:
            return not self._queue.empty()
        except Exception:
            return False

    def engine_name(self) -> str:
        return self._engine_type

    def voice_name(self) -> str:
        return self._voice_name

    def current_amplitude(self) -> float:
        return get_live_amplitude()


# ── Module singleton ──────────────────────────────────────────────────────────
_singleton: Optional[TTSWorker] = None
_singleton_lock = threading.Lock()


def get_tts_worker(g: Optional[dict[str, Any]] = None) -> TTSWorker:
    """Return process-wide TTSWorker singleton, creating it lazily.

    `g` is optional but recommended on first call so the worker can publish
    _tts_speaking / _tts_amplitude to globals. If passed on a later call the
    worker late-binds via attach_globals().
    """
    global _singleton
    if _singleton is not None:
        if g is not None and _singleton._g is None:
            _singleton.attach_globals(g)
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = TTSWorker(g=g)
        elif g is not None and _singleton._g is None:
            _singleton.attach_globals(g)
    return _singleton
