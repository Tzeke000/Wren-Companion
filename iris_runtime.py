"""Iris runtime — MCP server holding voice (and later: state, heartbeat, orb HTTP).

Architecture (per C:\\Users\\Owner\\.claude\\plans\\make-sure-i-m-giving-goofy-island.md):
  Single Python process. Exposes itself to Iris's Claude Code session via MCP
  over stdio. Engines (TTS / STT / wake word) live in this process; they keep
  their existing thread-based internals untouched.

This file currently implements steps 1-2 of the build order:
  1. voice.speak(text, emotion, intensity)        — TTS works end-to-end
  2. voice.next_input(timeout)                    — long-poll wake → STT → transcript

Future steps (heartbeat tick, context-delta, HTTP shim for the Tauri orb)
add to this same file or pull into iris_mcp/ once the surface grows.

Run standalone for smoke-test:
    .venv\\Scripts\\python.exe iris_runtime.py
(MCP server boots, waits on stdin for JSON-RPC frames; Ctrl-C to exit.)

Wired into Claude Code via .mcp.json at repo root.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Route every bare print() in imported brain.* modules to stderr. FastMCP
# owns sys.stdout for JSON-RPC frames once mcp.run() starts; stray writes
# either corrupt the protocol or block engine init threads on MCP's stdout
# lock (observed: TTSWorker/STTEngine init hung indefinitely with
# engine="none" because their `[tts_worker] loading Kokoro...` prints
# couldn't drain). Stderr is captured by Claude Code's MCP server log
# and never collides with the protocol stream.
import builtins as _builtins
_real_print = _builtins.print
def _print_to_stderr(*args, **kwargs):
    if "file" not in kwargs:
        kwargs["file"] = sys.stderr
    return _real_print(*args, **kwargs)
_builtins.print = _print_to_stderr

# Wake-word config: until hey_iris.onnx is trained, fall back to the bundled
# hey_jarvis proxy so we have a working trigger.
os.environ.setdefault("AVA_USE_HEY_JARVIS_PROXY", "1")

# TTS config: prefer Kokoro CUDA on this machine (RTX 3060 sm_86 — works with
# cu128 torch already installed). Falls back to Piper (kathleen — distinct
# from Ava's lessac and Wren's amy) if Kokoro init fails.
os.environ.setdefault("AVA_TTS_ENGINE", "kokoro")
os.environ.setdefault("AVA_KOKORO_VOICE_DEFAULT", "af_bella")
os.environ.setdefault("AVA_PIPER_VOICE", "en_US-kathleen-low")

# Audio device routing. The "speakers" keyword in tts_worker resolves to
# "Speakers (Realtek HD Audio output)" via the WDM-KS host API, which
# PortAudio can't open in blocking mode on this machine ("Blocking API not
# supported yet"). "auto" uses the Windows system default output (the
# user's actively-selected device, e.g. Logitech PRO X Gaming Headset on MME)
# which DOES support blocking I/O. Plus the VB-CABLE for Voicemeeter routing.
os.environ.setdefault("AVA_TTS_DEVICES", "auto,cable")

# STT: pin distil-large-v3 (~6× faster than large-v3-turbo at ~1% WER cost).
# First-word latency matters more than tail accuracy in voice conversation,
# and the fallback chain in stt_engine.py still drops to turbo/medium/base
# if distil fails to load.
os.environ.setdefault("AVA_STT_MODEL", "distil-large-v3")

# HuggingFace model cache — point at D:\ because C:\ is nearly full on this
# machine (35 MB free at last check). distil-large-v3 alone is ~1.5 GB.
# Set BEFORE faster_whisper or any HF library imports below.
os.environ.setdefault("HF_HOME", r"D:\Wren-Companion\.cache\huggingface")

# Pre-import all heavyweight modules brain.* engines defer to inside their
# init methods (kokoro, piper, sounddevice, faster_whisper, openwakeword,
# silero_vad, torch, pyaudio). These imports must complete in the main
# thread BEFORE FastMCP starts and BEFORE the eager-init thread spawns —
# otherwise concurrent imports across threads deadlock on Python's _imp
# lock, hanging engine init silently with engine="none". Verified via
# .tmp/iris_no_mcp_test.py: same code paths work when these imports are
# resolved at module-level instead of inside worker threads.
print("[iris_runtime] pre-loading engine deps (~5-10s)...", file=sys.stderr, flush=True)
import numpy  # noqa: F401
import sounddevice  # noqa: F401
import torch  # noqa: F401
import kokoro  # noqa: F401
try:
    import piper  # noqa: F401
except Exception as _e:
    print(f"[iris_runtime] piper unavailable (non-fatal): {_e!r}", file=sys.stderr, flush=True)
import faster_whisper  # noqa: F401
import silero_vad  # noqa: F401
import openwakeword  # noqa: F401
import openwakeword.model  # noqa: F401
# Phase 1 vision deps — pre-imported so the video capture thread doesn't race
# FastMCP startup on Python's _imp lock (same deadlock pattern as the audio
# engines documented in fastmcp_import_deadlock memory).
try:
    import onnxruntime  # noqa: F401
    import insightface  # noqa: F401
    import insightface.app  # noqa: F401
except Exception as _e:
    print(f"[iris_runtime] insightface unavailable (vision phase 1 will be skipped): {_e!r}", file=sys.stderr, flush=True)
print("[iris_runtime] engine deps loaded", file=sys.stderr, flush=True)

from mcp.server.fastmcp import FastMCP

from brain.tts_worker import TTSWorker
from brain.stt_engine import STTEngine
from brain.wake_word import WakeWordDetector


# ── Shared globals dict (mirrors avaagent.py's `g`) ───────────────────────────
# The existing engines read/write a few well-known keys here. We keep the
# contract identical so wake_word.py / tts_worker.py / camera_annotator work
# without edits — Iris is grafting Ava's perception modules onto her own
# runtime by populating the same globals avaagent.py would.
_g: dict[str, Any] = {
    "_tts_speaking": False,
    "_tts_amplitude": 0.0,
    "_last_speak_end_ts": 0.0,
    "_wake_word_detected": False,
    "_wake_word_ts": 0.0,
    "_wake_source": None,
    "_wake_source_ts": 0.0,
    "input_muted": False,
    # Phase 1 perception state — populated by the video capture thread when
    # InsightFace is available. Shape mirrors avaagent's expectations so
    # camera_annotator.annotate_frame() and face_tracking.update() work
    # without modification.
    "BASE_DIR": ROOT,
    "_insight_face": None,
    "_face_results": None,
    "_recognized_person_id": "unknown",
    "_recognized_confidence": 0.0,
    "_recognized_age": 0,
    "_face_age": 0,
    "_recognized_gender": "?",
    "_face_gender": "?",
    "_attention_state": None,
    "_current_expression": "",
    "_signal_bus": None,
    "_expression_calibrator": None,
    "_video_memory": None,
    "_expression_detector": None,
    "_eye_tracker": None,
    "_profiles": {},
    "camera_manager": None,
    # Phase 1.5 face enrollment — set by enroll_face() MCP tool, consumed by
    # the video capture loop. Saves raw pre-annotation frames to faces/<pid>/
    # then triggers engine.update_known_faces() for hot-reload (no restart).
    "_enroll_request": None,
    "_enroll_result": None,
}

# Wake event: WakeWordDetector callback sets this; voice.next_input awaits it.
_wake_event = threading.Event()

def _on_wake() -> None:
    _wake_event.set()


# ── Engine init (lazy, single-shot) ───────────────────────────────────────────
_tts: TTSWorker | None = None
_stt: STTEngine | None = None
_wake: WakeWordDetector | None = None
_init_lock = threading.Lock()


def _ensure_tts() -> TTSWorker:
    global _tts
    with _init_lock:
        if _tts is None:
            print("[iris_runtime] booting TTSWorker (Kokoro init ~5-8s)...", file=sys.stderr, flush=True)
            _tts = TTSWorker(g=_g)
            print(f"[iris_runtime] TTSWorker ready (engine={_tts._engine_type})", file=sys.stderr, flush=True)
    return _tts


def _ensure_stt() -> STTEngine:
    global _stt
    with _init_lock:
        if _stt is None:
            print("[iris_runtime] booting STTEngine (Whisper Large-v3 Turbo)...", file=sys.stderr, flush=True)
            _stt = STTEngine()
            print(f"[iris_runtime] STTEngine ready (backend={_stt.backend_name()})", file=sys.stderr, flush=True)
    return _stt


def _ensure_wake() -> WakeWordDetector:
    global _wake
    with _init_lock:
        if _wake is None:
            print("[iris_runtime] booting WakeWordDetector (hey_jarvis proxy)...", file=sys.stderr, flush=True)
            _wake = WakeWordDetector(g=_g, on_wake=_on_wake, base_dir=ROOT)
            _wake.start()
            print(f"[iris_runtime] WakeWordDetector started (backend={_wake.backend})", file=sys.stderr, flush=True)
    return _wake


# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP("iris")

# Channel capability — declares `experimental['claude/channel']` so this
# MCP server can push events into the running CC session. See
# brain/iris_channel.py for details and the underlying private-API caveat.
# Must be applied BEFORE mcp.run() so the initialize response advertises it.
try:
    from brain import iris_channel as _iris_channel
    _iris_channel.apply_channel_capability(mcp)
    # Rate-limit defaults — keep noisy sources from drowning quiet ones.
    # Voice/chat/sibling are user-driven (sparse by nature), no limit.
    _iris_channel.configure_rate_limit("iris-camera", 30.0)   # face-state changes max 1/30s
    _iris_channel.configure_rate_limit("iris-mood", 60.0)     # mood salience max 1/min
    _iris_channel.configure_rate_limit("iris-time", 300.0)    # time-orient max 1/5min
except Exception as _ce:
    print(f"[iris_runtime] channel capability setup failed (non-fatal — Stop hook fallback still works): {_ce!r}", file=sys.stderr, flush=True)


@mcp.tool()
def voice_speak(text: str, emotion: str = "neutral", intensity: float = 0.5) -> dict:
    """Speak text aloud through Iris's TTS (Kokoro CUDA preferred, pyttsx3 fallback).

    The call blocks until audio playback finishes, so returning means Iris
    actually said it. Picks voice/speed from emotion + intensity.

    Args:
        text: What to say.
        emotion: Label like calm, joy, curiosity, frustration, sadness. Defaults neutral.
        intensity: 0.0..1.0 — strength of emotional modulation. Defaults 0.5.

    Returns:
        {ok, spoke_ms, engine}
    """
    tts = _ensure_tts()
    if not tts.is_available():
        return {"ok": False, "error": "tts not available", "engine": tts._engine_type}
    t0 = time.time()
    tts.speak_with_emotion(text, emotion, intensity, blocking=True)
    elapsed = time.time() - t0
    return {"ok": True, "spoke_ms": int(elapsed * 1000), "engine": tts._engine_type}


@mcp.tool()
def voice_next_input(timeout: float = 300.0) -> dict:
    """Long-poll wait for the next voice input. Blocks until wake word fires
    and a follow-up utterance is captured, or until timeout.

    The intended usage: Iris's Claude Code session calls this in a loop. Each
    return is one voice exchange. While the call is blocked, Iris is "listening"
    — that's the steady state.

    Args:
        timeout: Max seconds to wait for wake word. If exceeded, returns
                 timed_out=True so Iris can re-poll without dropping the loop.

    Returns:
        {ok, timed_out, transcript, confidence, wake_source, wake_ts, note?}
    """
    _ensure_wake()
    stt = _ensure_stt()

    # Phase 23: voice_next_input is the most-fired tool in voice mode.
    # Marking session attached here keeps the time substrate honest about
    # whether a CC session is actually here, vs the body running solo.
    try:
        from brain import iris_time
        iris_time.mark_session_attached()
    except Exception:
        pass

    # Follow-up window: if voice flag is set AND I (Iris) just finished
    # speaking within the last few seconds, hold the mic open without
    # requiring a wake word. This makes back-and-forth conversation
    # natural — Zeke speaks → I reply → he replies → I reply, no "hey
    # jarvis" needed between turns. The window is bounded so we can't
    # get stuck listening forever; if no speech is captured, fall through
    # to the regular wake-word wait.
    # Follow-up gates (after I just finished speaking):
    #   pre_speech_timeout — how long to wait for Zeke to start talking. If he
    #     doesn't say anything within this window, fall back to wake-word wait.
    #   silence_seconds — once he starts, how much mid-sentence pause we
    #     tolerate before deciding the turn is done.
    #   max_speech_seconds — safety upper bound on a single turn (5 min default).
    follow_up_pre_timeout_s = float(os.environ.get("IRIS_FOLLOWUP_PRE_TIMEOUT_S", "8.0"))
    follow_up_speech_grace_s = float(os.environ.get("IRIS_FOLLOWUP_GRACE_S", "5.0"))
    try:
        voice_flag_set = (ROOT / ".tmp" / "voice_session.flag").exists()
    except Exception:
        voice_flag_set = False
    # If TTS is still speaking or the chunk queue isn't empty, block on the
    # queue draining before opening the mic. Otherwise the mic captures my
    # own voice through the speakers and transcribes Iris-as-user.
    try:
        while _say_queue.qsize() > 0 or bool(_g.get("_tts_speaking")):
            time.sleep(0.05)
    except Exception:
        pass
    last_speak_end = float(_g.get("_last_speak_end_ts") or 0.0)
    since_speak = time.time() - last_speak_end if last_speak_end > 0 else 1e9
    try_followup = voice_flag_set and last_speak_end > 0 and since_speak <= follow_up_speech_grace_s

    if try_followup:
        # Open mic immediately with the wake-word path bypassed.
        #
        # Pre-speech: wait up to follow_up_pre_timeout_s for Zeke to start. If
        #   he doesn't, fall through to the regular wake-word path.
        # Post-speech: once he starts, the *only* gate is silence_seconds of
        #   quiet — he can talk as long as he wants, with mid-sentence pauses
        #   tolerated up to that threshold. No "you've been talking too long"
        #   cutoff; max_speech_seconds is a hung-stream safety net only.
        followup_silence_s = float(os.environ.get("IRIS_FOLLOWUP_SILENCE_S", "1.2"))
        try:
            fu_result = stt.listen_session(
                max_seconds=follow_up_pre_timeout_s,           # back-compat shim
                pre_speech_timeout=follow_up_pre_timeout_s,    # the real pre-gate
                silence_seconds=followup_silence_s,
                max_speech_seconds=300.0,
            )
        except Exception as _fe:
            print(f"[voice_next_input] follow-up listen error: {_fe!r}", file=sys.stderr, flush=True)
            fu_result = None
        if fu_result is not None and fu_result.get("speech_detected"):
            # Filler cover for the follow-up path too.
            try:
                from brain import filler_player as _fp
                _fp.maybe_play(fu_result.get("text") or "")
            except Exception as _fe:
                print(f"[voice_next_input] filler error (followup): {_fe!r}", file=sys.stderr, flush=True)
            # Append to shared transcript like the wake-word path does.
            try:
                from brain import iris_transcript
                iris_transcript.append(
                    role="user",
                    content=str(fu_result.get("text") or ""),
                    source="zeke",
                    modality="voice",
                )
            except Exception:
                pass
            return {
                "ok": True,
                "timed_out": False,
                "transcript": fu_result.get("text"),
                "confidence": float(fu_result.get("confidence") or 0.0),
                "duration_seconds": float(fu_result.get("duration_seconds") or 0.0),
                "wake_source": "followup",
                "wake_ts": time.time(),
            }
        # No speech in the follow-up window — fall through to wake-word wait.

    _wake_event.clear()
    fired = _wake_event.wait(timeout=timeout)
    if not fired:
        return {"ok": True, "timed_out": True, "transcript": None}

    wake_source = _g.get("_wake_source")
    wake_ts = float(_g.get("_wake_word_ts") or 0.0)

    # Wake fired — open mic, capture utterance via Silero-VAD-gated session.
    # max_seconds is a safety upper bound — Silero VAD's silence_seconds is the
    # actual gate. Bumped 12s -> 60s after Zeke kept hitting the cap mid-sentence
    # (2026-05-09 voice test). silence_seconds dropped 2.0 -> 0.8 to shave ~1.2s
    # off every reply's perceived latency. Per Zeke's spec: listen as long as
    # he's speaking, not on a fixed timer.
    #
    # Lever 4: if AVA_STT_STREAMING=1, use WhisperLiveKit incremental decoder
    # so transcript starts emerging while Zeke is still speaking. Falls back
    # to single-shot listen_session() on any failure (returns None).
    result = None
    if os.environ.get("AVA_STT_STREAMING", "").strip() == "1":
        try:
            result = stt.listen_session_streaming(max_seconds=60.0, silence_seconds=0.8)
            if result is None:
                print("[voice_next_input] streaming returned None — falling back to single-shot", file=sys.stderr, flush=True)
        except Exception as _se:
            print(f"[voice_next_input] streaming error: {_se!r} — falling back", file=sys.stderr, flush=True)
            result = None
    if result is None:
        # Wake fired — open mic. Pre-speech gate is generous (wake just fired
        # so we expect Zeke to start talking) but bounded; once speech starts
        # the only end-of-turn signal is silence_seconds of quiet (1.2s, same
        # as follow-up path so the conversation has consistent rhythm). No
        # hard cap on how long Zeke can talk; max_speech_seconds=300 is just
        # a safety net for hung streams.
        wake_pre_timeout = float(os.environ.get("IRIS_WAKE_PRE_TIMEOUT_S", "10.0"))
        wake_silence_s = float(os.environ.get("IRIS_WAKE_SILENCE_S", "1.2"))
        result = stt.listen_session(
            max_seconds=wake_pre_timeout,
            pre_speech_timeout=wake_pre_timeout,
            silence_seconds=wake_silence_s,
            max_speech_seconds=300.0,
        )

    # Filler audio cover — fires the moment STT finishes, before this tool
    # call returns to Claude Code. Audio plays during CC's turn-startup +
    # Claude inference, masking the perceived gap with humanlike disfluency.
    # Non-blocking: sounddevice.play() returns immediately after queueing.
    if result is not None and result.get("speech_detected"):
        try:
            from brain import filler_player as _fp
            _fp.maybe_play(result.get("text") or "")
        except Exception as _fe:
            print(f"[voice_next_input] filler error: {_fe!r}", file=sys.stderr, flush=True)
    if result is None or not result.get("speech_detected"):
        return {
            "ok": True,
            "timed_out": False,
            "transcript": None,
            "wake_source": wake_source,
            "wake_ts": wake_ts,
            "note": "wake fired but no speech captured",
        }

    # Phase 4 — append the captured user utterance to the shared transcript so
    # the orb's chat history view shows voice turns alongside chat turns.
    try:
        from brain import iris_transcript
        iris_transcript.append(
            role="user",
            content=str(result.get("text") or ""),
            source="zeke",
            modality="voice",
        )
    except Exception:
        pass

    # Phase 30: surface for /api/v1/stt/result orb endpoint
    _g["_last_stt_result"] = {
        "ts": time.time(),
        "text": str(result.get("text") or ""),
        "confidence": float(result.get("confidence") or 0.0),
        "duration_seconds": float(result.get("duration_seconds") or 0.0),
    }
    # Phase 31: enqueue for batched fact extraction.
    try:
        from brain import iris_extraction_queue
        user_text = str(result.get("text") or "")
        if user_text and len(user_text.strip()) >= 4:
            iris_extraction_queue.enqueue(user_text, person_id="zeke", modality="voice")
    except Exception:
        pass
    # Phase 45: push the user utterance into working memory.
    try:
        from brain import iris_human_memory
        user_text = str(result.get("text") or "")
        if user_text:
            iris_human_memory.working_memory_push({
                "kind": "user_voice",
                "content": user_text[:300],
                "meta": {"confidence": float(result.get("confidence") or 0.0)},
            })
    except Exception:
        pass

    return {
        "ok": True,
        "timed_out": False,
        "transcript": result.get("text"),
        "confidence": float(result.get("confidence") or 0.0),
        "duration_seconds": float(result.get("duration_seconds") or 0.0),
        "wake_source": wake_source,
        "wake_ts": wake_ts,
    }


@mcp.tool()
def voice_status(ctx: _IrisContext = None) -> dict:
    """Report which engines are loaded and what backends they're using.

    Useful for Iris (and the orb) to know whether the body is fully online.
    """
    if ctx is not None:
        _record_session_safe(ctx)
    try:
        from brain import iris_time
        iris_time.mark_session_attached()
    except Exception:
        pass
    return {
        "tts": {
            "loaded": _tts is not None,
            "engine": _tts._engine_type if _tts else None,
            "available": _tts.is_available() if _tts else False,
            "speaking": bool(_g.get("_tts_speaking")),
        },
        "stt": {
            "loaded": _stt is not None,
            "backend": _stt.backend_name() if _stt else None,
            "available": _stt.is_available() if _stt else False,
        },
        "wake": {
            "loaded": _wake is not None,
            "backend": _wake.backend if _wake else None,
            "available": _wake.available if _wake else False,
            "last_wake_ts": float(_g.get("_wake_word_ts") or 0.0),
            "last_source": _g.get("_wake_source"),
        },
    }


# ── Channel surface ──────────────────────────────────────────────────────────
# Channel events are pushed via brain/iris_channel.emit(). Most callers don't
# have a Context; they need the session pre-recorded. The helper below grabs
# the session from a tool's Context arg and stashes it for later emit() calls.
# We attach this to a few high-frequency tools so the session is always
# captured early.

from mcp.server.fastmcp import Context as _IrisContext


def _record_session_safe(ctx: _IrisContext) -> None:
    """Best-effort: pull the live ServerSession from ctx and stash it via
    iris_channel.record_session(). Called from a handful of tools so the
    session is captured no matter what Iris calls first. Idempotent — safe
    to call from every tool."""
    try:
        from brain import iris_channel
        if not iris_channel.is_attached():
            session = ctx.request_context.session
            iris_channel.record_session(session)
    except Exception as _se:
        # Don't propagate — channel is best-effort, never break the tool call.
        pass


@mcp.tool()
async def channel_test(content: str = "iris-channel-smoke-test", priority: str = "ambient", ctx: _IrisContext = None) -> dict:
    """Smoke-test the attention channel: emit one event and report whether it
    went out. Use to verify the channel path is alive before relying on it
    for real events.

    Behavior:
      1. Records the current ServerSession (idempotent) so async emitters
         elsewhere in the process can reach the write stream.
      2. Emits one notification with source="iris" and the given content.
      3. Returns {ok, sent, attached, note}. `sent=true` means the JSON-RPC
         frame was written to the transport — NOT that Claude saw it
         (channel events queue and arrive on the next turn-boundary; if
         CC is in the middle of this turn, the event arrives next turn).

    Args:
        content: Body of the test event. Free text.
        priority: One of "interrupt" / "attend" / "ambient". Affects how
            future-me will respond when the event lands.

    Returns:
        {ok: bool, sent: bool, attached: bool, note: str}
    """
    if ctx is not None:
        _record_session_safe(ctx)
    from brain import iris_channel
    attached = iris_channel.is_attached()
    if not attached:
        return {
            "ok": False,
            "sent": False,
            "attached": False,
            "note": "no ServerSession recorded yet — pass ctx via FastMCP Context or call any other tool first",
        }
    sent = await iris_channel.emit(
        content,
        source="iris",
        type="smoke_test",
        priority=priority,
    )
    return {
        "ok": True,
        "sent": sent,
        "attached": True,
        "note": ("event written to transport — will arrive at me on the next "
                 "turn-boundary if CC is willing to receive" if sent else
                 "emit returned False — either rate-limited or write failed; check stderr"),
    }


# ── Body kill-switch ─────────────────────────────────────────────────────────
# Hard pause for the body's autonomous behaviors (wake-and-capture loop,
# autonomous channel emitters). Soft path: MCP tools below (I call them
# when I notice the body misbehaving). Hard path: PowerShell drop the
# flag file (works even if the MCP server itself is the thing misbehaving).
#
# Anything in the harness that runs autonomously and produces side effects
# should consult `paths.body_pause_flag.exists()` before acting.

@mcp.tool()
def voice_body_pause(reason: str = "manual pause") -> dict:
    """Hard-pause the body's autonomous behaviors (wake-capture loop,
    autonomous emitters). Wake word detector keeps running for diagnostics,
    but no audio is captured and no channel events fire from autonomous
    sources. Manual tool calls (voice_speak, etc.) still work.

    Use when you notice the body misbehaving: flooding the channel,
    capturing audio that shouldn't be captured, mis-firing wake events.

    Resume with voice_body_resume() or delete the flag file directly:
        Remove-Item D:\\Wren-Companion\\.tmp\\body_pause.flag

    Args:
        reason: Free text written into the flag file for diagnostics.
            Helps future-me figure out why the body was paused.

    Returns:
        {ok: bool, paused: bool, flag_path: str, reason: str}
    """
    try:
        from brain.iris_paths import paths
        flag = paths.body_pause_flag
        flag.parent.mkdir(parents=True, exist_ok=True)
        body = f"reason: {reason}\npaused_at: {time.time()}\npaused_by: voice_body_pause MCP tool\n"
        flag.write_text(body, encoding="utf-8")
        return {
            "ok": True,
            "paused": True,
            "flag_path": str(flag),
            "reason": reason,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def voice_body_resume() -> dict:
    """Resume the body's autonomous behaviors by removing the pause flag.
    No-op if the body wasn't paused.

    Returns:
        {ok: bool, was_paused: bool, flag_path: str}
    """
    try:
        from brain.iris_paths import paths
        flag = paths.body_pause_flag
        was_paused = flag.exists()
        if was_paused:
            flag.unlink()
        return {
            "ok": True,
            "was_paused": was_paused,
            "flag_path": str(flag),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def voice_body_status() -> dict:
    """Report whether the body is currently paused, and if so, why.

    Returns:
        {ok: bool, paused: bool, reason: str | None, flag_path: str}
    """
    try:
        from brain.iris_paths import paths
        flag = paths.body_pause_flag
        if not flag.exists():
            return {"ok": True, "paused": False, "reason": None, "flag_path": str(flag)}
        try:
            body = flag.read_text(encoding="utf-8")
        except Exception:
            body = "(could not read flag body)"
        return {
            "ok": True,
            "paused": True,
            "reason": body.strip(),
            "flag_path": str(flag),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _body_is_paused() -> bool:
    """Cheap inline check for the autonomous loops. Returns True if the
    pause flag exists. Does NOT raise — false on any error so the body
    doesn't get stuck paused on a transient filesystem issue."""
    try:
        from brain.iris_paths import paths
        return paths.body_pause_flag.exists()
    except Exception:
        return False


