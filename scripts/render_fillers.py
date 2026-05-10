"""Pre-render filler audio clips via Kokoro CUDA.

Run once after a Kokoro-stack install. Output: state/fillers/<bucket>/<n>.wav
Loaded into memory at iris_runtime startup by brain/filler_player.py and
played via sounddevice as a perceived-latency cover during Claude's inference.

Per the filler research (2026-05-10): hybrid pattern A+C — pre-cached audio
+ heuristic decider. Live Kokoro synth costs 400-700ms first phoneme even
warm; pre-rendered clips drop to ~30-60ms via direct sounddevice playback.

Voice: af_bella (Iris's locked Kokoro voice, distinct from Wren's amy and
Ava's af_heart). 24kHz mono PCM16. Hand-curate after rendering — Kokoro is
trained on long-form narration and handles emotional fillers unevenly.
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
VOICE = os.environ.get("AVA_KOKORO_VOICE_DEFAULT", "af_bella")

# Tonight-shippable subset per filler research: 8 clips across 3 buckets.
# Skip the short-hesitate and clarify-uncertain buckets for v1.
CLIPS: dict[str, list[str]] = {
    "ack": [
        "Mm.",
        "Yeah,",
        "Okay,",
    ],
    "hesitate-long": [
        "Hmm, okay,",
        "Let me think,",
        "Give me a sec,",
    ],
    "compute": [
        "Hold on, working it out.",
        "Let me pull that up.",
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


def render() -> int:
    from kokoro import KPipeline  # type: ignore

    device = "cpu"
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            device = "cuda"
    except Exception:
        pass

    print(f"[render_fillers] booting KPipeline (device={device}, voice={VOICE})...", flush=True)
    pipeline: Any = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device=device)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    skipped = 0
    for bucket, phrases in CLIPS.items():
        bucket_dir = OUT_DIR / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        for i, phrase in enumerate(phrases):
            print(f"[render_fillers] {bucket}/{i:02d}: {phrase!r}", flush=True)
            chunks: list[np.ndarray] = []
            try:
                for _gs, _ps, audio in pipeline(phrase, voice=VOICE, speed=1.0):
                    a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
                    if a is None or a.size == 0:
                        continue
                    chunks.append(a.astype(np.float32))
            except Exception as e:
                print(f"  ERROR synth: {e!r} — skipping", flush=True)
                skipped += 1
                continue
            if not chunks:
                print("  EMPTY — skipping", flush=True)
                skipped += 1
                continue
            full = np.concatenate(chunks)
            duration = len(full) / SR
            out = bucket_dir / f"{i:02d}.wav"
            _save_wav(out, full, SR)
            print(f"  -> {out.relative_to(ROOT)} ({duration:.2f}s, {full.size} samples)", flush=True)
            saved += 1

    print(f"[render_fillers] done. saved={saved} skipped={skipped} -> {OUT_DIR}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(render())
