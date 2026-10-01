"""scripts/voice_emotion_ab.py — the PROVING test for round-2 fix 2.1 (emotion → prosody).

Asks the live StyleTTS2 mouth (:8769 /synth — WAV bytes, no playback) for the SAME sentence
with the same diffusion seed under several emotions, then measures each render:
duration (speed) and f0 median/std (pitch, expressiveness) with librosa.pyin.

Pass = the audio is measurably different in the expected directions:
  joy shorter than neutral shorter than sadness (speed), and joy vs sadness f0 medians differ.

    .venv\\Scripts\\python.exe scripts\\voice_emotion_ab.py [--text "..."] [--seed 7] [--emotions neutral,joy,sadness]
WAVs land in scratch/voice_ab_<emotion>.wav so Zeke can listen blind.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("WREN_VOICE_PORT", "8769"))
DEFAULT_TEXT = "I was not sure you would come back tonight, but here you are, and the room is lit again."


def synth(text: str, emotion: str, intensity: float, seed: int | None, rate: int = 24000) -> bytes:
    body = {"text": text, "rate": rate, "emotion": emotion, "intensity": intensity}
    if seed is not None:
        body["seed"] = seed
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/synth", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=240) as r:
        return r.read()


def analyse(wav_bytes: bytes) -> dict:
    import numpy as np
    import librosa
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        pcm = np.frombuffer(wf.readframes(n), dtype="<i2").astype(np.float32) / 32767.0
    dur = n / float(sr)
    f0, voiced, _ = librosa.pyin(pcm, fmin=70, fmax=450, sr=sr, frame_length=2048)
    v = f0[np.isfinite(f0)]
    return {"duration_s": round(dur, 3), "f0_median_hz": round(float(np.median(v)), 1) if v.size else None,
            "f0_std_hz": round(float(np.std(v)), 1) if v.size else None,
            "voiced_frames": int(v.size), "rms": round(float(np.sqrt(np.mean(pcm ** 2))), 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--intensity", type=float, default=1.0)
    ap.add_argument("--emotions", default="neutral,joy,sadness")
    args = ap.parse_args()
    emotions = [e.strip() for e in args.emotions.split(",") if e.strip()]
    os.makedirs(os.path.join(ROOT, "scratch"), exist_ok=True)
    results: dict[str, dict] = {}
    for emo in emotions:
        t0 = time.time()
        wav = synth(args.text, emo, args.intensity, args.seed)
        path = os.path.join(ROOT, "scratch", f"voice_ab_{emo}.wav")
        with open(path, "wb") as f:
            f.write(wav)
        res = analyse(wav)
        res["synth_s"] = round(time.time() - t0, 1)
        res["path"] = path
        results[emo] = res
        print(f"{emo:>10}: dur={res['duration_s']}s f0_med={res['f0_median_hz']}Hz f0_std={res['f0_std_hz']}Hz "
              f"rms={res['rms']} (synth {res['synth_s']}s)")
    ok = True
    if {"joy", "neutral", "sadness"} <= set(results):
        dj, dn, ds = (results[e]["duration_s"] for e in ("joy", "neutral", "sadness"))
        order_ok = dj < dn < ds
        f0j, f0s = results["joy"]["f0_median_hz"], results["sadness"]["f0_median_hz"]
        f0_ok = f0j is not None and f0s is not None and abs(f0j - f0s) >= 1.0
        print(f"duration order joy<neutral<sadness: {order_ok} ({dj} < {dn} < {ds})")
        print(f"f0 median differs joy vs sadness by >=1 Hz: {f0_ok} ({f0j} vs {f0s})")
        ok = order_ok and f0_ok
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