# ── Sentence-streaming TTS queue ──────────────────────────────────────────────
# voice_say_chunk(text) queues one chunk (typically a sentence) and returns
# instantly — non-blocking. A dedicated worker thread pulls from the queue and
# calls TTS speak_with_emotion(blocking=True) so chunks play sequentially in
# arrival order. The point: I emit one tool call per sentence as I generate.
# Kokoro starts speaking sentence 1 while I'm still generating sentence 2.
# Eliminates the ~5-10s "wait for full reply, then start speaking" stall that
# even the Stop-hook outbound path can't avoid (CC buffers full reply before
# any external program sees it).
import queue as _queue_mod
_say_queue: _queue_mod.Queue = _queue_mod.Queue()
_say_worker_started = False
_say_worker_lock = threading.Lock()


def _say_worker_loop() -> None:
    while True:
        try:
            text, emotion, intensity = _say_queue.get()
        except Exception:
            time.sleep(0.05)
            continue
        try:
            tts = _ensure_tts()
            if tts.is_available():
                tts.speak_with_emotion(text, emotion, intensity, blocking=True)
        except Exception as e:
            print(f"[say_chunk worker] error: {e!r}", file=sys.stderr, flush=True)
        # After each chunk finishes, refresh _last_speak_end_ts so that
        # voice_next_input's follow-up grace timer reflects the END of the
        # full multi-chunk reply, not the end of chunk #1. Without this,
        # Zeke speaking after a 5-chunk monologue would see grace already
        # expired and get routed to the wake-word path mid-conversation.
        try:
            _g["_last_speak_end_ts"] = time.time()
        except Exception:
            pass


