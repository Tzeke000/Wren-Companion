"""
Phase 74 — Real Voice Loop (STT → LLM → TTS tight loop) with attentive state.

States:
  passive   → wait for wake word / clap. Slow polling.
  attentive → just finished speaking. Faster polling, no wake word required;
              any speech > 1s is treated as direct address. Decays back to
              passive after 60s of silence.
  listening → actively recording user speech.
  thinking  → run_ava is producing a reply.
  speaking  → TTS is playing.

Wake-word recognition uses brain.wake_detector. When a transcribed clip is
ambiguous, brain.wake_learner asks for clarification. After STT completes we
also analyse the audio with brain.voice_mood_detector and stash the result
for prompt_builder.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any


def _trace(label: str) -> None:  # TRACE-PHASE1
    """Timestamped diagnostic trace for the voice path. Removed/gated in Phase 3."""  # TRACE-PHASE1
    ts = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"  # TRACE-PHASE1
    print(f"[trace] {ts} {label}")  # TRACE-PHASE1


# ── Tunables (overridable via config/voice_tuning.json) ──────────────────────

_DEFAULT_TUNING: dict[str, Any] = {
    # Attentive window — how long after Ava speaks she keeps listening
    # without requiring a wake word.
    "attentive_base_seconds": 180,
    # Early-exit silence threshold during attentive (seconds without user speech).
    "attentive_silence_exit_seconds": 30,
    # If true, also require user gaze on screen to stay in attentive past
    # the silence threshold.
    "attentive_require_gaze_to_stay": True,
    # Minimum speech duration to treat an attentive utterance as direct input.
    "attentive_min_speech_seconds": 1.0,
    # Per-listen silence cutoffs.
    "default_silence_seconds": 2.5,
    "continue_silence_seconds": 4.0,
    "long_silence_seconds": 1.5,
    "short_words": 3,
    "long_words": 10,
}


def _load_tuning() -> dict[str, Any]:
    """Read config/voice_tuning.json if present; merge over defaults."""
    cfg_path = Path("config") / "voice_tuning.json"
    out = dict(_DEFAULT_TUNING)
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out.update({k: v for k, v in data.items() if k in _DEFAULT_TUNING})
        except Exception as e:
            print(f"[voice_loop] tuning load error: {e}")
    return out


_TUNING = _load_tuning()
_ATTENTIVE_BASE_SECONDS = float(_TUNING["attentive_base_seconds"])
_ATTENTIVE_SILENCE_EXIT_SECONDS = float(_TUNING["attentive_silence_exit_seconds"])
_ATTENTIVE_REQUIRE_GAZE = bool(_TUNING["attentive_require_gaze_to_stay"])
_ATTENTIVE_MIN_SPEECH_SEC = float(_TUNING["attentive_min_speech_seconds"])
_DEFAULT_SILENCE_SEC = float(_TUNING["default_silence_seconds"])
_CONTINUE_SILENCE_SEC = float(_TUNING["continue_silence_seconds"])
_LONG_SILENCE_SEC = float(_TUNING["long_silence_seconds"])
_SHORT_WORDS = int(_TUNING["short_words"])
_LONG_WORDS = int(_TUNING["long_words"])

# Transcript-wake patterns. Match BEFORE Eva→Ava normalization so we catch
# "Eva" as well; voice_loop strips the wake phrase before passing to run_ava.
_TRANSCRIPT_WAKE_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("hey_ava",   re.compile(r"\bhey\s+(?:ava|eva)\b", re.IGNORECASE)),
    ("hi_ava",    re.compile(r"\bhi\s+(?:ava|eva)\b", re.IGNORECASE)),
    ("hello_ava", re.compile(r"\bhello\s+(?:ava|eva)\b", re.IGNORECASE)),
    ("yo_ava",    re.compile(r"\byo\s+(?:ava|eva)\b", re.IGNORECASE)),
    ("ok_ava",    re.compile(r"\bok(?:ay)?\s+(?:ava|eva)\b", re.IGNORECASE)),
]
# Match "ava" alone only at start of a short utterance (≤ 4 words).
_TRANSCRIPT_WAKE_BARE_AVA = re.compile(r"^\s*(?:ava|eva)\b", re.IGNORECASE)


def _classify_transcript_wake(text: str) -> tuple[bool, str, str]:
    """Return (matched, source_label, stripped_text).

    matched=True only if text contains a transcript-wake pattern.
    stripped_text has the wake phrase removed.
    """
    if not text or not text.strip():
        return False, "", ""
    raw = text.strip()
    # Try the explicit "hey ava" / etc patterns first.
    for label, pat in _TRANSCRIPT_WAKE_PATTERNS:
        m = pat.search(raw)
        if m:
            stripped = (raw[:m.start()] + raw[m.end():]).strip(" ,.!?-")
            return True, f"transcript_wake:{label}", stripped
    # Then "Ava ..." at start of a short utterance.
    word_count = len(raw.split())
    if word_count <= 4:
        m = _TRANSCRIPT_WAKE_BARE_AVA.match(raw)
        if m:
            stripped = raw[m.end():].strip(" ,.!?-")
            return True, "transcript_wake:bare_ava", stripped
    return False, "", ""


class VoiceLoop:
    STATES = ("passive", "attentive", "listening", "thinking", "speaking")

    def __init__(self, g: dict[str, Any]) -> None:
        self._g = g
        self._state = "passive"
        self._active = False
        self._thread: threading.Thread | None = None
        self._last_audio_ts: float = 0.0  # any speech detected
        self._last_speak_end_ts: float = 0.0
        self._last_audio_array: Any = None  # cached for voice mood

    # ── public interface ──────────────────────────────────────

    def start(self) -> bool:
        stt = self._g.get("stt_engine")
        tts = self._g.get("tts_engine")
        if stt is None or tts is None:
            return False
        if not (callable(getattr(stt, "is_available", None)) and stt.is_available()):
            return False
        if not (callable(getattr(tts, "is_available", None)) and tts.is_available()):
            return False
        self._active = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ava-voice-loop")
        self._thread.start()
        print("[voice_loop] started passive listening")
        return True

    def stop(self) -> None:
        self._active = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        return self._active

    def on_wake(self) -> None:
        """Called by wake word / clap detector to trigger active listening."""
        if not self._active:
            return
        self._g["_voice_loop_wake_requested"] = True

    # ── internal loop ─────────────────────────────────────────

    def _set_state(self, state: str) -> None:
        prev = self._state
        self._state = state
        self._g["_voice_loop_state"] = state
        # _conversation_active spans the entire conversation window — wake →
        # speaking → attentive → wake → ... — so background subsystems
        # (curiosity, dual_brain Stream B, proactive triggers, question_engine)
        # know to defer until Zeke and Ava are done. Cleared only when we drop
        # back to passive (timeout or user_disengaged from _attentive_wait).
        if state in ("listening", "attentive", "thinking", "speaking"):
            self._g["_conversation_active"] = True
        elif state == "passive":
            self._g["_conversation_active"] = False
        # Drive the orb's inner-state line. reply_engine overrides this with
        # "thinking — fast path" / "thinking — full path" while it runs.
        if state in ("listening", "attentive"):
            self._g["_inner_state_line"] = "listening"
        elif state == "thinking":
            # Leave any reply_engine-set value alone if it's already a
            # thinking-* variant; otherwise default to plain "thinking".
            cur = str(self._g.get("_inner_state_line") or "")
            if not cur.startswith("thinking"):
                self._g["_inner_state_line"] = "thinking"
        elif state == "speaking":
            # Empty so the speaking-text region above the orb takes over.
            self._g["_inner_state_line"] = ""
        elif state == "passive":
            self._g["_inner_state_line"] = ""
        if prev != state:
            print(f"[voice_loop] state: {prev} → {state}")

    def _loop(self) -> None:
        while self._active:
            try:
                # ── Choose passive vs attentive ────────────────────────────
                # Take the max of our own _last_speak_end_ts and the global —
                # so question_engine / proactive_triggers / face_greeting
                # speech (which goes through tts_worker directly, not via
                # voice_loop._speak) also drops us into attentive afterwards.
                _global_speak_end = float(self._g.get("_last_speak_end_ts") or 0.0)
                if _global_speak_end > self._last_speak_end_ts:
                    self._last_speak_end_ts = _global_speak_end
                in_attentive = (
                    self._last_speak_end_ts > 0
                    and (time.time() - self._last_speak_end_ts) < _ATTENTIVE_BASE_SECONDS
                )
                if in_attentive:
                    self._set_state("attentive")
                    triggered = self._attentive_wait()
                else:
                    self._set_state("passive")
                    triggered = self._passive_wait()
                if not self._active:
                    break
                if not triggered:
                    # attentive timed out → fall back to passive
                    continue
                self._listen_and_respond()
            except Exception as e:
                print(f"[voice_loop] error in loop: {e}")
                self._set_state("passive")
                time.sleep(2.0)

    def _should_drop_self_listen(self) -> bool:
        """Return True if the current audio capture would just hear Ava's
        own TTS through the speakers. Used to gate listen_session calls so
        Whisper never transcribes Ava's voice as user input ("self-listen").

        Wake sources (clap detector, openWakeWord) bypass this gate via the
        explicit `_voice_loop_wake_requested` / `_wake_word_detected` checks
        that fire BEFORE listen_session in each loop body, so the user can
        still interrupt Ava mid-sentence.

        200ms trailing window after last_speak_end_ts catches the audio
        buffer's trailing edge — Kokoro's last samples can still be on the
        OutputStream when _tts_speaking drops to False.
        """
        if bool(self._g.get("_tts_speaking")):
            return True
        last_end = float(self._g.get("_last_speak_end_ts") or 0.0)
        if last_end > 0 and (time.time() - last_end) < 0.2:
            return True
        return False

    def _passive_wait(self) -> bool:
        """Block until wake word / clap fires. Returns True when triggered."""
        while self._active:
            if self._g.get("_voice_loop_wake_requested") or self._g.get("_wake_word_detected"):
                self._g.pop("_voice_loop_wake_requested", None)
                self._g.pop("_wake_word_detected", None)
                return True
            time.sleep(0.2)
        return False

    def _attentive_wait(self) -> bool:
        """Open-conversation window: poll mic faster, react to any speech > 1s
        without requiring a wake word.

        Exit conditions:
          1. Wake/clap fires → return True (defer to wake path).
          2. User speech > _ATTENTIVE_MIN_SPEECH_SEC → return True with text stashed.
          3. Hard timeout: time since last speak >= _ATTENTIVE_BASE_SECONDS.
             The timer RESETS each time the user speaks (rolling extension).
          4. Early exit: silence ≥ _ATTENTIVE_SILENCE_EXIT_SECONDS AND
             gaze is not 'center' (user disengaged). Skipped if
             _ATTENTIVE_REQUIRE_GAZE is False — then silence-only triggers
             early exit.
        """
        attentive_started = time.time()
        last_user_audio_ts = time.time()  # rolling silence timer base
        while self._active:
            # Wake / clap triggers immediately exit to listening.
            if self._g.get("_voice_loop_wake_requested") or self._g.get("_wake_word_detected"):
                self._g.pop("_voice_loop_wake_requested", None)
                self._g.pop("_wake_word_detected", None)
                return True
            # Quick mic snapshot for ~0.8s — if we hear something significant, trigger.
            heard_audio_this_tick = False
            stt = self._g.get("stt_engine")
            # Self-listen guard — don't capture Ava's own TTS as user input.
            if self._should_drop_self_listen():
                time.sleep(0.1)
                continue
            if stt is not None and callable(getattr(stt, "is_available", None)) and stt.is_available():
                try:
                    snap = stt.listen_session(max_seconds=0.8, silence_seconds=0.4)
                    if isinstance(snap, dict) and snap.get("speech_detected"):
                        dur = float(snap.get("duration_seconds") or 0)
                        # Any detected audio resets the silence timer.
                        if dur >= 0.3:
                            heard_audio_this_tick = True
                            last_user_audio_ts = time.time()
                            # Reset the rolling base so the 180s timer restarts
                            # from this moment for the hard timeout too.
                            self._last_speak_end_ts = time.time()
                        if dur >= _ATTENTIVE_MIN_SPEECH_SEC:
                            self._g["_attentive_initial_text"] = snap.get("text") or ""
                            return True
                except Exception:
                    pass

            now = time.time()
            silence_since_audio = now - last_user_audio_ts
            since_speak_end = now - self._last_speak_end_ts

            # Early exit on disengagement.
            if silence_since_audio >= _ATTENTIVE_SILENCE_EXIT_SECONDS:
                if _ATTENTIVE_REQUIRE_GAZE:
                    gaze = str(self._g.get("_gaze_region") or "").lower()
                    looking_away = gaze and gaze != "center"
                    if looking_away:
                        print(f"[voice_loop] exiting attentive: user_disengaged (silence={silence_since_audio:.0f}s, gaze={gaze})")
                        _trace(f"vl.enter_passive disengaged silence={silence_since_audio:.0f} gaze={gaze}")  # TRACE-PHASE1
                        return False
                else:
                    print(f"[voice_loop] exiting attentive: silence (silence={silence_since_audio:.0f}s)")
                    _trace(f"vl.enter_passive silence_exit silence={silence_since_audio:.0f}")  # TRACE-PHASE1
                    return False

            # Hard timeout — 180s without any user audio at all.
            if since_speak_end >= _ATTENTIVE_BASE_SECONDS:
                print(f"[voice_loop] exiting attentive: timeout ({since_speak_end:.0f}s ≥ {_ATTENTIVE_BASE_SECONDS:.0f}s)")
                _trace(f"vl.enter_passive timeout {since_speak_end:.0f}s")  # TRACE-PHASE1
                return False
            time.sleep(0.4)
        return False

    def _silence_seconds_for(self, word_count: int) -> float:
        if word_count < _SHORT_WORDS:
            return _CONTINUE_SILENCE_SEC
        if word_count > _LONG_WORDS:
            return _LONG_SILENCE_SEC
        return _DEFAULT_SILENCE_SEC

    def _listen_and_respond(self) -> None:
        if self._g.get("input_muted"):
            print("[voice_loop] skipped — input_muted is True")
            return

        stt = self._g.get("stt_engine")
        tts = self._g.get("tts_engine")
        if stt is None or tts is None:
            print(f"[voice_loop] skipped — stt={'set' if stt else 'None'} tts={'set' if tts else 'None'}")
            return

        # ── LISTEN ────────────────────────────────────────────────────────────
        self._set_state("listening")
        _trace("vl.enter_listening")  # TRACE-PHASE1
        print("[voice_loop] listening…")
        initial_text = str(self._g.pop("_attentive_initial_text", "") or "").strip()
        try:
            # Initial pass at 2.5s silence cutoff — most utterances finish here.
            result = stt.listen_session(max_seconds=12.0, silence_seconds=_DEFAULT_SILENCE_SEC)
        except Exception as e:
            print(f"[voice_loop] listen_session error: {e!r}")
            self._set_state("passive")
            return

        if result is None or not result.get("speech_detected"):
            # If we entered listening because attentive heard a partial, keep that.
            if initial_text:
                text = initial_text
                result = {"text": text, "speech_detected": True, "duration_seconds": 1.0}
            else:
                # Try wake-transcript fallback BEFORE giving up — covers the
                # synthesized-voice case (Piper played to CABLE Input, finished
                # before listen started, leaving listen to capture silence).
                # The fallback below at the existing location handles
                # "speech_detected but text empty" — this here handles
                # "no speech detected at all".
                import os as _os_vl_early
                _vl_test_mode_early = _os_vl_early.environ.get("AVA_TEST_MODE", "0").strip() == "1"
                _wake_tx_window_early = 30.0 if _vl_test_mode_early else 5.0
                _wake_tx_early = str(self._g.get("_wake_transcript") or "").strip()
                _wake_tx_ts_early = float(self._g.get("_wake_transcript_ts") or 0.0)
                if _wake_tx_early and (time.time() - _wake_tx_ts_early) <= _wake_tx_window_early:
                    print(f"[voice_loop] no speech in listen — falling back to wake-phase transcript: {_wake_tx_early[:120]!r}")
                    text = _wake_tx_early
                    result = {"text": text, "speech_detected": True, "duration_seconds": 1.0}
                    self._g.pop("_wake_transcript", None)
                    self._g.pop("_wake_transcript_ts", None)
                else:
                    print("[voice_loop] no speech detected")
                    self._set_state("passive")
                    return
        text = str((result or {}).get("text") or "").strip()
        if initial_text and not text.lower().startswith(initial_text.lower()[:20]):
            text = (initial_text + " " + text).strip()

        # Word-count-aware continuation: if Zeke gave us only a couple words,
        # extend the silence window and keep listening up to 4s more.
        word_count = len(text.split())
        if word_count > 0 and word_count < _SHORT_WORDS:
            try:
                cont = stt.listen_session(max_seconds=6.0, silence_seconds=_CONTINUE_SILENCE_SEC)
                if isinstance(cont, dict) and cont.get("speech_detected"):
                    extra = str(cont.get("text") or "").strip()
                    if extra:
                        text = (text + " " + extra).strip()
                        print(f"[voice_loop] short utterance extended: +{len(extra)} chars")
            except Exception:
                pass

        # ── WAKE-TRANSCRIPT FALLBACK ──────────────────────────────────────────
        # If listening captured nothing but Whisper-poll's wake-phase chunk
        # has a fresh transcript, use that instead. Handles the case where
        # user says "Hey Ava, X" in one breath: Whisper-poll's capture window
        # catches the whole phrase but listening's NEW recording starts
        # post-wake and only captures end-of-utterance silence. See
        # brain/wake_word.py:_whisper_poll_loop where _wake_transcript is
        # stashed. Vault: 2026-05 work order Phase B retry.
        # In AVA_TEST_MODE the window is widened to 30s because synthesized
        # voice via the audio loopback harness can have larger wake→listen
        # gaps than human speech.
        import os as _os_vl
        _vl_test_mode = _os_vl.environ.get("AVA_TEST_MODE", "0").strip() == "1"
        _wake_tx_window_s = 30.0 if _vl_test_mode else 5.0
        if not text:
            wake_tx = str(self._g.get("_wake_transcript") or "").strip()
            wake_tx_ts = float(self._g.get("_wake_transcript_ts") or 0.0)
            if wake_tx and (time.time() - wake_tx_ts) <= _wake_tx_window_s:
                print(f"[voice_loop] empty listen — falling back to wake-phase transcript: {wake_tx[:120]!r}")
                text = wake_tx
                # Consume so it doesn't fire on a subsequent passive→listening cycle
                self._g.pop("_wake_transcript", None)
                self._g.pop("_wake_transcript_ts", None)

        if not text:
            print("[voice_loop] empty transcription after listen")
            self._set_state("passive")
            return
        _trace(f"vl.heard chars={len(text)}")  # TRACE-PHASE1
        print(f"[voice_loop] heard: {text[:120]!r}")

        # ── TRANSCRIPT WAKE FALLBACK ──────────────────────────────────────────
        # If no explicit wake source has been stamped (no clap, no openWakeWord
        # fire), see if the transcript itself starts with "hey ava" / "hi ava"
        # / "hello ava" / "yo ava" / "ok ava" / "ava <short>". If yes, that's
        # an explicit direct-address signal — stamp the source and strip the
        # wake phrase before passing to run_ava.
        existing_wake = str(self._g.get("_wake_source") or "")
        if not existing_wake:
            tw_matched, tw_label, tw_stripped = _classify_transcript_wake(text)
            if tw_matched:
                print(f"[voice_loop] transcript-wake match: {tw_label} → stripped={tw_stripped[:80]!r}")
                self._g["_wake_source"] = tw_label
                self._g["_wake_source_ts"] = time.time()
                if tw_stripped:
                    text = tw_stripped

        # ── WAKE-WORD CHECK (passive mode only) ───────────────────────────────
        # In attentive state we already decided this is for Ava. In passive we
        # need to confirm direct address before invoking run_ava.
        in_attentive_when_started = self._state in ("attentive", "listening") and self._last_speak_end_ts > 0 and (
            time.time() - self._last_speak_end_ts
        ) < _ATTENTIVE_BASE_SECONDS

        # Explicit wake sources (clap or openWakeWord model or transcript_wake)
        # → ALWAYS direct, no classification, no clarify. Each detector stamps
        # _wake_source before raising the wake flag.
        wake_source = str(self._g.get("_wake_source") or "")
        if wake_source in ("clap", "openwakeword") or wake_source.startswith("transcript_wake"):
            print(f"[voice_loop] {wake_source}-triggered → bypassing wake classification")
            # Consume the wake_source so a later passive listen re-classifies.
            self._g.pop("_wake_source", None)
            self._g.pop("_wake_source_ts", None)
            # Skip wake_detector entirely — fall through to run_ava.
        elif not in_attentive_when_started:
            try:
                from brain.wake_detector import get_wake_detector
                wd = get_wake_detector()
                is_direct, conf, reason = wd.classify(text, self._g)
                print(f"[voice_loop] wake-classify direct={is_direct} conf={conf:.2f} reason={reason}")
                # Borderline → ask for clarification AND wait for the answer.
                if not is_direct or conf < 0.6:
                    handled_via_clarify = self._handle_clarification(text, is_direct=is_direct)
                    if handled_via_clarify is not None:
                        # Either we got a yes (handled_via_clarify holds the
                        # phrase to run) or a no (None returned from helper);
                        # treat True as "proceed", False as "skip".
                        if handled_via_clarify:
                            # Continue with the original text into run_ava.
                            pass
                        else:
                            self._set_state("passive")
                            return
                    elif not is_direct:
                        # No clarification was attempted (cooldown or unable)
                        # AND classifier said indirect → skip.
                        print("[voice_loop] not direct address — skipping")
                        self._set_state("passive")
                        return
            except Exception as e:
                print(f"[voice_loop] wake detector error: {e!r}")

        # ── VOICE MOOD ANALYSIS (best-effort, reuses STT audio) ───────────────
        try:
            self._analyze_voice_mood_from_result(result)
        except Exception as e:
            print(f"[voice_loop] voice mood error: {e!r}")

        # ── THINKING ──────────────────────────────────────────────────────────
        self._set_state("thinking")
        _trace(f"vl.calling_run_ava chars={len(text)}")  # TRACE-PHASE1
        # Turn-in-progress flag — gates app discovery and any other heavy
        # background work that might starve the LLM. Cleared after TTS done.
        self._g["_turn_in_progress"] = True
        self._g["_turn_started_ts"] = time.time()
        print(f"[voice_loop] calling run_ava with: {text[:100]!r}")
        # Run run_ava in a worker thread with a hard 120s deadline. Without
        # the timeout, a hung run_ava (observed intermittently — see vault
        # bugs/voice-loop-restart-hang.md) wedges voice_loop's `thinking`
        # state forever. With the timeout, voice_loop drops to `passive` and
        # the user can wake Ava again. Worker is daemon=True so it doesn't
        # block process exit; on timeout we DON'T spawn a new run_ava (the
        # ghost worker is the cost of one stuck Ollama call, bounded).
        run_ava_result = None
        run_ava_error: list = [None]
        run_ava_done = threading.Event()

        def _vl_run_ava_worker():
            try:
                from brain.reply_engine import run_ava
                print(f"[vl-diag] worker entering run_ava", flush=True)
                run_ava_error[0] = run_ava(text)
                print(f"[vl-diag] worker run_ava returned cleanly", flush=True)
            except Exception as _ra_e:
                run_ava_error[0] = _ra_e
                import traceback as _tb
                print(f"[voice_loop] run_ava failed: {_ra_e!r}\n{_tb.format_exc()[:600]}", flush=True)
            finally:
                run_ava_done.set()

        try:
            print(f"[vl-diag] about to call run_ava (with 120s timeout)", flush=True)
            _t = threading.Thread(target=_vl_run_ava_worker, daemon=True, name="ava-vl-run-ava")
            _t.start()
            if not run_ava_done.wait(timeout=120.0):
                print(f"[vl-diag] run_ava TIMEOUT after 120s — abandoning ghost worker", flush=True)
                self._g.pop("_turn_in_progress", None)
                self._g.pop("_turn_started_ts", None)
                self._set_state("passive")
                return
            result = run_ava_error[0]
            if isinstance(result, Exception):
                self._g.pop("_turn_in_progress", None)
                self._g.pop("_turn_started_ts", None)
                self._set_state("passive")
                return
            run_ava_result = result
            print(f"[vl-diag] run_ava returned, type={type(run_ava_result).__name__}", flush=True)
            try:
                _len = len(run_ava_result) if run_ava_result is not None else -1
            except Exception:
                _len = -2
            print(f"[vl-diag] result len={_len}", flush=True)
            reply, _visual, _profile, _actions, _reflection = run_ava_result
            print(f"[vl-diag] unpack ok reply_type={type(reply).__name__}", flush=True)
            _trace(f"vl.run_ava_returned chars={len(str(reply or ''))}")  # TRACE-PHASE1
            print(f"[voice_loop] run_ava returned reply_chars={len(str(reply or ''))}", flush=True)
        except Exception as e:
            import traceback as _tb
            print(f"[voice_loop] run_ava handoff failed: {e!r}\n{_tb.format_exc()[:600]}", flush=True)
            self._g.pop("_turn_in_progress", None)
            self._g.pop("_turn_started_ts", None)
            self._set_state("passive")
            return

        reply_text = str(reply or "").strip()
        if not reply_text:
            self._g.pop("_turn_in_progress", None)
            self._g.pop("_turn_started_ts", None)
            print("[voice_loop] reply was empty — nothing to speak")
            self._set_state("passive")
            return
        print(f"[voice_loop] reply preview: {reply_text[:80]!r}")

        # ── SPEAKING ──────────────────────────────────────────────────────────
        self._set_state("speaking")
        import re as _re
        clean = _re.sub(r"[*_`#\[\]()]", "", reply_text)
        clean = _re.sub(r"\s+", " ", clean).strip()[:400]
        if not (clean and _re.search(r"[A-Za-z0-9]", clean)):
            self._g.pop("_turn_in_progress", None)
            self._g.pop("_turn_started_ts", None)
            print("[voice_loop] reply had no speakable content after cleanup")
            self._set_state("passive")
            return

        tts_enabled = bool(self._g.get("tts_enabled", False))
        if not tts_enabled:
            self._g.pop("_turn_in_progress", None)
            self._g.pop("_turn_started_ts", None)
            print("[voice_loop] TTS disabled in globals — not speaking. Toggle via /api/v1/tts/toggle.")
            self._set_state("passive")
            return

        _trace(f"vl.tts_enqueued chars={len(clean)}")  # TRACE-PHASE1
        spoke_ok = self._speak(clean)
        self._last_speak_end_ts = time.time() if spoke_ok else 0.0
        # Mirror to globals so other paths (heartbeat checks, snapshot) and
        # any future code that reads the public marker stay in sync.
        if spoke_ok:
            self._g["_last_speak_end_ts"] = self._last_speak_end_ts
        _trace(f"vl.tts_done ok={spoke_ok}")  # TRACE-PHASE1
        # Clear turn-in-progress flag — app discovery and other background
        # work can resume.
        self._g.pop("_turn_in_progress", None)
        self._g.pop("_turn_started_ts", None)

        # Drop into attentive so a quick follow-up doesn't need a wake word.
        if spoke_ok:
            self._set_state("attentive")
            _trace("vl.enter_attentive")  # TRACE-PHASE1
        else:
            self._set_state("passive")
            _trace("vl.enter_passive ok_false")  # TRACE-PHASE1

    # ── helpers ───────────────────────────────────────────────────────────────

    def _handle_clarification(self, original_text: str, is_direct: bool) -> "bool | None":
        """Ask WakeLearner to speak a clarification, then BLOCK and wait up
        to 8 seconds for a yes/no answer. Persist the answer via
        learn_from_correction so future utterances of this shape don't need
        re-asking.

        Returns:
          True   → user confirmed; caller should proceed with run_ava
          False  → user denied; caller should skip
          None   → no clarification was attempted (cooldown, no TTS, etc).
        """
        try:
            from brain.wake_learner import get_wake_learner
            wl = get_wake_learner()
            if wl is None or not wl.can_clarify():
                return None
            asked = wl.ask_clarification(original_text, self._g)
            if not asked:
                return None
        except Exception as _e:
            print(f"[voice_loop] wake clarify dispatch error: {_e!r}")
            return None

        # Wait for yes/no with a tight short-listen.
        stt = self._g.get("stt_engine")
        if stt is None or not callable(getattr(stt, "listen_short", None)):
            print("[voice_loop] no listen_short available — clarification can't be answered")
            return None
        print("[voice_loop] waiting up to 8s for clarification answer…")
        try:
            answer = stt.listen_short(max_seconds=8.0, silence_seconds=1.0) or ""
        except Exception as e:
            print(f"[voice_loop] clarification listen error: {e!r}")
            return None
        a = (answer or "").lower().strip()
        if not a:
            print("[voice_loop] no clarification answer — skipping")
            self._g.pop("_wake_clarify_pending", None)
            return False

        yes_words = (
            "yes", "yeah", "yep", "yup", "correct", "right",
            "i was", "talking to you", "that's right", "thats right",
            "you", "ya", "uh huh", "uh-huh", "mhm",
        )
        no_words = (
            "no", "nope", "nah", "not", "wasn't", "wasnt", "wasn't talking",
            "not you", "not to you", "different", "other one",
        )

        is_yes = any(w in a for w in yes_words)
        is_no = any(w in a for w in no_words)
        if is_yes and not is_no:
            print(f"[voice_loop] clarification YES → run_ava with original")
            try:
                wl.learn_from_correction(original_text, was_direct=True, g=self._g)
            except Exception:
                pass
            self._g.pop("_wake_clarify_pending", None)
            return True
        if is_no and not is_yes:
            print(f"[voice_loop] clarification NO → skip and learn indirect")
            try:
                wl.learn_from_correction(original_text, was_direct=False, g=self._g)
            except Exception:
                pass
            self._g.pop("_wake_clarify_pending", None)
            return False
        # Unclear — neither yes nor no. Skip this turn.
        print(f"[voice_loop] clarification ambiguous ({a!r}) — skipping this turn")
        self._g.pop("_wake_clarify_pending", None)
        return False

    def _analyze_voice_mood_from_result(self, stt_result: dict | None) -> None:
        """Run voice_mood_detector on the audio array STT already captured.

        Avoids the 1.5s extra recording the previous implementation did. If the
        STT result doesn't include the audio (older API or fallback path), we
        just skip — no fresh recording, no added latency. Best-effort.
        """
        det = self._g.get("_voice_mood_detector")
        if det is None or not getattr(det, "available", False):
            return
        if not isinstance(stt_result, dict):
            return
        audio = stt_result.get("audio_array")
        sr = int(stt_result.get("sample_rate") or 16000)
        if audio is None:
            return
        try:
            mood = det.analyze(audio, sr=sr)
            mood["ts"] = time.time()
            self._g["_voice_mood"] = mood
            print(
                f"[voice_mood] {mood.get('label')} energy={mood.get('energy')} "
                f"q={mood.get('is_question')} (reused STT audio)"
            )
        except Exception as e:
            print(f"[voice_mood] analyze error: {e!r}")

    def _speak(self, clean: str) -> bool:
        # Streaming-chunk coordination: when run_ava streamed the reply directly
        # into the TTS queue (Component 1 of conversational naturalness), we
        # don't re-enqueue here. Just wait for the queued chunks to finish
        # playing. See docs/CONVERSATIONAL_DESIGN.md.
        if self._g.get("_streamed_reply"):
            worker_for_drain = self._g.get("_tts_worker")
            if worker_for_drain is not None:
                _drain_t0 = time.time()
                try:
                    while time.time() - _drain_t0 < 60.0:
                        _busy = (
                            getattr(worker_for_drain, "is_busy", lambda: False)()
                            if hasattr(worker_for_drain, "is_busy")
                            else worker_for_drain.is_speaking()
                        )
                        if not _busy:
                            break
                        time.sleep(0.05)
                except Exception as _drain_e:
                    print(f"[voice_loop] drain wait error: {_drain_e!r}")
            self._g["_streamed_reply"] = False
            print("[voice_loop] streamed reply drained")
            return True

        worker = self._g.get("_tts_worker")
        if worker is not None and getattr(worker, "available", False):
            try:
                emotion = "neutral"
                intensity = 0.5
                try:
                    mood_state = self._g.get("_current_mood")
                    if isinstance(mood_state, dict):
                        emotion = str(mood_state.get("current_mood") or mood_state.get("primary_emotion") or "neutral")
                        intensity = float(mood_state.get("energy") or mood_state.get("intensity") or 0.5)
                    else:
                        load_mood = self._g.get("load_mood")
                        if callable(load_mood):
                            m = load_mood() or {}
                            emotion = str(m.get("current_mood") or m.get("primary_emotion") or "neutral")
                            intensity = float(m.get("energy") or m.get("intensity") or 0.5)
                except Exception:
                    pass
                print(f"[voice_loop] speaking response ({len(clean)} chars) emotion={emotion} intensity={intensity:.2f}")
                worker.speak_with_emotion(clean, emotion=emotion, intensity=intensity, blocking=True)
                print("[voice_loop] done speaking (worker)")
                return True
            except Exception as e:
                print(f"[voice_loop] worker.speak_with_emotion failed: {e!r} — falling back to tts_engine")

        tts = self._g.get("tts_engine")
        speak_callable = callable(getattr(tts, "speak", None))
        if not speak_callable:
            print("[voice_loop] tts.speak is not callable")
            return False
        try:
            print(f"[voice_loop] speaking response ({len(clean)} chars) via tts_engine fallback")
            tts.speak(clean, blocking=True)
            print("[voice_loop] done speaking (engine)")
            return True
        except Exception as e:
            print(f"[voice_loop] TTS failed to speak: {e!r}")
            return False


# ── module singleton ──────────────────────────────────────────

_voice_loop_instance: VoiceLoop | None = None


def get_voice_loop(g: dict[str, Any] | None = None) -> VoiceLoop | None:
    global _voice_loop_instance
    return _voice_loop_instance


def start_voice_loop(g: dict[str, Any]) -> bool:
    global _voice_loop_instance
    loop = VoiceLoop(g)
    ok = loop.start()
    if ok:
        _voice_loop_instance = loop
        g["_voice_loop"] = loop
    return ok
