"""scripts/capture_ava_tts.py — verify Ava's TTS reaches CABLE Output.

Records from "CABLE Output" (the listening side of the VB-CABLE pair Ava
plays her TTS to in addition to speakers), reports peak amplitude.

Usage:
    py -3.11 scripts/capture_ava_tts.py --seconds 8

Run this concurrent with an inject_transcript that triggers TTS, e.g.
in another shell:
    curl -m 60 -X POST http://127.0.0.1:5876/api/v1/debug/inject_transcript \\
         -H "Content-Type: application/json" \\
         -d '{"text":"hi","wake_source":"test","speak":true,"as_user":"claude_code","wait_for_audio":true,"timeout_seconds":50}'

Output: peak amplitude, mean energy. Peak > 0.05 means audio is flowing
through the cable. Peak < 0.01 means silent — TTS not actually playing
to CABLE Input, or wrong device.
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import sounddevice as sd


def find_device(name_substr: str, kind: str) -> int | None:
    target_key = f"max_{kind}_channels"
    name_lower = name_substr.lower()
    for i, dev in enumerate(sd.query_devices()):
        if name_lower in dev.get("name", "").lower() and dev.get(target_key, 0) > 0:
            return i
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--device", default="CABLE Output")
    p.add_argument("--rate", type=int, default=44100)
    args = p.parse_args()

    in_idx = find_device(args.device, "input")
    if in_idx is None:
        print(f"[capture] FAIL: device {args.device!r} not found")
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                print(f"  available input: {i}: {d['name']!r}")
        return 1
    dev = sd.query_devices(in_idx)
    print(f"[capture] device {in_idx}: {dev['name']!r}")
    print(f"[capture] recording {args.seconds:.1f}s at {args.rate} Hz...")

    captured = sd.rec(int(args.rate * args.seconds), samplerate=args.rate, channels=1, dtype="float32", device=in_idx)
    sd.wait()
    flat = np.asarray(captured).flatten()
    if flat.size == 0:
        print("[capture] FAIL: empty buffer")
        return 1
    peak = float(np.abs(flat).max())
    rms = float(np.sqrt(np.mean(flat ** 2)))

    # Find the loudest 100 ms window
    window = max(1, int(args.rate * 0.1))
    if flat.size > window:
        max_window_peak = max(float(np.abs(flat[i:i+window]).max()) for i in range(0, flat.size - window, window))
    else:
        max_window_peak = peak

    print(f"[capture] peak={peak:.4f}  rms={rms:.6f}  max_window_peak={max_window_peak:.4f}")
    if peak < 0.01:
        print("[capture] RESULT: SILENT — TTS not reaching CABLE Output")
        return 1
    if peak < 0.05:
        print("[capture] RESULT: VERY QUIET — possibly background noise, not actual TTS")
        return 1
    print(f"[capture] RESULT: AUDIO PRESENT (peak {peak:.3f} ≥ 0.05)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