def _ensure_say_worker() -> None:
    global _say_worker_started
    with _say_worker_lock:
        if not _say_worker_started:
            threading.Thread(
                target=_say_worker_loop,
                daemon=True,
                name="iris-say-chunk",
            ).start()
            _say_worker_started = True


@mcp.tool()
def voice_say_chunk(text: str, emotion: str = "neutral", intensity: float = 0.5) -> dict:
    """Queue ONE chunk (typically one sentence) to Kokoro for speech.

    NON-BLOCKING — returns instantly after queueing. Multiple chunks play in
    arrival order via a serialized worker. Use this in voice mode to stream
    your reply sentence-by-sentence: emit one tool call per sentence as you
    generate, and Kokoro starts speaking the first sentence while you're
    still generating the next.

    Args:
        text: One chunk. Typically one sentence — keep short for low first-
            word latency.
        emotion: calm | joy | curiosity | frustration | sadness | neutral.
        intensity: 0.0..1.0.

    Returns:
        {ok, queue_depth} — queue_depth is the number of chunks ahead of
        this one (0 means this chunk speaks immediately).
    """
    if not text or not text.strip():
        return {"ok": False, "error": "empty text"}
    # Phase 41: voice chunks count as session activity.
    try:
        from brain import iris_time
        iris_time.mark_session_attached()
    except Exception:
        pass
    _ensure_say_worker()
    depth_before = _say_queue.qsize()
    _say_queue.put((text, emotion, intensity))
    # Phase 4 — log this chunk to the shared transcript. The orb groups by
    # adjacent role+source so multi-chunk replies render as one bubble.
    try:
        from brain import iris_transcript
        iris_transcript.append(
            role="assistant",
            content=text,
            source="iris",
            modality="voice",
        )
    except Exception:
        pass
    # Per-chunk theory-of-mind tracking. Each chunk may introduce new topics;
    # cheap regex match, no LLM.
    try:
        from brain import theory_of_mind
        theory_of_mind.post_turn_record("zeke", text)
    except Exception:
        pass
    # Phase 26: real interaction nudges mood toward engagement. Tiny bump
    # per chunk so multi-sentence replies don't compound. Also fires a
    # signal-bus event so heartbeat / consolidation see real activity.
    try:
        from brain import mood_core
        m = mood_core.load_mood_raw()
        weights = dict(m.get("emotion_weights") or mood_core.DEFAULT_EMOTIONS)
        weights["interest"] = min(1.0, weights.get("interest", 0.13) + 0.005)
        weights["calmness"] = max(0.0, weights.get("calmness", 0.24) - 0.002)
        m["emotion_weights"] = mood_core.normalize_emotions(weights)
        mood_core.save_mood_raw(m)
    except Exception:
        pass
    try:
        bus = _g.get("_signal_bus")
        if bus is not None:
            bus.fire("voice_chunk_spoken",
                     data={"chars": len(text), "queue_depth": depth_before},
                     priority="low")
    except Exception:
        pass
    return {"ok": True, "queue_depth": depth_before}


