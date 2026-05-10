"""Filler audio player — perceived-latency cover during Claude inference.

Loads pre-rendered Kokoro filler clips at startup (from state/fillers/<bucket>/
*.wav, produced by scripts/render_fillers.py) and plays them via sounddevice
on the voice_next_input return path. Audio fires before Claude's first token
generates, masking the inference delay with humanlike disfluency.

Pattern A+C per filler research 2026-05-10: pre-cached audio + heuristic
decider. Direct sounddevice playback bypasses Kokoro on the hot path —
fire latency ~30-60ms vs ~400-700ms for live synth.

NOT a replacement for voice_say_chunk. Filler fires DURING the gap between
end-of-utterance and Claude's first sentence; voice_say_chunk delivers the
substantive reply once Claude has it.
"""
from __future__ import annotations

import random
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

_CLIPS_BY_BUCKET: dict[str, list[np.ndarray]] = {}
_SR = 24000
_LOAD_LOCK = threading.Lock()
_loaded = False
_last_play_ts = 0.0


def _load_wav(path: Path) -> Optional[np.ndarray]:
    try:
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            n_frames = w.getnframes()
            n_channels = w.getnchannels()
            data = w.readframes(n_frames)
            audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if n_channels != 1:
                audio = audio.reshape(-1, n_channels).mean(axis=1)
            if sr != _SR:
                # No resample in v1 — expect render script's 24kHz output.
                print(f"[filler_player] {path.name} sr={sr} != {_SR}, skipping")
                return None
            return audio
    except Exception as e:
        print(f"[filler_player] load error {path}: {e!r}")
        return None


def load(state_dir: Path) -> int:
    """Load all filler wavs into memory. Idempotent.

    Returns total clip count loaded across all buckets.
    """
    global _loaded
    with _LOAD_LOCK:
        if _loaded:
            return sum(len(v) for v in _CLIPS_BY_BUCKET.values())
        fillers_dir = state_dir / "fillers"
        if not fillers_dir.is_dir():
            print(f"[filler_player] no fillers dir at {fillers_dir} — disabled")
            _loaded = True
            return 0
        total = 0
        for bucket_dir in sorted(fillers_dir.iterdir()):
            if not bucket_dir.is_dir():
                continue
            bucket = bucket_dir.name
            clips: list[np.ndarray] = []
            for wav_path in sorted(bucket_dir.glob("*.wav")):
                audio = _load_wav(wav_path)
                if audio is not None and audio.size > 0:
                    clips.append(audio)
                    total += 1
            if clips:
                _CLIPS_BY_BUCKET[bucket] = clips
        _loaded = True
        bucket_summary = ", ".join(f"{k}={len(v)}" for k, v in _CLIPS_BY_BUCKET.items()) or "(none)"
        print(f"[filler_player] loaded {total} clips ({bucket_summary})")
        return total


def _choose_bucket(transcript: str) -> Optional[str]:
    """Pick a bucket (or None) based on transcript shape. ~5ms heuristic.

    Mirrors the research's tonight-shippable rules: long/thinky -> compute,
    short question -> ack, medium -> hesitate-long sometimes, short other
    -> ack sometimes, else nothing.
    """
    if not transcript:
        return None
    t = transcript.strip().lower()
    words = t.split()
    n = len(words)
    is_q = (
        "?" in t
        or any(t.startswith(w + " ") for w in ("what", "why", "how", "where", "when", "who", "is", "do", "can", "could", "would", "should", "are", "did"))
    )
    thinky_terms = ("why ", "how ", "explain", "difference between", "what's the")

    if n > 18 or any(k in t for k in thinky_terms):
        return "compute"
    if is_q and n < 6:
        return "ack"
    if n >= 6:
        return "hesitate-long" if random.random() < 0.6 else None
    return "ack" if random.random() < 0.4 else None


def maybe_play(transcript: str, min_gap_s: float = 0.5) -> Optional[str]:
    """Decide on a filler for `transcript` and start playing it asynchronously.

    Returns the bucket name actually played, or None if suppressed (no
    bucket chosen / dedup window / load failed / playback error). Always
    non-blocking — sounddevice.play() returns once the buffer is queued.
    """
    global _last_play_ts
    if not _loaded or not _CLIPS_BY_BUCKET:
        return None
    now = time.time()
    if (now - _last_play_ts) < min_gap_s:
        return None
    bucket = _choose_bucket(transcript or "")
    if not bucket:
        return None
    clips = _CLIPS_BY_BUCKET.get(bucket)
    if not clips:
        return None
    clip = random.choice(clips)
    try:
        import sounddevice as sd  # type: ignore
        sd.play(clip, _SR, blocking=False)
        _last_play_ts = now
        return bucket
    except Exception as e:
        print(f"[filler_player] play error: {e!r}")
        return None
