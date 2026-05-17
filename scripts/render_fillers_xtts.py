"""Pre-render filler audio clips via XTTS-v2 (the production voice).

Replaces scripts/render_fillers.py (Kokoro-based) for the post-XTTS-switch
fillers. The original render_fillers.py still works but produces clips in
Kokoro Bella voice — switching voice mid-conversation between Kokoro
fillers and XTTS replies sounds wrong. This script renders in the same
XTTS voice (cloned from Kokoro Bella reference) used in production.

Voice: tts_models/multilingual/multi-dataset/xtts_v2, speaker_wav =
D:\\Wren-Companion\\.tmp\\voice_test\\kokoro_samples\\reference.wav
(same reference the production xtts_server.py uses).

Sample rate: 24000 (XTTS-v2 output rate, matches filler_player.py's _SR).

RUN VIA .venv_xtts:
    "D:\\Wren-Companion\\.venv_xtts\\Scripts\\python.exe" scripts\\render_fillers_xtts.py

Per Anthropic-mobile-app research 2026-05-17 (cookbook + the-decoder):
they use ElevenLabs sentence-streaming TTS at ~150ms first-audio, no
audible fillers — they rely on raw low latency. Our XTTS hits 400-800ms
warm on RTX 3060, so we still benefit from filler cover at chunk
boundaries. Buckets and phrasing per Zeke 2026-05-17:
  - ack: short acknowledgments (response received)
  - hesitate-long: "let me think" — for cognition-without-lookup
  - compute: legacy "let me pull that up" — keeps existing behavior for
    cases that don't specifically need think vs lookup distinction
  - lookup (NEW): "let me look that up" — for tool-using / research
  - think (NEW): "give me a moment" / "let me think for a second" —
    for cognition-without-lookup
"""
from __future__ import annotations

import os
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "state" / "fillers"
SR = 24000
SPEAKER_WAV = r"D:\Wren-Companion\.tmp\voice_test\kokoro_samples\reference.wav"

# Per Zeke 2026-05-17 + Anthropic-mobile-app research: keep minimal.
# Existing buckets re-rendered in XTTS, two new buckets for semantic
# coverage (lookup vs think). Total 14 clips across 5 buckets. Variety
# beyond this is decorative — agent research said real low-latency
# pipelines (ElevenLabs Flash) skip fillers entirely.
CLIPS: dict[str, list[str]] = {
    "ack": [
        "Mm.",
        "Yeah.",
        "Okay.",
    ],
    "hesitate-long": [
        "Hmm, okay.",
        "Let me think.",
        "Give me a sec.",
    ],
    "compute": [
        "Hold on, working it out.",
        "Let me pull that up.",
    ],
    # NEW per Zeke 2026-05-17 — for tool-using / research scenarios
    "lookup": [
        "Let me look that up.",
        "Checking on that now.",
        "One sec, pulling it up.",
    ],
    # NEW — for cognition-without-lookup (thinking through, not researching)
    "think": [
        "Let me think for a second.",
        "Sitting with that for a moment.",
        "Working through it.",
    ],
}


def _save_wav(path: Path, audio_float: np.ndarray, sample_rate: int) -> None:
    audio_float = np.clip(audio_float, -1.0, 1.0)
    pcm16 = (audio_float * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())


def _load_model():
    """Boot XTTS-v2 with the same patch xtts_server.py uses (force
    weights_only=False so the older checkpoint format loads under
    torch>=2.6's default-secure load).
    """
    import torch
    _orig_load = torch.load
    def _patched_load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return _orig_load(*args, **kwargs)
    torch.load = _patched_load
    os.environ["COQUI_TOS_AGREED"] = "1"

    from TTS.api import TTS
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[render_fillers_xtts] loading XTTS-v2 on {device}...", flush=True)
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
    print(f"[render_fillers_xtts] model ready", flush=True)
    return tts


def render() -> int:
    if not Path(SPEAKER_WAV).is_file():
        print(f"[render_fillers_xtts] FATAL: speaker_wav not found at {SPEAKER_WAV}",
              file=sys.stderr, flush=True)
        return 2

    tts = _load_model()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    skipped = 0
    for bucket, phrases in CLIPS.items():
        bucket_dir = OUT_DIR / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        for i, phrase in enumerate(phrases):
            out = bucket_dir / f"{i:02d}.wav"
            print(f"[render_fillers_xtts] {bucket}/{i:02d}: {phrase!r}", flush=True)
            try:
                # Use tts_to_file directly — matches xtts_server.py's legacy
                # fallback path, simpler than the streaming variant for
                # batch render. Each render is <5s so latency doesn't matter.
                tmp = bucket_dir / f"{i:02d}.xtts.wav"
                tts.tts_to_file(
                    text=phrase,
                    speaker_wav=SPEAKER_WAV,
                    language="en",
                    file_path=str(tmp),
                )
                # Read back, resample if needed (XTTS outputs 24000 by default
                # but we verify against SR explicitly), save in our canonical
                # PCM16 format.
                import soundfile as sf
                audio, sr = sf.read(str(tmp), dtype="float32")
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                if sr != SR:
                    print(f"  WARN sr={sr} != {SR}, resampling not implemented; skipping", flush=True)
                    tmp.unlink(missing_ok=True)
                    skipped += 1
                    continue
                _save_wav(out, audio, SR)
                duration = len(audio) / SR
                print(f"  -> {out.relative_to(ROOT)} ({duration:.2f}s, {audio.size} samples)", flush=True)
                tmp.unlink(missing_ok=True)
                saved += 1
            except Exception as e:
                print(f"  ERROR: {e!r} — skipping", flush=True)
                skipped += 1
                continue

    print(f"[render_fillers_xtts] done. saved={saved} skipped={skipped} -> {OUT_DIR}",
          flush=True)
    return 0 if saved > 0 else 1


if __name__ == "__main__":
    sys.exit(render())