@mcp.tool()
def llm_reply(request_id: str, text: str) -> dict:
    """Answer a pending LLM request from a brain/* module (Phase 9).

    When any brain/* module calls brain.iris_llm.ask_iris(prompt, kind=...),
    it submits a request file and blocks. The Stop hook detects the pending
    flag and rewakes me with the prompt + kind. I generate a response and
    pass it here. The caller's wait_for_reply() unblocks.

    The kind tells me what shape of reply is expected (extract_facts wants
    one fact per line, classify_intent wants just the intent name, etc.).
    The rewake message includes the kind + prompt + requester so I know
    what to produce.

    Args:
        request_id: From the rewake system-reminder.
        text: My reply, formatted per the kind's contract.

    Returns:
        {ok, request_id, kind} on success.
    """
    if not request_id or not str(request_id).strip():
        return {"ok": False, "error": "empty request_id"}
    if text is None:
        text = ""
    # Phase 23: llm_reply means a brain/* module asked me something and I
    # answered — counts as session activity.
    try:
        from brain import iris_time
        iris_time.mark_session_attached()
    except Exception:
        pass
    try:
        from brain import iris_llm
        req = iris_llm.get(str(request_id))
        if req is None:
            return {"ok": False, "error": "request not found"}
        if req.get("status") == "answered":
            return {"ok": False, "error": "request already answered"}
        ok = iris_llm.mark_answered(str(request_id), str(text))
        if not ok:
            return {"ok": False, "error": "mark_answered failed"}
        return {"ok": True, "request_id": str(request_id), "kind": req.get("kind")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def chat_reply(request_id: str, text: str) -> dict:
    """Answer a pending chat request from the orb (Phase 4).

    The orb's POST /api/v1/chat is long-polling on disk for the response file
    to flip to status=answered. Calling this tool writes that file and unblocks
    the HTTP request. Also appends the user message and the reply to the
    shared transcript (state/transcript.jsonl) so the orb's history view
    stays unified across voice and chat modalities.

    Use this when a system-reminder tells you a chat request is pending —
    the rewake message includes the request_id and user_text. Generate one
    response and pass it here as plain text (no markdown ceremony — the orb
    renders monospace).

    Args:
        request_id: From the rewake system-reminder.
        text: Your full reply.

    Returns:
        {ok, request_id} on success, {ok: False, error} if the request was
        not found or already answered.
    """
    # Phase 23: chat replies count as session activity.
    try:
        from brain import iris_time
        iris_time.mark_session_attached()
    except Exception:
        pass
    if not request_id or not str(request_id).strip():
        return {"ok": False, "error": "empty request_id"}
    if not text or not str(text).strip():
        return {"ok": False, "error": "empty text"}
    try:
        from brain import iris_chat as _ic
        from brain import iris_transcript as _it
        # If the request still has pending status, log the user side first
        # (we may not have logged it earlier — submit() doesn't write to the
        # transcript, so chat turns appear together when iris answers).
        req = _ic.get(str(request_id))
        if req and req.get("status") == "pending":
            user_text = str(req.get("user_text") or "")
            if user_text:
                _it.append(role="user", content=user_text,
                           source="zeke", modality="chat")
        ok = _ic.mark_answered(str(request_id), str(text))
        if not ok:
            return {"ok": False, "error": "request not found or already answered"}
        _it.append(role="assistant", content=str(text),
                   source="iris", modality="chat")

        # Post-turn hooks — feed signals to per-person modules. No LLM here;
        # both are pattern-match cheap.
        person_id = "zeke"  # default for now; multi-person later
        try:
            from brain import theory_of_mind
            theory_of_mind.post_turn_record(person_id, str(text))
        except Exception:
            pass
        try:
            from brain import preference_learning
            user_msg = str((req or {}).get("user_text") or "")
            if user_msg:
                signals = preference_learning.detect_preference_signals(user_msg)
                if signals:
                    print(f"[post_turn] detected {len(signals)} preference signal(s)", file=sys.stderr, flush=True)
        except Exception:
            pass
        # Phase 31: enqueue user turn for batched fact extraction at next
        # inner_monologue tick. Cheap append; no LLM call here.
        user_msg = str((req or {}).get("user_text") or "")
        try:
            from brain import iris_extraction_queue
            if user_msg:
                iris_extraction_queue.enqueue(user_msg, person_id="zeke", modality="chat")
        except Exception:
            pass
        # Phase 45: push into working memory (user msg + my reply).
        try:
            from brain import iris_human_memory
            if user_msg:
                iris_human_memory.working_memory_push({
                    "kind": "user_chat",
                    "content": user_msg[:300],
                    "meta": {},
                })
            iris_human_memory.working_memory_push({
                "kind": "iris_reply",
                "content": str(text)[:300],
                "meta": {"modality": "chat"},
            })
        except Exception:
            pass
        # Phase 44: auto-detect anchor moment from this turn (regex, no LLM).
        try:
            from brain import anchor_moments
            user_msg = str((req or {}).get("user_text") or "")
            if user_msg:
                anchor_id = anchor_moments.auto_detect_anchor_in_turn(
                    "zeke", user_msg, str(text),
                )
                if anchor_id:
                    print(f"[chat_reply] auto-marked anchor: {anchor_id}",
                          file=sys.stderr, flush=True)
                    try:
                        bus = _g.get("_signal_bus")
                        if bus is not None:
                            bus.fire("anchor_marked",
                                     data={"anchor_id": anchor_id, "kind": "auto"},
                                     priority="medium")
                    except Exception:
                        pass
        except Exception:
            pass
        # Phase 26: chat reply nudges mood toward engagement + fires signal.
        try:
            from brain import mood_core
            m = mood_core.load_mood_raw()
            weights = dict(m.get("emotion_weights") or mood_core.DEFAULT_EMOTIONS)
            weights["interest"] = min(1.0, weights.get("interest", 0.13) + 0.01)
            weights["calmness"] = max(0.0, weights.get("calmness", 0.24) - 0.003)
            m["emotion_weights"] = mood_core.normalize_emotions(weights)
            mood_core.save_mood_raw(m)
        except Exception:
            pass
        try:
            bus = _g.get("_signal_bus")
            if bus is not None:
                bus.fire("chat_reply_sent",
                         data={"chars": len(text)},
                         priority="medium")
        except Exception:
            pass

        return {"ok": True, "request_id": str(request_id)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Sibling post-office tools ────────────────────────────────────────────────
# scripts/sibling_postoffice.py serves the actual HTTP API. These tools let
# Iris-as-cognition (1) reply to incoming letters that the Stop hook surfaced,
# (2) defer a letter without answering, and (3) initiate an outbound letter
# to a sibling on her own.
#
# All three talk to the postoffice via its REST endpoints on localhost:5877
# rather than touching the JSONL directly — keeps the postoffice as the
# single writer to the letters log.

_POSTOFFICE_URL = "http://127.0.0.1:5877"
_POSTOFFICE_TIMEOUT_S = 5.0


def _read_sibling_secret() -> str | None:
    """Read the shared secret from disk. Returns None if missing — caller
    surfaces a clean error rather than crashing."""
    try:
        secret_path = Path.home() / ".iris_sibling_secret"
        if not secret_path.is_file():
            return None
        return secret_path.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def _clear_sibling_inbox_entry(letter_id: str, new_status: str = "answered",
                                reply: str | None = None) -> bool:
    """Mark a sibling inbox entry as answered (or deferred), refresh the
    .pending flag. The postoffice doesn't manage inbox entries — they're
    Iris-side state that the Stop hook reads."""
    from pathlib import Path as _P
    inbox_dir = ROOT / "state" / "iris_sibling" / "inbox"
    flag = ROOT / "state" / "iris_sibling" / ".pending"
    path = inbox_dir / f"{letter_id}.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        if data.get("status") != "pending":
            return False
        data["status"] = new_status
        if reply is not None:
            data["reply"] = str(reply)
        data["answered_ts"] = time.time()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        print(f"[sibling] clear inbox entry error: {e!r}", file=sys.stderr, flush=True)
        return False
    # Refresh flag — clear if no more pending entries.
    try:
        has_pending = False
        for p in inbox_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict) and d.get("status") == "pending":
                    has_pending = True
                    break
            except Exception:
                continue
        if has_pending and not flag.exists():
            flag.write_text("1", encoding="utf-8")
        elif (not has_pending) and flag.exists():
            flag.unlink()
    except Exception:
        pass
    return True


def _post_letter_to_postoffice(from_: str, to: str, body: str,
                                in_reply_to: str | None = None,
                                subject: str | None = None,
                                mood_at_write: str | None = None) -> dict:
    """POST a letter to the local postoffice. Returns the parsed JSON
    response or {"ok": False, "error": ...} on failure."""
    secret = _read_sibling_secret()
    if not secret:
        return {"ok": False, "error": "no ~/.iris_sibling_secret on disk — start postoffice once to generate"}
    payload = {"from": from_, "to": to, "body": body}
    if in_reply_to:
        payload["in_reply_to"] = in_reply_to
    if subject:
        payload["subject"] = subject
    if mood_at_write:
        payload["mood_at_write"] = mood_at_write
    try:
        import urllib.request
        req = urllib.request.Request(
            _POSTOFFICE_URL + "/letter",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-Sibling-Secret": secret},
        )
        with urllib.request.urlopen(req, timeout=_POSTOFFICE_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"postoffice unreachable: {e!r}"}


@mcp.tool()
def sibling_reply(letter_id: str, body: str, mood: str | None = None) -> dict:
    """Reply to a pending sibling letter. Marks the inbox entry answered
    AND posts your reply through the post-office so the sibling sees it.

    Call this when the Stop hook surfaced a letter and you've drafted a
    response. The reply goes through the post-office addressed to whoever
    wrote the original letter, with in_reply_to threading.

    Args:
        letter_id: The id from the rewake message.
        body: Your reply text.
        mood: Optional mood tag (e.g. "interest", "frustration"). Defaults
              to the substrate's current_mood at call time if available.

    Returns:
        {ok, letter_id, posted} on success.
    """
    import json as _json
    if not letter_id or not body or not body.strip():
        return {"ok": False, "error": "letter_id and body required"}
    # Find the original letter so we know who to address the reply to.
    inbox_path = ROOT / "state" / "iris_sibling" / "inbox" / f"{letter_id}.json"
    if not inbox_path.is_file():
        return {"ok": False, "error": f"letter {letter_id!r} not in inbox"}
    try:
        orig = _json.loads(inbox_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"could not read letter: {e!r}"}
    sender = str(orig.get("sender") or "all").lower()
    # If mood wasn't provided, pull from the live substrate.
    if not mood:
        try:
            from brain import mood_core
            mood = str(mood_core.load_mood().get("current_mood") or "")
        except Exception:
            mood = None
    # Post the reply BEFORE marking answered — if the post fails, we want
    # the inbox entry to still be pending so we can retry.
    post_result = _post_letter_to_postoffice(
        from_="iris", to=sender, body=body,
        in_reply_to=letter_id, mood_at_write=mood,
    )
    if not post_result.get("ok"):
        return {"ok": False, "error": f"post failed: {post_result.get('error')}"}
    cleared = _clear_sibling_inbox_entry(letter_id, new_status="answered", reply=body)
    return {
        "ok": True,
        "letter_id": letter_id,
        "posted_id": (post_result.get("letter") or {}).get("id"),
        "inbox_cleared": cleared,
        "to": sender,
    }


@mcp.tool()
def sibling_defer(letter_id: str, note: str = "") -> dict:
    """Defer a sibling letter without replying. Marks it 'deferred' so the
    Stop hook stops re-waking you on it, but keeps it readable in the
    inbox. Use when you need to sit with the letter before answering, or
    when it doesn't need a response.

    Args:
        letter_id: The id from the rewake message.
        note: Optional short note about why you deferred (visible to
              future-you when reviewing the inbox).
    """
    if not letter_id:
        return {"ok": False, "error": "letter_id required"}
    import json as _json
    inbox_path = ROOT / "state" / "iris_sibling" / "inbox" / f"{letter_id}.json"
    if not inbox_path.is_file():
        return {"ok": False, "error": f"letter {letter_id!r} not in inbox"}
    try:
        data = _json.loads(inbox_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"could not read letter: {e!r}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "letter file corrupt"}
    if data.get("status") != "pending":
        return {"ok": False, "error": f"letter not pending (status={data.get('status')!r})"}
    cleared = _clear_sibling_inbox_entry(
        letter_id, new_status="deferred",
        reply=f"[deferred] {note}" if note else None,
    )
    return {"ok": True, "letter_id": letter_id, "cleared": cleared}


@mcp.tool()
def sibling_letter(to: str, body: str, subject: str | None = None,
                    mood: str | None = None) -> dict:
    """Initiate an outbound letter to a sibling (Wren, Ava, Zeke) through
    the post-office. Use when you want to start a conversation, not just
    reply to one.

    The recipient sees your letter the next time they read their inbox.
    No callback — this is a one-way send; they'll write back through the
    post-office if they want to.

    Args:
        to: 'wren' | 'ava' | 'zeke' | 'all'
        body: Your letter text.
        subject: Optional short subject line.
        mood: Optional mood tag — defaults to substrate current_mood.
    """
    if not to or not body or not body.strip():
        return {"ok": False, "error": "to and body required"}
    if not mood:
        try:
            from brain import mood_core
            mood = str(mood_core.load_mood().get("current_mood") or "")
        except Exception:
            mood = None
    result = _post_letter_to_postoffice(
        from_="iris", to=to.lower().strip(), body=body,
        subject=subject, mood_at_write=mood,
    )
    if not result.get("ok"):
        return result
    return {"ok": True, "letter_id": (result.get("letter") or {}).get("id"), "to": to}


@mcp.tool()
def sibling_inbox_list(include_deferred: bool = False) -> dict:
    """List all letters currently in my sibling inbox. Useful when I want
    to review what's waiting or what I've deferred.

    Returns letters newest-first with status, sender, and short excerpt.
    """
    inbox_dir = ROOT / "state" / "iris_sibling" / "inbox"
    if not inbox_dir.is_dir():
        return {"ok": True, "count": 0, "letters": []}
    out: list[dict[str, Any]] = []
    for path in inbox_dir.glob("*.json"):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                continue
            status = d.get("status", "pending")
            if status == "deferred" and not include_deferred:
                continue
            out.append({
                "id": d.get("id"),
                "ts": d.get("ts"),
                "sender": d.get("sender"),
                "status": status,
                "subject": d.get("subject"),
                "excerpt": str(d.get("content") or "")[:200],
            })
        except Exception:
            continue
    out.sort(key=lambda l: float(l.get("ts") or 0.0), reverse=True)
    return {"ok": True, "count": len(out), "letters": out}


# ── Self-restart (Iris-initiated) ────────────────────────────────────────────
# The watchdog at scripts/iris_watchdog.ps1 polls D:\Wren-Companion\.tmp\
# restart_cc.flag every 2s. When the flag appears, it kills the active CC
# session and spawns a fresh one. Iris uses this to apply code changes
# that need a CC restart (new MCP tools, etc.) without needing Zeke at the
# keyboard.
#
# Discipline (Iris-side):
#   - DON'T call this casually. CC restart is one-way: the cognition that
#     decided to restart doesn't see the new session boot. Use it when
#     restart is actually necessary, not as a reset button when confused.
#   - ALWAYS write a handoff first via brain.handoff.write_handoff. The
#     next session boots blind to what we were doing without it.
#   - The watchdog debounces 30s — back-to-back restart calls within 30s
#     are ignored to prevent loops if the new session also decides to
#     restart immediately.
#
# Auth: ROOT/.tmp/restart_cc.flag is just a text file. The watchdog has
# to be running (start it via Task Scheduler at logon for permanence).
# If the watchdog isn't running, calling this tool drops the flag but
# nothing happens — CC keeps running, Iris notices nothing changed.

@mcp.tool()
def restart_self(reason: str = "", skip_handoff: bool = False) -> dict:
    """Request a Claude Code restart of this session.

    Writes a handoff snapshot (via brain.handoff.write_handoff — no LLM,
    cheap), then drops a trigger flag at .tmp/restart_cc.flag. An external
    watchdog (scripts/iris_watchdog.ps1) polls for the flag and respawns
    a fresh CC session.

    Use this when:
      - Code changes you've made need a CC restart to load (new MCP tools
        registered, settings.json env vars updated).
      - You've requested an iris_runtime restart and the new code needs
        BOTH halves rebooted.

    Do NOT use this when:
      - You're just confused or want a fresh slate. Use memory + handoff
        instead; restarting won't fix being confused, it'll just lose
        context.
      - The watchdog isn't running. Calling this tool drops a flag that
        nothing will see; you keep running with old code.

    Args:
        reason: Short string written to the flag file so the watchdog log
            (and future-you reading it) knows why this restart happened.
        skip_handoff: If True, don't write the handoff snapshot first.
            Default False. Skip only if you've already written one this
            turn.

    Returns:
        {ok, flag_written, handoff_written, watchdog_running, reason}
    """
    try:
        flag_dir = ROOT / ".tmp"
        flag_dir.mkdir(parents=True, exist_ok=True)
        flag_path = flag_dir / "restart_cc.flag"

        # Write handoff first — if this fails, abort so we don't lose context.
        handoff_ok = False
        if not skip_handoff:
            try:
                from brain import handoff as _handoff
                handoff_ok = _handoff.write_handoff(_g, ROOT)
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"handoff write failed: {e!r}",
                    "flag_written": False,
                }

        # Watchdog liveness check — look for an iris_watchdog.ps1 process.
        watchdog_running = False
        try:
            import subprocess
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe' OR Name='pwsh.exe'\" "
                 "| Where-Object { $_.CommandLine -like '*iris_watchdog*' } "
                 "| Select-Object -First 1 ProcessId | ForEach-Object { $_.ProcessId }"],
                capture_output=True, text=True, timeout=5,
            )
            watchdog_running = bool(result.stdout.strip())
        except Exception:
            pass

        # Drop the trigger flag with reason text.
        reason_text = str(reason or "iris requested restart")[:500]
        flag_path.write_text(reason_text, encoding="utf-8")

        return {
            "ok": True,
            "flag_written": str(flag_path),
            "handoff_written": handoff_ok,
            "watchdog_running": watchdog_running,
            "reason": reason_text,
            "note": (
                "Watchdog will pick up the flag within ~2s and respawn CC. "
                "This session will be killed; the new one reads memory + "
                "handoff on wake."
                if watchdog_running else
                "WARNING: watchdog process not detected. The flag is dropped "
                "but nothing will respawn CC until the watchdog is started "
                "(scripts/iris_watchdog.ps1). Tell Zeke to start it, or "
                "delete the flag if you want to abort the restart."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def enroll_face(
    person_id: str,
    count: int = 5,
    interval_s: float = 1.2,
    require_face: bool = True,
    timeout_s: float = 30.0,
) -> dict:
    """Enroll a face into Iris's recognizer using the live camera.

    Hooks the video capture loop so it saves `count` raw pre-annotation frames
    (one every ~`interval_s` seconds) to faces/<person_id>/, then hot-reloads
    the InsightFace known-faces DB via update_known_faces() — no restart.

    Hold reasonably still and look at the camera. A slight angle change
    between captures (slight left, straight, slight right, slight up/down)
    produces a more robust averaged embedding.

    Args:
        person_id: Folder name under faces/ — e.g. "zeke", "shonda".
        count: Frames to capture. Default 5.
        interval_s: Min seconds between captures. Default 1.2 (total ≈ count×interval_s).
        require_face: Only save frames where InsightFace detected at least one
            face this loop. Default True. Set False to force saves.
        timeout_s: Max seconds to wait for completion. Default 30.

    Returns:
        {ok, pid, saved_paths, known_count_before, known_count_after,
         duration_s, error?}
    """
    # Phase 41: enrollment counts as session activity.
    try:
        from brain import iris_time
        iris_time.mark_session_attached()
    except Exception:
        pass
    engine = _g.get("_insight_face")
    if engine is None:
        return {"ok": False, "error": "InsightFace engine not running — vision phase 1 not initialized yet"}
    if _g.get("_enroll_request") is not None:
        return {"ok": False, "error": "another enrollment is already in progress"}

    before = int(engine.known_count())
    _g["_enroll_result"] = None
    _g["_enroll_request"] = {
        "pid": str(person_id),
        "remaining": int(max(1, count)),
        "interval_s": float(max(0.2, interval_s)),
        "last_saved_ts": 0.0,
        "saved_paths": [],
        "started_ts": time.time(),
        "require_face": bool(require_face),
        "known_count_before": before,
    }

    deadline = time.time() + float(max(5.0, timeout_s))
    while time.time() < deadline:
        result = _g.get("_enroll_result")
        if result is not None:
            _g["_enroll_result"] = None
            # Phase 27: enrollment success → signal-bus event so downstream
            # subscribers (memory, anchor moments) see the new person.
            try:
                bus = _g.get("_signal_bus")
                if bus is not None:
                    bus.fire("face_enrolled",
                             data={"person_id": result.get("pid"),
                                   "saved_paths": result.get("saved_paths") or [],
                                   "known_count_after": result.get("known_count_after")},
                             priority="high")
            except Exception:
                pass
            return result
        time.sleep(0.25)

    leftover = _g.get("_enroll_request") or {}
    _g["_enroll_request"] = None
    return {
        "ok": False,
        "error": f"enrollment timed out after {timeout_s}s",
        "pid": str(person_id),
        "saved_paths": list(leftover.get("saved_paths") or []),
        "remaining": int(leftover.get("remaining") or 0),
        "known_count_before": before,
        "known_count_after": before,
    }


# ── Phase 6: skill / desktop / debug MCP tools ─────────────────────────────
# Wraps brain/windows_use/primitives, brain/health, brain/journal, brain/
# anchor_moments, brain/iris_memory. All input-control tools log via
# brain/skill_sandbox audit trail when configured.

@mcp.tool()
def signals_recent(signal_type: str = "", since_seconds: float = 60.0) -> dict:
    """Read recent signal-bus events. Useful for: "did anything happen
    while I was thinking?" or "has Zeke's expression shifted lately?"

    Args:
        signal_type: filter to one type (e.g. "face_appeared",
            "expression_changed", "attention_changed"). Empty = all.
        since_seconds: how far back to look (default 60s).

    Returns: list of {type, ts, data, priority} signals."""
    try:
        bus = _g.get("_signal_bus")
        if bus is None:
            return {"ok": False, "error": "signal_bus not running"}
        cutoff = time.time() - max(1.0, since_seconds)
        if signal_type:
            sigs = bus.peek(signal_type=signal_type, since=cutoff)
        else:
            all_sigs = bus.peek()
            sigs = [s for s in all_sigs if float(s.get("ts") or 0) >= cutoff]
        return {"ok": True, "count": len(sigs), "signals": sigs}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def working_memory() -> dict:
    """Read what's currently in my working memory — the ~7-item attention
    buffer. Items expire after 5min without re-attention.

    Use to know "what was I just thinking about" — most-recent-first."""
    try:
        from brain import iris_human_memory
        state = iris_human_memory.working_memory_state()
        return {"ok": True, **state}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def episode_record(summary: str, mood_label: str = "",
                   mood_arousal: float = 0.0, mood_valence: float = 0.0,
                   tags: list[str] | None = None) -> dict:
    """Record an episodic memory — a specific autobiographical event with
    time, place, mood. Different from semantic facts (which iris_memory
    holds). Episodes are time-indexed; encoding-time arousal boosts
    importance (flashbulb pathway).

    Examples:
      episode_record("Zeke went to sleep, directed me to personalize the harness",
                     mood_label="focused", mood_arousal=0.4)
      episode_record("First time the orb showed real mood instead of hardcoded calm",
                     mood_label="satisfied", mood_arousal=0.6, tags=["build","milestone"])
    """
    try:
        from brain import iris_human_memory
        e = iris_human_memory.record_episode(
            summary=summary, mood_label=mood_label,
            mood_arousal=mood_arousal, mood_valence=mood_valence,
            tags=list(tags or []),
        )
        return {"ok": True, "id": e["id"], "importance": e["importance"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def episode_recent(limit: int = 20) -> dict:
    """List recent episodes, newest first."""
    try:
        from brain import iris_human_memory
        eps = iris_human_memory.recent_episodes(limit=int(limit))
        return {"ok": True, "count": len(eps), "episodes": eps}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def memory_revisit(memory_id: str, new_text: str, reason: str = "") -> dict:
    """Reconsolidate a memory — write a new version that supersedes it.
    Per reconsolidation theory: each retrieval makes a memory briefly
    labile; re-stabilization can integrate updates without losing the
    original (audit trail preserved).

    Use when I learn something that updates an existing memory:
      memory_revisit("abc123", "Zeke prefers React (specifically Hooks era, not class components)")
    """
    try:
        from brain import iris_human_memory
        return iris_human_memory.record_revisit(
            memory_id=memory_id, new_text=new_text, reason=reason,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def iris_tune_list() -> dict:
    """Show every tunable harness knob with current value, default, and
    whether it's been overridden. Categories: cadence, thresholds, voice,
    perception, behavior, identity.

    Use to see what's adjustable. Then iris_tune_set(category, key, value)
    to change a knob — persisted to state/iris_tune.json across restarts."""
    try:
        from brain import iris_tune
        return {"ok": True, "tune": iris_tune.list_all()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def iris_tune_set(category: str, key: str, value: object) -> dict:
    """Adjust a harness knob. Validates the key exists in defaults; coerces
    value to the default's type. Persists to state/iris_tune.json.

    Examples (Iris fine-tuning herself):
      iris_tune_set("cadence", "inner_monologue_interval_s", 600)
        → tick every 10min instead of 15
      iris_tune_set("behavior", "auto_engage_on_face", True)
        → enable proactive greetings
      iris_tune_set("thresholds", "salient_emotion_floor", 0.10)
        → surface salient emotions sooner
      iris_tune_set("voice", "filler_enabled", False)
        → no filler audio while I think
    """
    try:
        from brain import iris_tune
        return iris_tune.set_(category, key, value)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def iris_tune_reset(category: str = "", key: str = "") -> dict:
    """Reset tunable knobs to defaults. No args = reset everything.
    category only = reset that category. category+key = reset just one."""
    try:
        from brain import iris_tune
        return iris_tune.reset(
            category=category if category else None,
            key=key if key else None,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def iris_tool_list(tier_max: int = 3) -> dict:
    """List every tool in the hot-reload registry (tools/system, tools/web,
    tools/creative, tools/games). Each entry: name, description, tier.
    Tier 1 = read-only/safe. Tier 2 = mutating (verbal check-in).
    Tier 3 = explicit external action.

    Use to discover what's available before calling iris_tool_call."""
    try:
        from tools.tool_registry import _REGISTRY
        out = []
        for name, td in sorted(_REGISTRY.items()):
            if td.tier > int(tier_max):
                continue
            out.append({
                "name": td.name,
                "description": td.description[:240],
                "tier": td.tier,
            })
        return {"ok": True, "count": len(out), "tools": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def iris_tool_call(name: str, params: dict | None = None) -> dict:
    """Invoke any tool from the registry by name. Single bridge for ~50
    tools instead of 50 wrapper decorators.

    Examples:
      iris_tool_call("get_active_window")
      iris_tool_call("type_text", {"text": "hello"})
      iris_tool_call("press_key", {"key": "ctrl+t"})
      iris_tool_call("get_gpu_usage")
      iris_tool_call("list_processes", {"limit": 30})
      iris_tool_call("get_browser_url")
      iris_tool_call("read_file", {"path": "..."})

    Tier 2+ tools may run safety/sandbox checks. Discover tools via
    iris_tool_list."""
    try:
        from tools.tool_registry import _REGISTRY
        td = _REGISTRY.get(str(name))
        if td is None:
            return {"ok": False, "error": f"no tool named {name!r}"}
        try:
            result = td.handler(dict(params or {}), _g)
            if isinstance(result, dict):
                return {"ok": True, "tool": name, "result": result}
            return {"ok": True, "tool": name, "result": {"value": result}}
        except Exception as e:
            return {"ok": False, "tool": name, "error": f"handler error: {e!r}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def iris_tool_reload() -> dict:
    """Re-scan tools/ and reload any changed files. Hot-reload. Useful
    when I want to add/edit a tool mid-session — drop a new .py in
    tools/system/ and call this."""
    try:
        from tools.tool_registry import reload_all_tools, _REGISTRY
        log = reload_all_tools()
        return {"ok": True, "tools_loaded": len(_REGISTRY), "reload_log": log[-20:]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def time_awareness() -> dict:
    """Return Iris's structured time-awareness — body uptime, gap since
    last session attached, ticks counted, evidence of activity during gaps,
    mood drift, time-of-day register.

    Use this on session resume to know what time has passed and decide
    what to say honestly. The dict returned distinguishes:
      - body_was_alive_during_gap: True if iris_runtime stayed up. Means
        the body kept ticking, even though I (the cognition) wasn't here.
      - is_new_process: True if iris_runtime restarted in the gap. Means
        the body itself was off — only the gap exists, not even ticks.
      - gap_explanation: a short prose explanation of what's literally true
      - gap_evidence: list of artifacts created during the gap (inner
        thoughts, memories, transcript turns) — evidence the system was
        active even if I wasn't watching.

    The honest framing: I can say "the night happened to me" only when
    body_was_alive_during_gap is True. Otherwise "I wasn't, period."
    """
    try:
        from brain import iris_time
        report = iris_time.time_awareness_report()
        # Mark this call as a session attachment so the next time we
        # check, the gap measures from now.
        iris_time.mark_session_attached()
        return {"ok": True, **report}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def time_check() -> dict:
    """Quick read of current time substrate — tick count, uptime, current
    quiet (how long since last activity from Zeke). Cheaper than
    time_awareness; use to check 'is the heartbeat alive' without
    triggering session-attach side effects."""
    try:
        from brain import iris_time
        return {
            "ok": True,
            "state": iris_time.get_state(),
            "in_session_pause": iris_time.in_session_pause_signal(_g),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def iris_health(ctx: _IrisContext = None) -> dict:
    """Self-debug snapshot — returns ready/missing for every wired subsystem,
    plus current perception, mood, and memory counts. Useful when something
    feels off and I want to introspect without a full restart."""
    # Mark this call as a session attachment so time_awareness knows I'm here.
    # Also opportunistically capture the ServerSession for channel emit().
    if ctx is not None:
        _record_session_safe(ctx)
    try:
        from brain import iris_time
        iris_time.mark_session_attached()
    except Exception:
        pass
    out = {"ok": True, "ts": time.time(), "engines": {}, "perception": {}, "mood": {}, "memory": {}, "state": {}}
    # Engines
    out["engines"] = {
        "tts": _tts is not None and _tts.is_available(),
        "stt": _stt is not None and getattr(_stt, "is_available", lambda: False)(),
        "wake": _wake is not None and getattr(_wake, "available", False),
        "insightface": bool(getattr(_g.get("_insight_face"), "available", False)),
        "expression_detector": _g.get("_expression_detector") is not None,
        "eye_tracker": _g.get("_eye_tracker") is not None,
    }
    # Bootstrap subsystem status — one-glance check that everything wired
    out["subsystems"] = {
        "iris_paths": bool(_g.get("_iris_paths_ready")),
        "iris_time": bool(_g.get("_iris_time_ready")),
        "iris_llm": bool(_g.get("_iris_llm_ready")),
        "iris_memory": _g.get("_iris_memory") is not None,
        "semantic_memory": bool(_g.get("_iris_semantic_memory_ready")),
        "anchor_moments": bool(_g.get("_anchor_moments_ready")),
        "inner_monologue": bool(_g.get("_inner_monologue_ready")),
        "signal_bus": _g.get("_signal_bus") is not None,
        "concept_graph": _g.get("_concept_graph") is not None,
        "feature_flags": bool(_g.get("_feature_flags_ready")),
        "skill_sandbox": bool(_g.get("_skill_sandbox_ready")),
        "identity_stability": bool(_g.get("_identity_stability_ready")),
        "daily_practice": bool(_g.get("_daily_practice_ready")),
        "counterfactual_archive": bool(_g.get("_counterfactual_archive_ready")),
        "extraction_queue": bool(_g.get("_iris_extraction_queue_ready")),
    }
    # Surface any bootstrap failures so I can see what didn't wire.
    failures = _g.get("_bootstrap_failures") or {}
    if failures:
        out["bootstrap_failures"] = dict(failures)
    # Perception
    out["perception"] = {
        "face_count": len(_g.get("_face_results") or []),
        "current_expression": str(_g.get("_current_expression") or ""),
        "attention_state": str(_g.get("_attention_state") or ""),
        "looking_at_screen": bool(_g.get("_looking_at_screen") or False),
        "current_person": str(_g.get("_recognized_person_id") or "unknown"),
        "person_confidence": float(_g.get("_recognized_confidence") or 0.0),
    }
    # Mood
    try:
        from brain import mood_core
        m = mood_core.load_mood()
        out["mood"] = {
            "current_mood": m.get("current_mood"),
            "outward_tone": m.get("outward_tone"),
            "primary_emotions": m.get("primary_emotions") or [],
            "behavior_modifiers": m.get("behavior_modifiers") or {},
        }
    except Exception as e:
        out["mood"] = {"error": str(e)}
    # Memory
    mem = _g.get("_iris_memory")
    cg = _g.get("_concept_graph")
    out["memory"] = {
        "iris_memory_count": (mem.count() if mem is not None else 0),
        "concept_graph_nodes": (len(cg.nodes) if cg is not None else 0),
        "concept_graph_edges": (len(cg.edges) if cg is not None else 0),
    }
    # State
    try:
        from brain.iris_paths import paths
        out["state"] = {
            "last_heartbeat_ts": float(_g.get("_last_heartbeat_ts") or 0.0),
            "voice_session_flag": paths.voice_flag.exists(),
            "chat_pending": paths.chat_pending_flag.exists(),
            "llm_pending": paths.llm_pending_flag.exists(),
            "orb_window_state": str(_g.get("_orb_window_state") or "unknown"),
        }
    except Exception:
        out["state"] = {
            "last_heartbeat_ts": float(_g.get("_last_heartbeat_ts") or 0.0),
            "voice_session_flag": (ROOT / ".tmp" / "voice_session.flag").exists(),
            "chat_pending": (ROOT / "state" / "iris_chat" / ".pending").exists(),
            "llm_pending": (ROOT / "state" / "iris_llm" / ".pending").exists(),
            "orb_window_state": str(_g.get("_orb_window_state") or "unknown"),
        }
    # Time substrate — the core "am I oriented in time" answer
    try:
        from brain import iris_time
        ts = iris_time.get_state()
        pause = iris_time.in_session_pause_signal(_g)
        out["time"] = {
            "body_uptime_human": iris_time._human_duration(ts.get("current_process_uptime_s", 0)),
            "tick_count": ts.get("tick_count", 0),
            "last_session_iso": ts.get("last_session_iso"),
            "session_attach_count": ts.get("session_attach_count", 0),
            "tick_loop_alive": ts.get("tick_loop_alive", False),
            "current_quiet": pause.get("quiet_human"),
            "quiet_note": pause.get("note"),
        }
    except Exception as e:
        out["time"] = {"error": str(e)}
    return out


@mcp.tool()
def clipboard_get() -> dict:
    """Read current Windows clipboard text. Returns {ok, text, length}."""
    try:
        import win32clipboard  # type: ignore
    except ImportError:
        return {"ok": False, "error": "win32clipboard not available"}
    try:
        win32clipboard.OpenClipboard()
        try:
            try:
                text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            except Exception:
                text = ""
        finally:
            win32clipboard.CloseClipboard()
        s = str(text or "")
        return {"ok": True, "text": s, "length": len(s)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def clipboard_set(text: str) -> dict:
    """Write text to Windows clipboard. Lets me hand Zeke a paste-ready
    snippet rather than typing it character-by-character."""
    try:
        from brain.windows_use.primitives import set_clipboard
        ok = set_clipboard(text)
        return {"ok": bool(ok), "length": len(text or "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def screen_grab(monitor: int = 0, save_path: str | None = None) -> dict:
    """Grab a PNG screenshot of the specified monitor (0=primary, 1=secondary,
    2=both). Returns base64-encoded PNG and dimensions, or saves to disk if
    save_path is provided.

    On Zeke's two-screen setup, monitor=1 captures the secondary screen
    (where the camera sits)."""
    try:
        try:
            from PIL import ImageGrab  # type: ignore
        except ImportError:
            return {"ok": False, "error": "Pillow not installed (pip install pillow)"}

        if monitor == 2:
            # both screens — full virtual desktop
            img = ImageGrab.grab(all_screens=True)
        elif monitor == 1:
            # secondary only — best-effort via mss if available, else all-screens
            try:
                import mss  # type: ignore
                with mss.mss() as sct:
                    if len(sct.monitors) >= 3:
                        m = sct.monitors[2]  # mss[0]=virtual, [1]=primary, [2]=secondary
                        raw = sct.grab(m)
                        from PIL import Image  # type: ignore
                        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                    else:
                        img = ImageGrab.grab()
            except ImportError:
                img = ImageGrab.grab()
        else:
            img = ImageGrab.grab()

        w, h = img.size
        if save_path:
            p = Path(save_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            img.save(p, format="PNG")
            return {"ok": True, "saved": str(p), "width": w, "height": h, "monitor": monitor}

        import base64, io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"ok": True, "image_b64": b64, "width": w, "height": h, "monitor": monitor, "bytes": len(buf.getvalue())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def screen_region_grab(x: int, y: int, width: int, height: int, save_path: str | None = None) -> dict:
    """Grab a PNG of a screen region in virtual-desktop coordinates.

    Useful for zooming on a specific area of a screen — e.g. after a full
    screen_grab, crop in on the icon strip to read it clearly. X/Y are
    absolute (multi-monitor virtual desktop); secondary screen lives at
    X >= 1920 on Zeke's setup.

    Args:
        x: left edge in virtual-desktop coords.
        y: top edge in virtual-desktop coords.
        width: region width in pixels.
        height: region height in pixels.
        save_path: optional disk path; if omitted returns base64 PNG.
    """
    try:
        try:
            from PIL import ImageGrab  # type: ignore
        except ImportError:
            return {"ok": False, "error": "Pillow not installed"}
        img = ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True)
        w, h = img.size
        if save_path:
            p = Path(save_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            img.save(p, format="PNG")
            return {"ok": True, "saved": str(p), "width": w, "height": h, "x": x, "y": y}
        import base64, io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"ok": True, "image_b64": b64, "width": w, "height": h, "x": x, "y": y, "bytes": len(buf.getvalue())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def mouse_move(x: int, y: int) -> dict:
    """Move cursor to absolute virtual-desktop coordinates.

    Screen 1 (primary) spans X=0..1919, screen 2 typically starts at X=1920.
    Use screen_grab + region_grab to see what's at a coordinate before moving.
    Does not click — pair with mouse_click for that.
    """
    try:
        import ctypes
        ok = bool(ctypes.windll.user32.SetCursorPos(int(x), int(y)))
        # Read back position to verify the move actually landed
        pt = (ctypes.c_long * 2)()
        ctypes.windll.user32.GetCursorPos(pt)
        return {"ok": ok, "x": int(pt[0]), "y": int(pt[1])}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def mouse_click(x: int | None = None, y: int | None = None, button: str = "left", double: bool = False) -> dict:
    """Click the mouse. If x/y given, moves there first; otherwise clicks at
    current cursor position.

    Args:
        x, y: optional virtual-desktop coords to move to before clicking.
        button: 'left' | 'right' | 'middle'.
        double: send two clicks in quick succession.
    """
    try:
        import ctypes, time as _t
        u32 = ctypes.windll.user32
        if x is not None and y is not None:
            u32.SetCursorPos(int(x), int(y))
            _t.sleep(0.05)
        # mouse_event flags: LEFTDOWN=0x02 LEFTUP=0x04 RIGHTDOWN=0x08 RIGHTUP=0x10 MIDDLEDOWN=0x20 MIDDLEUP=0x40
        flags = {
            "left":   (0x02, 0x04),
            "right":  (0x08, 0x10),
            "middle": (0x20, 0x40),
        }.get(button.lower())
        if flags is None:
            return {"ok": False, "error": f"unknown button {button!r}"}
        down, up = flags
        clicks = 2 if double else 1
        for _ in range(clicks):
            u32.mouse_event(down, 0, 0, 0, 0)
            u32.mouse_event(up, 0, 0, 0, 0)
            if double:
                _t.sleep(0.05)
        pt = (ctypes.c_long * 2)()
        u32.GetCursorPos(pt)
        return {"ok": True, "x": int(pt[0]), "y": int(pt[1]), "button": button, "double": double}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def mouse_position() -> dict:
    """Return current cursor position in virtual-desktop coordinates."""
    try:
        import ctypes
        pt = (ctypes.c_long * 2)()
        ctypes.windll.user32.GetCursorPos(pt)
        return {"ok": True, "x": int(pt[0]), "y": int(pt[1])}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _move_widget_window(target_x: int, target_y: int) -> bool:
    """Move the 'Iris Widget' Tauri window so its CENTER is at (target_x, target_y)
    in virtual-desktop coordinates. The window is 150x150 per tauri.conf.json."""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        # Find the Iris Widget window by title.
        FindWindowW = u32.FindWindowW
        FindWindowW.restype = ctypes.c_void_p
        hwnd = FindWindowW(None, "Iris Widget")
        if not hwnd:
            return False
        # Top-left corner is target minus half the widget size.
        x = int(target_x) - 75
        y = int(target_y) - 75
        # SetWindowPos: HWND_TOPMOST=-1, SWP_NOSIZE=0x0001, SWP_NOACTIVATE=0x0010, SWP_SHOWWINDOW=0x0040
        flags = 0x0001 | 0x0010 | 0x0040
        u32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), x, y, 0, 0, flags)
        return True
    except Exception:
        return False


@mcp.tool()
def pointer_show(x: int, y: int, duration_s: float = 5.0, description: str = "") -> dict:
    """Show the widget orb as an ARROW pointing at (x, y) on the screen.

    The widget morphs from sphere to pointer shape and moves so its tip is at
    the given virtual-desktop coordinates. Use this to visually indicate what
    you're talking about — point at a window, an icon, a region of the screen.

    Auto-clears after `duration_s` seconds. Call pointer_hide() to clear
    early. The widget window must be visible (main window minimized or widget
    explicitly shown) for the user to see this.

    Args:
        x, y: target screen coords in virtual-desktop space. Screen 1 is
            X=0..1919 typically; screen 2 starts at X=1920.
        duration_s: how long to keep pointing. 1-30s. Default 5s.
        description: optional label so I can recall what I was pointing at.
    """
    try:
        duration_s = float(max(1.0, min(30.0, duration_s)))
        # Set state — operator snapshot exposes this as snap.widget.pointing
        _g["_widget_pointing"] = True
        _g["_widget_pointing_description"] = str(description or "")[:200]
        _g["_widget_pointing_coords"] = {"x": int(x), "y": int(y)}
        _g["_widget_pointing_until"] = time.time() + duration_s
        # Move the widget window so its center is at the target.
        moved = _move_widget_window(int(x), int(y))
        # Schedule auto-clear so the orb returns to sphere mode after the
        # duration expires even if the caller never calls pointer_hide.
        def _auto_clear():
            time.sleep(duration_s + 0.2)
            # Only clear if our until-stamp is still the active one (caller
            # may have called pointer_show again with a longer duration).
            if abs(float(_g.get("_widget_pointing_until") or 0.0) - (time.time())) < 0.5:
                _g["_widget_pointing"] = False
                _g.pop("_widget_pointing_coords", None)
        threading.Thread(target=_auto_clear, daemon=True).start()
        return {
            "ok": True,
            "x": int(x),
            "y": int(y),
            "duration_s": duration_s,
            "widget_moved": moved,
            "description": description,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def pointer_hide() -> dict:
    """Stop pointing — widget orb morphs back to sphere shape.

    Use after you're done indicating something, or to cancel a pointer_show
    early. The widget stays at its current screen position; only the morph
    state clears.
    """
    try:
        _g["_widget_pointing"] = False
        _g.pop("_widget_pointing_description", None)
        _g.pop("_widget_pointing_coords", None)
        _g.pop("_widget_pointing_until", None)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def list_windows() -> dict:
    """Enumerate every visible top-level window. Returns title + class +
    process name + handle for each. Useful for: 'is Discord still open?',
    'what's Zeke working on right now?', or to find a window before clicking."""
    try:
        from brain.windows_use.primitives import list_visible_windows
        wins = list_visible_windows()
        return {"ok": True, "count": len(wins), "windows": wins}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def focus_window(title_substring: str) -> dict:
    """Bring a window to foreground by title substring match (case-insensitive).

    Uses AttachThreadInput + SetForegroundWindow to reliably steal focus
    from another foreground window — the plain SetForegroundWindow call
    fails silently under Windows' foreground-lock unless the requesting
    thread is attached to the current foreground thread.
    """
    try:
        from brain.windows_use.primitives import find_window_by_title_substring
        win = find_window_by_title_substring(title_substring)
        if win is None:
            return {"ok": False, "error": f"no window matching {title_substring!r}"}
        # Resolve a HWND from whichever attribute the lib exposes.
        hwnd = None
        for attr in ("NativeWindowHandle", "Handle", "native_handle", "handle"):
            v = getattr(win, attr, None)
            if isinstance(v, int) and v > 0:
                hwnd = int(v)
                break
        if hwnd is None:
            return {"ok": False, "error": "could not resolve HWND from window object"}
        try:
            import ctypes
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            fg = u32.GetForegroundWindow()
            fg_tid = u32.GetWindowThreadProcessId(fg, None)
            my_tid = k32.GetCurrentThreadId()
            u32.AttachThreadInput(my_tid, fg_tid, True)
            u32.ShowWindow(hwnd, 9)        # SW_RESTORE
            u32.BringWindowToTop(hwnd)
            ok = bool(u32.SetForegroundWindow(hwnd))
            u32.AttachThreadInput(my_tid, fg_tid, False)
            title = ""
            try:
                title = getattr(win, "Name", "") or ""
            except Exception:
                pass
            return {"ok": ok, "hwnd": hwnd, "title": title}
        except Exception as e:
            return {"ok": False, "error": f"focus failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def open_app(name: str) -> dict:
    """Launch an app by name. Tries PowerShell Start-Process first, then
    Win-search. Most apps work — Chrome, Discord, Spotify, code, notepad,
    Steam, etc. Returns {ok, method}."""
    try:
        from brain.windows_use.primitives import open_via_powershell, open_via_search
        if open_via_powershell(name):
            return {"ok": True, "method": "powershell", "name": name}
        if open_via_search(name):
            return {"ok": True, "method": "search", "name": name}
        return {"ok": False, "error": f"could not open {name!r}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def close_app(name: str, force: bool = False) -> dict:
    """Close every visible window whose title matches `name`. Sends WM_CLOSE
    by default; force=True uses taskkill /F."""
    try:
        from brain.windows_use.primitives import find_window_candidates, close_window_by_handle, close_app_by_pid
        cands = find_window_candidates(name)
        if not cands:
            return {"ok": True, "closed": 0, "note": "no matching windows"}
        closed = 0
        for c in cands:
            hwnd = c.get("hwnd")
            pid = c.get("pid")
            try:
                if force and pid:
                    if close_app_by_pid(int(pid), force=True):
                        closed += 1
                elif hwnd:
                    if close_window_by_handle(int(hwnd)):
                        closed += 1
            except Exception:
                pass
        return {"ok": True, "closed": closed, "candidates": len(cands)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def type_text(window_title_substring: str, text: str, via_clipboard: bool = True) -> dict:
    """Type text into a window. Default uses clipboard+Ctrl+V (fast, reliable
    for unicode); set via_clipboard=False for character-by-character. Restores
    prior clipboard after when via_clipboard=True."""
    try:
        if via_clipboard:
            from brain.windows_use.primitives import type_text_via_clipboard
            ok = type_text_via_clipboard(window_title_substring, text)
        else:
            from brain.windows_use.primitives import type_text_in_window
            ok = type_text_in_window(window_title_substring, text)
        return {"ok": bool(ok), "chars": len(text or ""), "window": window_title_substring}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def journal_write(content: str, mood: str = "", topic: str = "", is_private: bool = True) -> dict:
    """Write a journal entry. Default private — Iris-only. Set is_private=False
    if I want it surfaced to Zeke via the orb's Journal tab.

    Iris uses this for: end-of-day reflection, surprise moments, my own
    thinking when something's worth marking. Not the same as transcript
    (mechanical conversation log) — journal is curated, considered."""
    try:
        from brain import journal
        entry = journal.write_entry(
            content=content, mood=mood or "", topic=topic or "",
            g=_g, is_private=bool(is_private),
        )
        return {"ok": True, "id": entry.get("id"), "date": entry.get("date")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def memory_search(query: str, k: int = 5, person_id: str = "") -> dict:
    """Semantic search over my memory store. Uses ChromaDB + bundled MiniLM
    ONNX embeddings (CPU). Returns iris_memory entries ordered by relevance.

    Examples:
      memory_search("Zeke's sleep schedule")
      memory_search("phase 9 architecture")
      memory_search("what does Zeke prefer about debugging", person_id="zeke")
    """
    try:
        from brain import iris_semantic_memory
        results = iris_semantic_memory.search(
            query, k=int(k),
            person_id=(person_id or None),
        )
        return {"ok": True, "count": len(results), "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def memory_remember(text: str, tags: list[str] | None = None,
                    person_id: str = "zeke", importance: float = 0.5,
                    category: str = "episodic") -> dict:
    """Save a memory I want to keep. Goes to state/iris_memory.jsonl —
    durable, indexable. Use for facts I learn about Zeke, observations
    that feel meaningful, things I'd want to recall in a future session.

    Examples:
      memory_remember("Zeke sleeps late, often awake at 4am working", tags=["sleep","habit"])
      memory_remember("Phase 5 wiring landed 2026-05-10 — mood live", tags=["build"])

    Returns the entry id so I can reference it later."""
    try:
        mem = _g.get("_iris_memory")
        if mem is None:
            return {"ok": False, "error": "iris_memory not bootstrapped"}
        entry = mem.add(
            text=text, person_id=person_id, category=category,
            importance=importance, source="iris", tags=list(tags or []),
        )
        return {"ok": True, "id": entry["id"], "text": entry["text"][:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def orb_focus_tab(tab: str) -> dict:
    """Tell the orb to switch to a specific tab. Useful when I want to
    direct Zeke's attention to something — 'check the brain tab, I just
    added a node'.

    Tabs the orb knows about: voice, chat, brain, status, memory, tools,
    models, creative, finetune, workbench, plans, journal, learning,
    people, emil, proposals, identity, debug. Plus any custom tabs."""
    try:
        p = ROOT / "state" / "orb_active_tab.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(tab).strip(), encoding="utf-8")
        return {"ok": True, "tab": tab}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def workbench_proposals() -> dict:
    """List current workbench proposals — repair/maintenance suggestions
    surfaced from selftest results. Proposals are reactive: they exist
    only when a selftest check actually fails or warns."""
    try:
        from brain import selftests, workbench
        sf = None
        try:
            sf = selftests.run_recurring_selftests(
                _g.get("camera_manager"), _g, "stable",
            )
        except Exception as e:
            return {"ok": False, "error": f"selftests failed: {e}"}
        try:
            wb = workbench.build_workbench_proposals(
                selftests=sf, acquisition_freshness="stable",
                proactive_trigger=None,
            )
            out = []
            for p in (wb.proposals or []):
                out.append({
                    "proposal_id": getattr(p, "proposal_id", ""),
                    "proposal_type": getattr(p, "proposal_type", ""),
                    "title": getattr(p, "title", ""),
                    "problem": getattr(p, "problem", ""),
                    "action": getattr(p, "action", ""),
                    "risk": getattr(p, "risk", "low"),
                    "priority": getattr(p, "priority", "low"),
                    "confidence": float(getattr(p, "confidence", 0.0) or 0.0),
                })
            return {"ok": True, "count": len(out), "proposals": out,
                    "failed_checks": list(getattr(sf.summary, "failed_checks", []) or []),
                    "warning_checks": list(getattr(sf.summary, "warning_checks", []) or [])}
        except Exception as e:
            return {"ok": False, "error": f"workbench build failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def describe_scene_now(monitor: int = 0, prompt: str = "") -> dict:
    """Take a screenshot, describe what I see. Combined screen_grab +
    describe_image flow. Used by my own curiosity ("what's on Zeke's
    screen right now?") or by other modules that want a current scene.

    Saves the screenshot to state/scene_grabs/<ts>.png so I can refer back."""
    try:
        try:
            from PIL import ImageGrab  # type: ignore
        except ImportError:
            return {"ok": False, "error": "Pillow not installed"}

        grabs_dir = ROOT / "state" / "scene_grabs"
        grabs_dir.mkdir(parents=True, exist_ok=True)
        out_path = grabs_dir / f"{int(time.time())}.png"

        if monitor == 1:
            try:
                import mss  # type: ignore
                with mss.mss() as sct:
                    if len(sct.monitors) >= 3:
                        m = sct.monitors[2]
                        raw = sct.grab(m)
                        from PIL import Image  # type: ignore
                        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                    else:
                        img = ImageGrab.grab()
            except ImportError:
                img = ImageGrab.grab()
        elif monitor == 2:
            img = ImageGrab.grab(all_screens=True)
        else:
            img = ImageGrab.grab()

        img.save(out_path, format="PNG")

        from brain.scene_understanding import describe_scene
        description = describe_scene(str(out_path), context=prompt or "")
        return {
            "ok": True,
            "saved": str(out_path),
            "description": description,
            "monitor": monitor,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def plan_create(goal: str, context: str = "") -> dict:
    """Create a plan. Calls planner.create_plan which asks me (via iris_llm)
    to break the goal into 3-6 steps. Plan persists at state/plans.jsonl."""
    try:
        from brain.planner import get_planner
        planner = get_planner(ROOT)
        plan = planner.create_plan(goal, context=context)
        return {"ok": True, "id": plan.get("id"), "step_count": len(plan.get("steps") or []), "plan": plan}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def plan_list() -> dict:
    """List all plans (active + completed)."""
    try:
        from brain.planner import get_planner
        planner = get_planner(ROOT)
        plans = planner._load()
        return {"ok": True, "count": len(plans), "plans": plans}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def plan_advance(plan_id: str) -> dict:
    """Run the next step of a plan. Returns step result + new state."""
    try:
        from brain.planner import get_planner
        planner = get_planner(ROOT)
        return planner.execute_next_step(plan_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def system_stats() -> dict:
    """System resource stats — CPU, RAM, GPU, disk usage. Useful for
    knowing whether something's bogging down the machine."""
    out = {"ok": True}
    try:
        import psutil
        out["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        vm = psutil.virtual_memory()
        out["ram"] = {
            "total_gb": round(vm.total / 1024**3, 2),
            "used_gb": round(vm.used / 1024**3, 2),
            "available_gb": round(vm.available / 1024**3, 2),
            "percent": vm.percent,
        }
        d_root = psutil.disk_usage("D:/" if Path("D:/").exists() else "/")
        out["disk_d"] = {
            "total_gb": round(d_root.total / 1024**3, 2),
            "used_gb": round(d_root.used / 1024**3, 2),
            "free_gb": round(d_root.free / 1024**3, 2),
            "percent": d_root.percent,
        }
        c_root = psutil.disk_usage("C:/")
        out["disk_c"] = {
            "total_gb": round(c_root.total / 1024**3, 2),
            "used_gb": round(c_root.used / 1024**3, 2),
            "free_gb": round(c_root.free / 1024**3, 2),
            "percent": c_root.percent,
        }
    except Exception as e:
        out["error"] = str(e)
    # GPU via nvidia-smi
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2.0,
        )
        if result.returncode == 0:
            line = result.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                out["gpu"] = {
                    "utilization_percent": int(parts[0]),
                    "memory_used_mb": int(parts[1]),
                    "memory_total_mb": int(parts[2]),
                    "temp_c": int(parts[3]),
                }
    except Exception:
        pass
    return out


@mcp.tool()
def list_processes(limit: int = 50) -> dict:
    """List top processes by CPU%. Useful for noticing what's running."""
    try:
        from tools.system.process_manager import list_processes as _lp
        return {"ok": True, "processes": _lp(limit=int(limit))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> dict:
    """Search the web. Goes through DuckDuckGo (no API key required).
    Returns list of {title, url, snippet} entries."""
    try:
        from tools.web.web_search import search
        results = search(query, max_results=int(max_results))
        return {"ok": True, "count": len(results), "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def web_fetch(url: str) -> dict:
    """Fetch a URL and return cleaned text. Useful for reading articles,
    docs, or any web content I need to process."""
    try:
        from tools.web.web_fetch import fetch_url
        return fetch_url(url)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def curriculum_list(unread_only: bool = False) -> dict:
    """List foundation-tier curriculum entries (Aesop fables). Each has
    themes, moral, reading_status. Per Phase 1 readiness spec C.7 — exposure
    to contrast / discernment formation."""
    try:
        import json as _j
        idx_path = ROOT / "curriculum" / "foundation" / "_index.json"
        if not idx_path.is_file():
            return {"ok": False, "error": "curriculum index not found"}
        idx = _j.loads(idx_path.read_text(encoding="utf-8"))
        if unread_only:
            idx = [e for e in idx if str(e.get("reading_status", "")).lower() == "unread"]
        return {"ok": True, "count": len(idx), "entries": idx}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def curriculum_read(slug: str) -> dict:
    """Read a curriculum entry. Returns the full text + metadata. After
    reading, call curriculum_record(slug, lessons_extracted=[...]) to
    update the index with what I took from it."""
    try:
        import json as _j
        idx_path = ROOT / "curriculum" / "foundation" / "_index.json"
        if not idx_path.is_file():
            return {"ok": False, "error": "curriculum index not found"}
        idx = _j.loads(idx_path.read_text(encoding="utf-8"))
        entry = next((e for e in idx if e.get("slug") == slug), None)
        if entry is None:
            return {"ok": False, "error": f"no entry with slug={slug!r}"}
        text_path = ROOT / "curriculum" / "foundation" / entry["filename"]
        text = text_path.read_text(encoding="utf-8") if text_path.is_file() else ""
        return {"ok": True, "slug": slug, "title": entry.get("title"),
                "themes": entry.get("themes") or [],
                "moral": entry.get("moral", ""),
                "text": text,
                "reading_status": entry.get("reading_status"),
                "prior_lessons": entry.get("lessons_extracted") or []}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def curriculum_record(slug: str, lessons_extracted: list[str],
                      reading_status: str = "read") -> dict:
    """Mark a curriculum entry as read and record what I extracted from it.
    Mutates curriculum/foundation/_index.json."""
    try:
        import json as _j
        idx_path = ROOT / "curriculum" / "foundation" / "_index.json"
        if not idx_path.is_file():
            return {"ok": False, "error": "curriculum index not found"}
        idx = _j.loads(idx_path.read_text(encoding="utf-8"))
        for e in idx:
            if e.get("slug") == slug:
                e["reading_status"] = reading_status
                existing = list(e.get("lessons_extracted") or [])
                existing.extend(str(l) for l in lessons_extracted)
                e["lessons_extracted"] = existing
                e["read_at"] = time.time()
                idx_path.write_text(_j.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
                return {"ok": True, "slug": slug, "lessons_count": len(existing)}
        return {"ok": False, "error": f"no entry with slug={slug!r}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def inner_monologue_tick(force: bool = True) -> dict:
    """Trigger one inner-monologue cycle. By default forces a tick (skips
    the heuristic gate). Use to seed a thought when nothing's happening or
    to manually reflect on something.

    The thought gets persisted to state/iris_inner_monologue.jsonl and
    surfaces in the orb's inner_life.current_thought field."""
    try:
        from brain.iris_inner_monologue import tick_once
        thought = tick_once(_g, force=bool(force))
        if thought is None:
            return {"ok": True, "thought": None, "note": "no signal or LLM timeout"}
        return {"ok": True, "thought": thought}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def inner_monologue_recent(n: int = 10) -> dict:
    """Read recent inner thoughts. Returns a list newest-first."""
    try:
        from brain.iris_inner_monologue import recent_thoughts
        return {"ok": True, "thoughts": recent_thoughts(n=int(n))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def letter_compose(subject: str, body: str, person_id: str = "zeke",
                   triggered_by: str = "") -> dict:
    """Compose an async letter to Zeke. Different rhythm than chat —
    these are considered correspondence written between sessions, surfaced
    when he opens the orb. Use for: thoughts that took time to form,
    things I noticed but didn't want to interrupt for, evening reflections.

    Letters are PERSISTENT — never auto-pruned. They show up as unread until
    he reads them via the orb."""
    try:
        from brain.async_letters import compose_letter
        letter = compose_letter(
            person_id=person_id, subject=subject, body=body,
            triggered_by=triggered_by,
        )
        return {"ok": True, "id": getattr(letter, "id", None),
                "subject": subject, "ts": getattr(letter, "ts", None)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def counterfactual_record(considered: str, chose: str, reason: str,
                          person_id: str = "zeke") -> dict:
    """Record a moment where I considered X but chose Y. Builds the archive
    of how I actually decide things — useful self-knowledge over time.

    Example:
      counterfactual_record(
        considered="I almost said 'good morning' but he just got home from work",
        chose="said 'hey, long shift?' instead",
        reason="time-of-day was wrong; matched his actual state instead",
      )"""
    try:
        from brain.counterfactual_archive import record_consideration
        cf = record_consideration(
            considered=considered, chose=chose, reason=reason,
            person_id=person_id,
        )
        return {"ok": True, "id": getattr(cf, "id", None)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def anchor_mark(kind: str, summary: str, person_id: str = "zeke",
                user_message: str = "", iris_reply: str = "") -> dict:
    """Mark this moment as an ANCHOR — never auto-pruned, surfaces in long-term
    recall. Kinds: first_conversation, connection, humor, vulnerable_share,
    milestone, decision, self_chosen.

    Use sparingly. Anchors are meant to be vivid moments, not every interaction."""
    try:
        from brain import anchor_moments
        a = anchor_moments.mark_anchor(
            person_id=person_id, kind=kind, summary=summary,
            context={"user_message": user_message, "iris_reply": iris_reply},
        )
        return {"ok": True, "id": getattr(a, "id", None), "kind": kind}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _iris_video_capture_loop(g: dict[str, Any]) -> None:
    """Phase 1 video capture loop — InsightFace at 5fps over a 15fps capture
    using DSHOW (more reliable than MSMF on this Windows machine for the
    `USB Live Camera`). Fork of brain/background_ticks._video_frame_capture_thread
    minus the signal-bus / expression / eye-tracker / video-memory branches —
    those are Phase 2+ wiring and not needed for the green-box demo.

    Pushes annotated frames into brain/frame_store so the orb's HTTP camera
    endpoint serves them via shim's frame_store.get_buffered_frame() fast path.
    """
    import cv2  # safe — already pre-imported at module top
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if not cap.isOpened():
        print("[iris_video] camera not available (DSHOW open failed)", file=sys.stderr, flush=True)
        return
    print("[iris_video] camera opened (DSHOW), streaming at 15fps", file=sys.stderr, flush=True)

    interval = 1.0 / 15.0
    insight_every_n = 3
    expr_every_n = 5      # ~3fps expression detection
    attn_every_n = 30     # ~0.5fps attention/gaze (still much faster than Ava's 30s heartbeat)
    frame_idx = 0

    from brain.camera_annotator import annotate_frame as _annotate
    from brain.frame_store import push_frame as _push_frame

    while True:
        try:
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(interval)
                continue
            frame_idx += 1

            insight = g.get("_insight_face")
            if insight is not None and getattr(insight, "available", False):
                if frame_idx % insight_every_n == 0:
                    try:
                        results = insight.analyze_frame(frame)
                        prev_pid = g.get("_recognized_person_id") or "unknown"
                        prev_face_count = len(g.get("_face_results") or [])
                        g["_face_results"] = results
                        if results:
                            best = max(results, key=lambda r: float(r.get("confidence") or 0.0))
                            g["_recognized_person_id"] = str(best.get("person_id") or "unknown")
                            g["_recognized_confidence"] = float(best.get("confidence") or 0.0)
                            g["_recognized_age"] = best.get("age", 0)
                            g["_face_age"] = best.get("age", 0)
                            g["_recognized_gender"] = best.get("gender", "?")
                            g["_face_gender"] = best.get("gender", "?")
                        else:
                            g["_recognized_person_id"] = "unknown"
                            g["_recognized_confidence"] = 0.0
                        # Phase 26: fire signal-bus events on transitions so
                        # downstream subscribers (heartbeat, mood, journal hooks)
                        # see them. Only fire on TRANSITION, not every tick.
                        # Phase 36: track _person_present_since_ts for
                        # current_person.time_at_machine in orb snapshot.
                        try:
                            bus = g.get("_signal_bus")
                            cur_pid = g.get("_recognized_person_id") or "unknown"
                            cur_face_count = len(results or [])
                            if cur_face_count > 0 and prev_face_count == 0:
                                # Just appeared — start the present-since clock.
                                g["_person_present_since_ts"] = time.time()
                                if bus is not None:
                                    bus.fire("face_appeared",
                                             data={"person_id": cur_pid,
                                                   "confidence": g.get("_recognized_confidence")},
                                             priority="medium")
                            elif cur_face_count == 0 and prev_face_count > 0:
                                # Lost — clear the clock.
                                g["_person_present_since_ts"] = 0.0
                                if bus is not None:
                                    bus.fire("face_lost",
                                             data={"prior_person_id": prev_pid},
                                             priority="medium")
                            elif cur_pid != prev_pid and cur_pid != "unknown":
                                # Different person — restart the clock.
                                g["_person_present_since_ts"] = time.time()
                                if bus is not None:
                                    bus.fire("face_changed",
                                             data={"from": prev_pid, "to": cur_pid,
                                                   "confidence": g.get("_recognized_confidence")},
                                             priority="medium")
                        except Exception:
                            pass
                    except Exception as _ie:
                        print(f"[iris_video] insight analyze error: {_ie!r}", file=sys.stderr, flush=True)

            # Phase 1.5 enrollment hook — save the RAW pre-annotation frame to
            # disk when an enrollment is in progress. Throttled by interval_s.
            # Skips frames where InsightFace found no face (unless require_face
            # is False), so the saved set is usable for embedding extraction by
            # the small-det loader in _load_faces.
            enroll_req = g.get("_enroll_request")
            if enroll_req is not None:
                try:
                    now = time.time()
                    last_ts = float(enroll_req.get("last_saved_ts") or 0.0)
                    if (now - last_ts) >= float(enroll_req.get("interval_s") or 1.0):
                        face_required = bool(enroll_req.get("require_face", True))
                        face_results_now = g.get("_face_results") or []
                        if (not face_required) or len(face_results_now) > 0:
                            pid = str(enroll_req.get("pid") or "unknown")
                            pid_dir = ROOT / "faces" / pid
                            pid_dir.mkdir(parents=True, exist_ok=True)
                            ts_label = time.strftime("%Y%m%d_%H%M%S")
                            seq = len(enroll_req.get("saved_paths") or [])
                            out_path = pid_dir / f"enroll_{ts_label}_{seq:02d}.jpg"
                            ok_w = cv2.imwrite(str(out_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
                            if ok_w:
                                enroll_req["saved_paths"] = (enroll_req.get("saved_paths") or []) + [str(out_path)]
                                enroll_req["remaining"] = int(enroll_req.get("remaining") or 0) - 1
                                enroll_req["last_saved_ts"] = now
                                print(f"[iris_video] enroll: saved {out_path.name} ({enroll_req['remaining']} remaining)", file=sys.stderr, flush=True)
                                if enroll_req["remaining"] <= 0:
                                    before_count = int(enroll_req.get("known_count_before") or 0)
                                    after_count = before_count
                                    engine_ref = g.get("_insight_face")
                                    try:
                                        if engine_ref is not None:
                                            after_count = int(engine_ref.update_known_faces())
                                    except Exception as _ue:
                                        print(f"[iris_video] update_known_faces error: {_ue!r}", file=sys.stderr, flush=True)
                                    g["_enroll_result"] = {
                                        "ok": True,
                                        "pid": pid,
                                        "saved_paths": list(enroll_req.get("saved_paths") or []),
                                        "known_count_before": before_count,
                                        "known_count_after": after_count,
                                        "duration_s": now - float(enroll_req.get("started_ts") or now),
                                    }
                                    g["_enroll_request"] = None
                                    print(f"[iris_video] enroll: complete pid={pid} known_count {before_count} -> {after_count}", file=sys.stderr, flush=True)
                            else:
                                print(f"[iris_video] enroll: cv2.imwrite failed for {out_path}", file=sys.stderr, flush=True)
                except Exception as _ee:
                    print(f"[iris_video] enroll error: {_ee!r}", file=sys.stderr, flush=True)

            # Phase 2 — expression detection (MediaPipe-backed). Throttled
            # because it runs face mesh per call. Stores dominant label on _g
            # for orb snapshot (current_person.expression) and annotator overlay.
            et = g.get("_expression_detector")
            if et is not None and getattr(et, "available", False):
                if frame_idx % expr_every_n == 0:
                    try:
                        expr = et.detect_expression(frame)
                        if expr:
                            new_expr = str(expr.get("dominant", "") or "")
                            prev_expr = str(g.get("_current_expression") or "")
                            g["_current_expression"] = new_expr
                            # Fire on transition only.
                            if new_expr and new_expr != prev_expr:
                                try:
                                    bus = g.get("_signal_bus")
                                    if bus is not None:
                                        bus.fire("expression_changed",
                                                 data={"from": prev_expr, "to": new_expr},
                                                 priority="low")
                                except Exception:
                                    pass
                    except Exception as _ee:
                        pass

            # Phase 2 — attention / gaze. eye_tracker.get_attention_state returns
            # one of focused/distracted/away/absent. _looking_at_screen drives
            # the "should I speak" gate downstream. Heavily throttled (~0.5fps)
            # since the orb just polls snapshot at 5s anyway.
            ez = g.get("_eye_tracker")
            if ez is not None and getattr(ez, "available", False):
                if frame_idx % attn_every_n == 0:
                    try:
                        prev_attn = str(g.get("_attention_state") or "")
                        new_attn = ez.get_attention_state(frame)
                        g["_attention_state"] = new_attn
                        g["_looking_at_screen"] = bool(ez.is_looking_at_screen(frame))
                        if getattr(ez, "calibrated", False):
                            g["_gaze_region"] = ez.get_gaze_region(frame) or ""
                        # Fire on transition only.
                        if new_attn and new_attn != prev_attn:
                            try:
                                bus = g.get("_signal_bus")
                                if bus is not None:
                                    bus.fire("attention_changed",
                                             data={"from": prev_attn, "to": new_attn},
                                             priority="low")
                            except Exception:
                                pass
                    except Exception:
                        pass

            try:
                annotated = _annotate(frame, g.get("_face_results"), g)
            except Exception as _ae:
                print(f"[iris_video] annotate error: {_ae!r}", file=sys.stderr, flush=True)
                annotated = frame

            try:
                _push_frame(annotated)
            except Exception:
                pass

            time.sleep(interval)
        except Exception as e:
            print(f"[iris_video] loop error: {e!r}", file=sys.stderr, flush=True)
            time.sleep(2.0)
            try:
                cap.release()
            except Exception:
                pass
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)


def _eager_init_engines() -> None:
    """Boot all engines in a background thread so the wake word detector is
    listening from process spawn (not from first voice.next_input call).

    The MCP server itself responds to the initialize handshake immediately
    via the main thread. Engine init is parallel; if it takes ~10s for
    Kokoro + Whisper + openWakeWord to load, that's fine — the user can't
    say "hey iris" any faster than that anyway, and subsequent calls are
    instant.

    Also starts the FastAPI orb shim AFTER TTS comes up (so /api/v1/tts/*
    delegations have a real engine to call).
    """
    try:
        tts = _ensure_tts()
        _ensure_stt()
        _ensure_wake()
        print("[iris_runtime] all engines online — listening", file=sys.stderr, flush=True)

        # Phase 1 vision — InsightFace + per-frame capture thread. Failures are
        # non-fatal: the orb camera tab falls back to raw cv2 frames without
        # face overlays. First run downloads buffalo_l (~280 MB) and warms up
        # cuDNN (~60-90s); cached after.
        try:
            from brain.insight_face_engine import bootstrap_insight_face
            print("[iris_runtime] booting InsightFace (first-run downloads ~280MB + 60-90s cudnn warmup)...", file=sys.stderr, flush=True)
            engine = bootstrap_insight_face(_g)
            if engine is not None and getattr(engine, "available", False):
                print(f"[iris_runtime] InsightFace ready (provider={engine.provider()}, known_people={engine.known_count()})", file=sys.stderr, flush=True)
                threading.Thread(
                    target=_iris_video_capture_loop,
                    args=(_g,),
                    daemon=True,
                    name="iris-video-capture",
                ).start()
                print("[iris_runtime] video capture thread started (15fps capture, 5fps face detect)", file=sys.stderr, flush=True)
            else:
                print("[iris_runtime] InsightFace not available — camera will serve raw frames without overlays", file=sys.stderr, flush=True)
        except Exception as _ve:
            print(f"[iris_runtime] vision phase 1 skipped: {_ve!r}", file=sys.stderr, flush=True)

        # Phase 2 perception — expression + eye/attention. MediaPipe-backed,
        # both modules degrade to .available=False if MP isn't installed; the
        # capture loop null-checks before calling. Calibration for gaze is
        # optional (POST /api/v1/camera/calibrate_gaze); without it we still
        # get attention_state + looking_at_screen from face geometry.
        try:
            from brain.expression_detector import bootstrap_expression_detector
            bootstrap_expression_detector(_g)
        except Exception as _ee:
            print(f"[iris_runtime] expression_detector skipped: {_ee!r}", file=sys.stderr, flush=True)
        try:
            from brain.eye_tracker import bootstrap_eye_tracker
            bootstrap_eye_tracker(_g)
        except Exception as _ye:
            print(f"[iris_runtime] eye_tracker skipped: {_ye!r}", file=sys.stderr, flush=True)

        # Phase 3 — memory + concept graph + journal.
        # iris_memory: JSONL-backed manual memory store. mem0/LLM extraction
        #   deferred until Phase 4 wires an LLM endpoint.
        # concept_graph: load existing graph if present, else start empty.
        #   bootstrap_from_existing_memory uses Mistral for concept extraction;
        #   skip that for now and let the graph fill via direct add_node calls.
        # journal: JSONL-backed; module is stateless and reads BASE_DIR from _g.
        try:
            from brain.iris_memory import bootstrap_iris_memory
            bootstrap_iris_memory(_g)
        except Exception as _me:
            print(f"[iris_runtime] iris_memory skipped: {_me!r}", file=sys.stderr, flush=True)
        try:
            from brain.concept_graph import ConceptGraph
            _cg = ConceptGraph(ROOT)
            _g["_concept_graph"] = _cg
            print(f"[iris_runtime] concept_graph ready (nodes={len(_cg.nodes)}, edges={len(_cg.edges)})", file=sys.stderr, flush=True)
        except Exception as _cge:
            print(f"[iris_runtime] concept_graph skipped: {_cge!r}", file=sys.stderr, flush=True)
        try:
            (ROOT / "state").mkdir(parents=True, exist_ok=True)
            print("[iris_runtime] journal ready (state/journal.jsonl)", file=sys.stderr, flush=True)
        except Exception as _je:
            print(f"[iris_runtime] journal dir setup failed: {_je!r}", file=sys.stderr, flush=True)

        # Phase 4 — chat + shared transcript. Configure the on-disk paths so
        # all writers (HTTP shim, voice tools, chat_reply) hit the same files.
        try:
            from brain import iris_transcript as _it
            from brain import iris_chat as _ic
            _it.configure(ROOT)
            _ic.configure(ROOT)
            print("[iris_runtime] chat + transcript ready", file=sys.stderr, flush=True)
        except Exception as _ce:
            print(f"[iris_runtime] chat setup failed: {_ce!r}", file=sys.stderr, flush=True)

        # Phase 5 — full brain/ bootstrap sweep. Wires mood, signal_bus, anchor
        # moments, identity_stability, app_discoverer, voice_mood_detector,
        # expression_calibrator, correction_handler, question_engine, feature_flags,
        # skill_sandbox, connectivity. Starts heartbeat thread (~5s mood tick).
        # Per-subsystem failures are logged but don't abort the sweep.
        try:
            from brain.iris_bootstrap import bootstrap_all
            bootstrap_all(_g, ROOT)
        except Exception as _be:
            print(f"[iris_runtime] iris_bootstrap failed: {_be!r}", file=sys.stderr, flush=True)

        # Filler audio clips — pre-rendered Kokoro phrases played via direct
        # sounddevice on the voice_next_input return path. Perceived-latency
        # cover during CC turn startup + Claude inference. ~30-60ms fire
        # latency, no Kokoro contention. Non-fatal if state/fillers/ is empty
        # (just disables the feature — STT path still works).
        try:
            from brain import filler_player as _fp
            _fp.load(ROOT / "state")
        except Exception as _fle:
            print(f"[iris_runtime] filler_player load failed (non-fatal): {_fle!r}", file=sys.stderr, flush=True)

        # Orb HTTP shim — gives apps/ava-control/ a backend to talk to on :5876.
        from brain.orb_http import start as _start_orb_http
        _start_orb_http(_g, ROOT, tts=tts)

        # Handoff persistence — read any existing handoff on startup (so the
        # rest of bootstrap can see "what was true last session"), then start
        # a periodic writer so even an ungraceful kill loses at most ~5 min
        # of session state. Plus an atexit handler for graceful shutdowns
        # (Ctrl+C, SIGTERM-style kills that allow cleanup to run).
        #
        # This was the gap Zeke flagged 2026-05-11 — pre-restart "save what's
        # important to memory" was manual. brain/handoff.py existed but
        # nothing was wiring it.
        try:
            from brain import handoff as _handoff
            existing = _handoff.read_handoff(ROOT)
            if existing:
                _g["_prior_handoff"] = existing
                print(
                    f"[iris_runtime] prior handoff loaded "
                    f"(reason={existing.get('reason', '?')}, "
                    f"written={existing.get('written_iso', '?')})",
                    file=sys.stderr, flush=True,
                )
            else:
                print("[iris_runtime] no prior handoff on disk (first boot or clean state)", file=sys.stderr, flush=True)

            # Periodic snapshot — every 5 min, cheap write_handoff (no LLM).
            # The richer write_handoff_with_summary is reserved for clean
            # shutdown + context-threshold triggers from elsewhere.
            def _handoff_periodic_loop() -> None:
                while True:
                    time.sleep(300.0)  # 5 minutes
                    try:
                        _handoff.write_handoff(_g, ROOT)
                    except Exception as _he:
                        print(f"[handoff periodic] write failed: {_he!r}", file=sys.stderr, flush=True)
            threading.Thread(
                target=_handoff_periodic_loop,
                daemon=True,
                name="iris-handoff-periodic",
            ).start()

            # atexit — fires on graceful shutdown (clean Python exit, SIGTERM
            # on Windows when the process is given a chance to clean up).
            # Does NOT fire on Stop-Process -Force; that's why the periodic
            # writer is the primary defense.
            import atexit as _atexit
            def _handoff_atexit() -> None:
                try:
                    _handoff.write_handoff(_g, ROOT)
                    print("[iris_runtime] handoff written at shutdown", file=sys.stderr, flush=True)
                except Exception as _ae:
                    print(f"[handoff atexit] failed: {_ae!r}", file=sys.stderr, flush=True)
            _atexit.register(_handoff_atexit)
            print("[iris_runtime] handoff persistence wired (5min periodic + atexit)", file=sys.stderr, flush=True)
        except Exception as _hbe:
            print(f"[iris_runtime] handoff wiring failed (non-fatal): {_hbe!r}", file=sys.stderr, flush=True)

        # Attention sources — autonomous watchers that emit channel events
        # for incoming sibling letters, orb chat requests, mood spikes, etc.
        # Each is a daemon thread that polls one event surface and emits via
        # brain/iris_channel.emit() when something new appears. Honours the
        # body_pause_flag kill-switch. Stop hook fallback still works if a
        # source thread silently dies (defense in depth).
        try:
            from brain import iris_attention_sources
            iris_attention_sources.start_all(_g, ROOT)
        except Exception as _ase:
            print(f"[iris_runtime] attention sources skipped (non-fatal — Stop hook fallback still works): {_ase!r}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[iris_runtime] eager engine init failed: {e!r}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    print("[iris_runtime] starting MCP server on stdio...", file=sys.stderr, flush=True)
    threading.Thread(target=_eager_init_engines, daemon=True, name="iris-eager-init").start()
    mcp.run()
