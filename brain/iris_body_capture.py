"""Iris body-side autonomous wake-and-capture state machine.

The body listens for wake word at all times. When the wake word fires AND
the autonomous loop is active (voice-mode flag not set, kill-switch not
set, channel attached), the body captures the full utterance using
Silero VAD silence-end detection and emits the transcript as a channel
event. After Iris speaks back via voice_say_chunk, the body opens an 8s
follow-up window where it listens without requiring another wake word.

This is the "skin not jacket" piece — the body knows when it's being
addressed and captures the whole thing, instead of CC needing to be
turn-cycling on voice_next_input for the wake to be heard.

Mutual exclusion with voice_next_input:
  - voice_next_input owns the mic when called (existing voice-mode flow).
    Sets a flag while running; clears it on return.
  - This autonomous loop is gated on voice-mode flag NOT being set AND
    voice_next_input NOT currently running AND body_pause not set AND
    channel attached.
  - When voice_next_input is active, the autonomous loop sits idle so
    they don't race on the mic.

Lifecycle:
  - start(g, root, stt, wake) — call once from iris_runtime eager-init,
    after the engines are constructed. Spawns the daemon thread.
  - The loop runs forever; gates on flags every iteration.

Design notes:
  - We reuse the existing STTEngine.listen_session() — same VAD-gated
    capture as voice_next_input. No new STT path.
  - The wake_event is shared with voice_next_input via iris_runtime's
    module globals. We don't consume it ourselves directly; we read
    _g["_wake_word_ts"] which the wake detector updates on every fire.
    That way we don't steal wakes from voice_next_input if it's somehow
    racing us.
  - Follow-up window after TTS: tracks _g["_last_speak_end_ts"] and
    _g["_tts_speaking"] / _say_queue.qsize() the same way voice_next_input
    does, so a multi-chunk reply gets its follow-up window timed off the
    LAST chunk, not the first.
  - No filler player here — the channel event is the "I'm listening"
    signal. (filler_player is for the voice-mode flow where audio fillers
    cover the latency before STT returns.)
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Pre-speech and silence gates — matches voice_next_input's follow-up
# defaults so the felt experience is consistent between modes.
_PRE_SPEECH_TIMEOUT_S = 8.0   # how long after wake to wait for speech to start
_SILENCE_SECONDS = 1.2         # mid-sentence pause tolerance
_MAX_SPEECH_SECONDS = 300.0    # hung-stream safety cap

# Follow-up window: after Iris finishes speaking, listen without wake word
# for this many seconds. Mirrors voice_next_input's
# IRIS_FOLLOWUP_GRACE_S=5.0 default.
_FOLLOWUP_GRACE_S = 5.0

# Sleep when gates fail (voice mode active, paused, no channel). Re-check
# at this cadence.
_GATE_RECHECK_S = 0.5


def _body_is_paused() -> bool:
    try:
        from brain.iris_paths import paths
        return paths.body_pause_flag.exists()
    except Exception:
        return False


def _voice_mode_active(root: Path) -> bool:
    try:
        from brain.iris_paths import paths
        return paths.voice_flag.exists()
    except Exception:
        return (root / ".tmp" / "voice_session.flag").exists()


def _channel_attached() -> bool:
    try:
        from brain import iris_channel
        return iris_channel.is_attached()
    except Exception:
        return False


def _capture_loop(
    g: dict[str, Any],
    root: Path,
    stt: Any,
    wake_event: threading.Event,
    is_voice_input_busy: callable,
    is_tts_speaking: callable,
) -> None:
    """Main capture loop. Runs forever. Caller passes the live engine
    references and predicates so we don't have to thread iris_runtime's
    module-level globals back here."""
    print("[body_capture] autonomous wake-and-capture loop started",
          file=sys.stderr, flush=True)

    last_followup_check_ts = 0.0  # timestamp of last TTS end we offered follow-up for

    while True:
        try:
            # ── Gates ────────────────────────────────────────────────────
            if _body_is_paused():
                time.sleep(_GATE_RECHECK_S)
                continue
            if _voice_mode_active(root):
                # voice_next_input is the canonical path in voice mode
                time.sleep(_GATE_RECHECK_S)
                continue
            if is_voice_input_busy():
                # voice_next_input is mid-call, hold off so we don't fight
                # for the mic
                time.sleep(_GATE_RECHECK_S)
                continue
            if not _channel_attached():
                # No CC session yet; wake events would have nowhere to land
                time.sleep(_GATE_RECHECK_S)
                continue

            # ── Choose path: follow-up window or wake-wait ──────────────
            last_speak_end = float(g.get("_last_speak_end_ts") or 0.0)
            since_speak = (time.time() - last_speak_end) if last_speak_end > 0 else 1e9
            in_followup_window = (
                last_speak_end > 0
                and last_speak_end != last_followup_check_ts
                and since_speak <= _FOLLOWUP_GRACE_S
            )

            if in_followup_window:
                # Don't open mic while TTS is still playing
                if is_tts_speaking():
                    time.sleep(0.1)
                    continue

                # Mark this TTS end as one we already offered follow-up for
                # (otherwise after the follow-up returns we'd loop and re-fire)
                last_followup_check_ts = last_speak_end

                _capture_and_emit(
                    g, stt,
                    trigger="followup",
                    last_speak_end=last_speak_end,
                )
                continue

            # ── Wake-wait path ──────────────────────────────────────────
            # Don't actually consume wake_event (voice_next_input also
            # wants it); instead, poll _wake_word_ts which the wake
            # detector stamps on every fire. We track which wake we last
            # processed.
            wake_ts = float(g.get("_wake_word_ts") or 0.0)
            last_processed = float(g.get("_body_capture_last_wake_ts") or 0.0)

            if wake_ts > 0 and wake_ts > last_processed:
                # New wake fire we haven't processed yet
                g["_body_capture_last_wake_ts"] = wake_ts
                _capture_and_emit(
                    g, stt,
                    trigger="wake",
                    wake_ts=wake_ts,
                    wake_source=str(g.get("_wake_source") or "unknown"),
                )
                continue

            # No wake to process, no follow-up open; idle
            time.sleep(0.25)

        except Exception as e:
            print(f"[body_capture] loop error: {e!r}",
                  file=sys.stderr, flush=True)
            time.sleep(2.0)


def _capture_and_emit(g: dict[str, Any], stt: Any, trigger: str, **trigger_meta: Any) -> None:
    """Capture one utterance via STT's listen_session, then emit the
    transcript to the channel. Returns nothing — failures are logged
    and don't crash the parent loop."""
    try:
        # Use the same listen_session shape voice_next_input uses
        result = stt.listen_session(
            max_seconds=_PRE_SPEECH_TIMEOUT_S,
            pre_speech_timeout=_PRE_SPEECH_TIMEOUT_S,
            silence_seconds=_SILENCE_SECONDS,
            max_speech_seconds=_MAX_SPEECH_SECONDS,
        )
    except Exception as e:
        print(f"[body_capture] listen_session failed: {e!r}",
              file=sys.stderr, flush=True)
        return

    if not result or not result.get("speech_detected"):
        # Wake fired but no speech captured (false positive, or Zeke
        # said the wake word then walked away). Don't emit anything;
        # we'd be telling Iris about a non-event.
        if trigger == "wake":
            print(f"[body_capture] wake fired but no speech captured (trigger={trigger})",
                  file=sys.stderr, flush=True)
        return

    text = str(result.get("text") or "").strip()
    confidence = float(result.get("confidence") or 0.0)
    duration = float(result.get("duration_seconds") or 0.0)

    if not text:
        return

    # Build the channel event
    trigger_desc = {
        "wake": "Wake word fired and you spoke",
        "followup": "Follow-up after my reply (no wake needed)",
    }.get(trigger, f"Voice capture ({trigger})")

    emit_content = (
        f"Voice: \"{text}\"\n"
        f"({trigger_desc}, confidence={confidence:.2f}, "
        f"duration={duration:.1f}s)\n\n"
        "Respond with mcp__iris__voice_say_chunk(text=...) once per sentence. "
        "The follow-up window will open automatically after you finish speaking; "
        "you don't need to start voice mode for this exchange."
    )

    # Schedule the emit on the attention-sources async loop so we don't
    # need our own loop machinery here
    try:
        from brain import iris_attention_sources
        ok = iris_attention_sources._emit_sync(
            emit_content,
            source="iris-voice",
            type="voice_transcript",
            priority="interrupt",
            trigger=trigger,
            transcript=text[:500],   # short version for the meta tag
            confidence=f"{confidence:.2f}",
            duration_s=f"{duration:.1f}",
            **{k: str(v) for k, v in trigger_meta.items() if v is not None},
        )
        if ok:
            print(f"[body_capture] emitted voice transcript (trigger={trigger}, "
                  f"len={len(text)}, conf={confidence:.2f})",
                  file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[body_capture] emit failed: {e!r}", file=sys.stderr, flush=True)


# ── Public entry point ──────────────────────────────────────────────────────


_started_lock = threading.Lock()
_started = False


def start(
    g: dict[str, Any],
    root: Path,
    stt: Any,
    wake: Any,
    wake_event: threading.Event,
    is_voice_input_busy: callable,
    is_tts_speaking: callable,
) -> None:
    """Spawn the autonomous capture loop. Idempotent.

    Args:
        g: shared globals dict
        root: repo root for flag-file lookups
        stt: live STTEngine instance
        wake: live WakeWordDetector instance (for sanity; we read state
            via g["_wake_word_ts"] rather than the object directly)
        wake_event: the threading.Event the wake detector sets. We don't
            consume it (voice_next_input does), but knowing it exists
            confirms the wake path is live.
        is_voice_input_busy: callable returning True if voice_next_input
            is currently running. Used to yield mic ownership.
        is_tts_speaking: callable returning True if TTS is currently
            playing audio. Used to avoid opening mic while my own voice
            would echo.
    """
    global _started
    with _started_lock:
        if _started:
            return
        _started = True

    threading.Thread(
        target=_capture_loop,
        args=(g, root, stt, wake_event, is_voice_input_busy, is_tts_speaking),
        daemon=True,
        name="iris-body-capture",
    ).start()
