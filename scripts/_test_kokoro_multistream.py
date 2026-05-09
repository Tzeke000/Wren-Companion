"""Test the multi-stream pattern Ava's tts_worker uses: open one OutputStream
each to Speakers, CABLE Input, and Voicemeeter VAIO3 Input, then write the
same chunks to all three in lockstep. Verify B3 capture in this scenario.

This isolates whether it's the multi-stream write loop (vs single-stream) that
causes Kokoro audio to drop at VAIO3."""
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


def trial(label, devices_list):
    """devices_list = list of (name, kind) tuples to open OutputStream against.
    Resolves indexes, opens streams, writes 1.5s of 660Hz tone in 2048-sample
    chunks, monitors B3 capture, reports peak."""
    samplerate = 24000  # Kokoro native
    channels = 1
    blocksize = 2048
    dur_s = 1.5
    n = int(samplerate * dur_s)
    t = np.linspace(0, dur_s, n, endpoint=False)
    audio = (0.4 * np.sin(2 * np.pi * 660.0 * t)).astype(np.float32).reshape(-1, 1)

    b3 = find_idx("Voicemeeter Out B3", "input")

    holder = {}
    def _record():
        holder["data"] = sd.rec(int(44100 * (dur_s + 1.0)),
                                 samplerate=44100, channels=1, dtype="float32",
                                 device=b3)
        sd.wait()
    rec_thread = threading.Thread(target=_record, daemon=True)
    rec_thread.start()
    time.sleep(0.2)

    streams = []
    open_labels = []
    for name, kind in devices_list:
        idx = find_idx(name, "output")
        if idx is None:
            print(f"  device not found: {name}")
            continue
        try:
            s = sd.OutputStream(
                samplerate=samplerate, channels=channels, dtype="float32",
                blocksize=blocksize, latency="low", device=idx,
            )
            s.start()
            streams.append(s)
            open_labels.append(name)
        except Exception as e:
            print(f"  open {name} failed: {e!r}")

    # Write chunks to ALL streams in lockstep (same as tts_worker)
    idx = 0
    n_samples = audio.shape[0]
    drops = []
    while idx < n_samples:
        end = min(idx + blocksize, n_samples)
        chunk = audio[idx:end]
        if chunk.shape[0] < blocksize:
            pad = np.zeros((blocksize - chunk.shape[0], 1), dtype=np.float32)
            chunk = np.concatenate([chunk, pad])
        for s in streams:
            try:
                s.write(chunk)
            except Exception as e:
                drops.append(repr(e)[:80])
        idx += blocksize

    for s in streams:
        try:
            s.stop(); s.close()
        except Exception:
            pass

    rec_thread.join()
    flat = np.asarray(holder.get("data", np.zeros(1))).flatten()
    peak = float(np.abs(flat).max())
    rms = float(np.sqrt(np.mean(flat ** 2))) if flat.size else 0.0
    drops_str = f" drops={len(drops)}" if drops else ""
    print(f"  {label:<55} streams={len(streams)} peak={peak:.4f} rms={rms:.4f}{drops_str}")


print("=== Multi-stream OutputStream → B3 probe ===\n")

trial("V1: VAIO3 only", [("Voicemeeter VAIO3 Input", "output")])
trial("V2: VAIO3 + CABLE", [("Voicemeeter VAIO3 Input", "output"),
                            ("CABLE Input", "output")])
trial("V3: VAIO3 + Speakers", [("Voicemeeter VAIO3 Input", "output"),
                                ("Speakers (Realtek", "output")])
trial("V4: All three (Ava actual)", [("Speakers (Realtek", "output"),
                                       ("CABLE Input", "output"),
                                       ("Voicemeeter VAIO3 Input", "output")])
trial("V5: Speakers + CABLE only (no VAIO3)", [("Speakers (Realtek", "output"),
                                                 ("CABLE Input", "output")])
