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
    result = stt.listen_session(max_seconds=60.0, silence_seconds=0.8)

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
def voice_status() -> dict:
    """Report which engines are loaded and what backends they're using.

    Useful for Iris (and the orb) to know whether the body is fully online.
    """
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
    _ensure_say_worker()
    depth_before = _say_queue.qsize()
    _say_queue.put((text, emotion, intensity))
    return {"ok": True, "queue_depth": depth_before}


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
    except Exception as e:
        print(f"[iris_runtime] eager engine init failed: {e!r}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    print("[iris_runtime] starting MCP server on stdio...", file=sys.stderr, flush=True)
    threading.Thread(target=_eager_init_engines, daemon=True, name="iris-eager-init").start()
    mcp.run()
