"""
Phase 22 — natural voice conversation and turn-taking (advisory layer).

Works with Gradio **record-then-stop** voice: each invocation is one user utterance chunk.
Provides soft turn state, pause/readiness **hints**, interruption **heuristics**, continuity
carry-forward for prompts, and **gates proactive** triggers when the user holds the conversational
floor via ``globals()["_voice_user_turn_priority"]``.

Does not replace STT/TTS engines; safe defaults everywhere when not in a voice cycle.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from .perception_types import VoiceConversationResult, VoiceTimingDecision

# Soft turn labels (runtime diagnostics; not enforced UI enums)
LISTENING = "listening"
USER_SPEAKING = "user_speaking"
USER_PAUSE = "user_pause"
ASSISTANT_READY = "assistant_ready"
ASSISTANT_SPEAKING = "assistant_speaking"
YIELDING = "yielding"
INTERRUPTED = "interrupted"
IDLE = "idle"

# Pause / interrupt — conservative (tunable without changing call sites)
_READINESS_COMPLETE_UTTERANCE_BONUS = 0.22
_READINESS_TRUNCATED_PENALTY = 0.28
_INTERRUPT_GAP_MS = 1180.0  # rapid new clip → plausible user cut-in
_ASSISTANT_AUDIO_TAIL_SEC = 0.45  # slack after EST TTS end
_CHARS_PER_SEC_TTS = 12.5  # rough English TTS pacing
_MIN_WORDS_FOR_CONFIDENT_READINESS = 5
_RAPID_GAP_SUPPRESSES_READINESS = 650.0  # ms — still "same breath" follow-up


@dataclass
class _VoiceSession:
    prev_user_text: str = ""
    prev_assistant_reply: str = ""
    last_user_wall_ts: float = 0.0
    last_reply_wall_ts: float = 0.0
    last_tts_estimate_sec: float = 0.0
    interrupted_carry: bool = False
    last_audio_est_sec: float = 0.0


_SESSION = _VoiceSession()


def reset_voice_session() -> None:
    """Reset voice continuity session (tests / disconnect)."""
    global _SESSION
    _SESSION = _VoiceSession()


def _estimate_audio_duration_sec(audio_path: str | None) -> float:
    if not audio_path or not os.path.isfile(audio_path):
        return 0.0
    try:
        import wave

        with wave.open(audio_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return 0.0
            return float(frames) / float(rate)
    except Exception:
        pass
    try:
        sz = os.path.getsize(audio_path)
        return float(max(0.12, min(55.0, sz / 32000.0)))
    except Exception:
        return 0.0


def _speech_duration_from_text(text: str) -> float:
    t = (text or "").strip()
    if not t:
        return 0.25
    words = max(1, len(t.split()))
    return float(max(0.2, min(90.0, words * 0.42)))


def _compute_response_readiness(user_text: str, gap_ms: float, rapid_follow_up: bool) -> tuple[float, list[str], bool]:
    """Higher readiness ⇒ user likely finished an idea (still conservative)."""
    notes: list[str] = []
    t = (user_text or "").strip()
    base = 0.48
    truncated = False
    if gap_ms > 0 and gap_ms < _RAPID_GAP_SUPPRESSES_READINESS:
        base -= 0.12
        notes.append("tight_inter_turn_gap_bias_wait")
        truncated = True
    if rapid_follow_up:
        base -= 0.08
        notes.append("rapid_follow_up")
    low = t.rstrip()
    if low.endswith(("...", "…", "—")):
        base -= _READINESS_TRUNCATED_PENALTY
        truncated = True
        notes.append("truncated_tail")
    elif low and low[-1] in ".?!":
        base += _READINESS_COMPLETE_UTTERANCE_BONUS
        notes.append("sentence_terminal")
    elif low.endswith(","):
        base -= 0.12
        truncated = True
        notes.append("mid_clause_comma")
    wc = len(t.split())
    if wc >= _MIN_WORDS_FOR_CONFIDENT_READINESS:
        base += 0.08
        notes.append("substantive_length")
    elif wc <= 2:
        base -= 0.06
        notes.append("very_short")
    rd = max(0.08, min(0.92, base))
    return rd, notes, truncated


def prepare_voice_turn_for_globals(
    g: dict,
    *,
    user_text: str,
    audio_path: str | None,
) -> VoiceConversationResult:
    """
    Call **before** ``workspace.tick`` / ``run_ava`` on a voice path.

    Writes ``g["_voice_conversation"]`` and sets ``g["_voice_user_turn_priority"]`` when
    ``user_text`` is non-empty (user holds floor this pipeline tick).
    """
    now = time.time()
    vs = _SESSION
    prev_wall = vs.last_user_wall_ts
    wall_gap_ms = (now - prev_wall) * 1000.0 if prev_wall > 0 else 1e6
    vs.last_user_wall_ts = now

    audio_sec = _estimate_audio_duration_sec(audio_path)
    text_sec = _speech_duration_from_text(user_text)
    est_user_speech_ms = max(audio_sec, text_sec * 0.85) * 1000.0
    vs.last_audio_est_sec = float(audio_sec or text_sec)

    gap_since_assistant_reply_ms = (
        (now - vs.last_reply_wall_ts) * 1000.0 if vs.last_reply_wall_ts > 0 else 1e6
    )
    tts_tail_ms = (vs.last_tts_estimate_sec + _ASSISTANT_AUDIO_TAIL_SEC) * 1000.0
    assistant_still_audible = (
        vs.last_reply_wall_ts > 0 and gap_since_assistant_reply_ms < tts_tail_ms
    )

    rapid_follow_up = gap_since_assistant_reply_ms < _INTERRUPT_GAP_MS and bool(
        vs.prev_assistant_reply.strip()
    )
    interrupt_user = rapid_follow_up and assistant_still_audible
    reason = ""
    if interrupt_user:
        reason = "rapid_follow_up_while_assistant_audible"
    elif rapid_follow_up and vs.prev_assistant_reply.strip():
        reason = "rapid_follow_up_possible_overlap"
        vs.interrupted_carry = True
    elif vs.interrupted_carry and wall_gap_ms > 3200:
        vs.interrupted_carry = False

    readiness, rnotes, truncated = _compute_response_readiness(user_text, wall_gap_ms, rapid_follow_up)

    should_respond = readiness >= 0.36 or not truncated
    timing = VoiceTimingDecision(
        should_wait=readiness < 0.42 or truncated,
        should_yield=interrupt_user or vs.interrupted_carry,
        should_interrupt=interrupt_user or bool(reason and "overlap" in reason),
        should_respond=should_respond,
        response_readiness=readiness,
        silence_window_ms=float(min(wall_gap_ms, gap_since_assistant_reply_ms)),
        pacing_notes=rnotes,
        meta={
            "gap_since_assistant_reply_ms": gap_since_assistant_reply_ms,
            "assistant_still_audible": assistant_still_audible,
            "estimated_user_speech_ms": est_user_speech_ms,
        },
    )

    if readiness < 0.38:
        timing.should_wait = True

    turn_state = ASSISTANT_READY
    if interrupt_user or vs.interrupted_carry:
        turn_state = INTERRUPTED
    elif truncated and readiness < 0.45:
        turn_state = USER_PAUSE
    elif assistant_still_audible and not interrupt_user:
        turn_state = ASSISTANT_SPEAKING

    cont_parts = []
    if vs.prev_user_text:
        cont_parts.append(f"prior_user_snippet={_trim(vs.prev_user_text, 120)}")
    if vs.prev_assistant_reply:
        cont_parts.append(f"prior_reply_snippet={_trim(vs.prev_assistant_reply, 120)}")
    if vs.interrupted_carry or interrupt_user:
        cont_parts.append("possible_interruption_or_overlap")
    continuity = "; ".join(cont_parts) if cont_parts else "voice_session_open"

    pacing = list(rnotes)
    if assistant_still_audible:
        pacing.append("assistant_output_recently_audible")
    if readiness < 0.42:
        pacing.append("bias_wait_over_blurt")

    result = VoiceConversationResult(
        turn_state=turn_state,
        user_speaking=False,
        assistant_speaking=assistant_still_audible,
        silence_window_ms=timing.silence_window_ms,
        timing=timing,
        interruption_reason=reason,
        continuity_hint=continuity,
        pacing_notes=pacing,
        notes=["record_stop_voice_path", "phase_22_advisory"],
        meta={
            "audio_est_sec": audio_sec,
            "text_est_sec": text_sec,
            "gap_since_assistant_reply_ms": gap_since_assistant_reply_ms,
        },
    )

    g["_voice_conversation"] = result
    g["_voice_last_user_text"] = user_text.strip()
    g["_voice_user_turn_priority"] = bool(user_text.strip())

    _maybe_record_calibration(interrupt_user, readiness, timing.should_wait)

    _log_voice_turn(result)
    return result


def finalize_voice_turn_after_reply(g: dict, assistant_reply: str | None) -> None:
    """After ``run_ava`` returns — store assistant surface for next-turn continuity."""
    vs = _SESSION
    reply = (assistant_reply or "").strip()
    vs.prev_assistant_reply = reply[-4000:]
    vs.prev_user_text = str(g.get("_voice_last_user_text") or "")[-4000:]
    vs.last_reply_wall_ts = time.time()
    vs.last_tts_estimate_sec = max(0.35, len(reply) / _CHARS_PER_SEC_TTS if reply else 0.45)
    g.pop("_voice_last_user_text", None)


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _log_voice_turn(vc: VoiceConversationResult) -> None:
    t = vc.timing
    print(
        f"[voice_conversation] state={vc.turn_state} readiness={t.response_readiness:.2f} "
        f"wait={t.should_wait} respond={t.should_respond} yield={t.should_yield}"
    )
    if vc.interruption_reason:
        print(f"[voice_conversation] interrupted={vc.interruption_reason}")
    if vc.continuity_hint:
        print(f"[voice_conversation] continuity={_trim(vc.continuity_hint, 140)}")


def _maybe_record_calibration(interrupt: bool, readiness: float, wait: bool) -> None:
    try:
        from .calibration import record_voice_calibration_hint

        record_voice_calibration_hint(
            interrupt=interrupt,
            readiness=readiness,
            wait=wait,
        )
    except Exception:
        pass


def apply_voice_conversation_to_perception_state(state: Any, g: dict | None) -> None:
    """Copy globals voice snapshot onto PerceptionState (safe defaults when missing)."""
    if g is None:
        _defaults_voice_fields(state)
        return
    vc = g.get("_voice_conversation")
    if vc is None:
        _defaults_voice_fields(state)
        return
    if not isinstance(vc, VoiceConversationResult):
        _defaults_voice_fields(state)
        return
    tm = vc.timing
    state.voice_turn_state = vc.turn_state
    state.voice_user_speaking = vc.user_speaking
    state.voice_assistant_speaking = vc.assistant_speaking
    state.voice_should_wait = tm.should_wait
    state.voice_should_respond = tm.should_respond
    state.voice_response_readiness = tm.response_readiness
    state.voice_interrupted = bool(vc.interruption_reason) or tm.should_interrupt
    state.voice_continuity_hint = vc.continuity_hint or ""
    state.voice_pacing_meta = {
        "silence_window_ms": tm.silence_window_ms,
        "should_yield": tm.should_yield,
        "interruption_reason": vc.interruption_reason,
        "pacing_notes": list(vc.pacing_notes),
        "timing_meta": dict(tm.meta),
        "voice_meta": dict(vc.meta),
    }


def _defaults_voice_fields(state: Any) -> None:
    state.voice_turn_state = IDLE
    state.voice_user_speaking = False
    state.voice_assistant_speaking = False
    state.voice_should_wait = False
    state.voice_should_respond = True
    state.voice_response_readiness = 0.5
    state.voice_interrupted = False
    state.voice_continuity_hint = ""
    state.voice_pacing_meta = {}


def default_voice_conversation_result() -> VoiceConversationResult:
    """Neutral snapshot for typing/tests."""
    return VoiceConversationResult(
        turn_state=IDLE,
        timing=VoiceTimingDecision(
            should_wait=False,
            should_respond=True,
            response_readiness=0.5,
        ),
    )
