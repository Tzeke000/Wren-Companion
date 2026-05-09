"""Reproduce Ava's tts_worker OutputStream config on VAIO3, verify whether
low-latency / blocksize / channel-count is what's dropping the audio at B3.

Compares 3 config flavors:
    A: tts_worker actual config — samplerate=24000, channels=1, dtype=float32,
       blocksize=2048, latency='low'   (matches brain/tts_worker.py:588-594)
    B: same but latency='high'
    C: same but latency unset (default)
    D: same as A but channels=2 (stereo) — covers Voicemeeter-prefers-stereo theory
    E: same as A but blocksize=0 (let driver pick)

For each, push 2s of 660 Hz tone via sd.OutputStream while concurrently
recording B3. Report capture peak.
"""
from __future__ import annotations

import threading
import time
import numpy as np
import sounddevice as sd


def find_idx(name, kind):
    nl = name.lower()
    key = f"max_{kind}_channels"
    for i, d in enumerate(sd.query_devices()):
        if nl in (d.get("name") or "").lower() and (d.get(key) or 0) > 0:
            return i
    return None


def trial(label, *, samplerate, channels, dtype, blocksize, latency):
    vaio3 = find_idx("Voicemeeter VAIO3 Input", "output")
    b3 = find_idx("Voicemeeter Out B3", "input")

    dur_s = 1.5
    n = int(samplerate * dur_s)
    t = np.linspace(0, dur_s, n, endpoint=False)
    mono = (0.4 * np.sin(2 * np.pi * 660.0 * t)).astype(np.float32)
    if channels == 2:
        audio = np.stack([mono, mono], axis=1)
    else:
        audio = mono.reshape(-1, 1)

    holder = {}
    def _record():
        holder["data"] = sd.rec(int(44100 * (dur_s + 0.5)),
                                 samplerate=44100, channels=1, dtype="float32",
                                 device=b3)
        sd.wait()
    rec_thread = threading.Thread(target=_record, daemon=True)
    rec_thread.start()
    time.sleep(0.2)

    kwargs = dict(samplerate=samplerate, channels=channels, dtype=dtype,
                  device=vaio3)
    if blocksize is not None:
        kwargs["blocksize"] = blocksize
    if latency is not None:
        kwargs["latency"] = latency

    try:
        s = sd.OutputStream(**kwargs)
        s.start()
        # Write in chunks like tts_worker does
        bs = blocksize if blocksize else 2048
        idx = 0
        n_samples = audio.shape[0]
        while idx < n_samples:
            chunk = audio[idx:idx + bs]
            if chunk.shape[0] < bs:
                pad = np.zeros((bs - chunk.shape[0], channels), dtype=np.float32)
                chunk = np.concatenate([chunk, pad])
            s.write(chunk)
            idx += bs
        s.stop()
        s.close()
    except Exception as e:
        print(f"  trial err: {e!r}")
        rec_thread.join()
        return

    rec_thread.join()
    flat = np.asarray(holder.get("data", np.zeros(1))).flatten()
    peak = float(np.abs(flat).max())
    print(f"  {label:<60} peak={peak:.4f}  {'PASS' if peak >= 0.05 else 'FAIL'}")


print("=== Voicemeeter VAIO3 → B3 OutputStream config probe ===\n")
print("Trials (matches Ava tts_worker config in brain/tts_worker.py:588-594):\n")

trial("A: ava actual (sr=24k ch=1 bs=2048 lat=low)",
      samplerate=24000, channels=1, dtype="float32", blocksize=2048, latency="low")

trial("B: ava but lat=high",
      samplerate=24000, channels=1, dtype="float32", blocksize=2048, latency="high")

trial("C: ava but lat=default",
      samplerate=24000, channels=1, dtype="float32", blocksize=2048, latency=None)

trial("D: ava but channels=2 (stereo)",
      samplerate=24000, channels=2, dtype="float32", blocksize=2048, latency="low")

trial("E: ava but blocksize=0 (driver-chosen)",
      samplerate=24000, channels=1, dtype="float32", blocksize=0, latency="low")

trial("F: ava but blocksize=None",
      samplerate=24000, channels=1, dtype="float32", blocksize=None, latency="low")

trial("G: ava but sr=48000 (Voicemeeter native?)",
      samplerate=48000, channels=1, dtype="float32", blocksize=2048, latency="low")

trial("H: ava but sr=48000 stereo",
      samplerate=48000, channels=2, dtype="float32", blocksize=2048, latency="low")
