"""Test VAIO3 routing at Kokoro's native sample rate (24000Hz)."""
import numpy as np
import sounddevice as sd

def find_idx(name, kind):
    key = f"max_{kind}_channels"
    nl = name.lower()
    for i, d in enumerate(sd.query_devices()):
        if nl in (d.get("name") or "").lower() and (d.get(key) or 0) > 0:
            return i
    return None

vaio3 = find_idx("Voicemeeter VAIO3 Input", "output")
b3 = find_idx("Voicemeeter Out B3", "input")
print(f"VAIO3={vaio3}, B3={b3}")
print(f"VAIO3 info: {sd.query_devices(vaio3)}")
print(f"B3 info: {sd.query_devices(b3)}")

# Test at multiple sample rates
for sr in (24000, 22050, 44100, 48000):
    print(f"\n--- sr={sr}Hz ---")
    t = np.linspace(0, 0.5, int(sr * 0.5), endpoint=False)
    tone = (0.4 * np.sin(2 * np.pi * 660.0 * t)).reshape(-1, 1).astype(np.float32)
    try:
        captured = sd.playrec(tone, samplerate=sr, channels=1, dtype="float32",
                              device=(b3, vaio3))
        sd.wait()
        flat = np.asarray(captured).flatten()
        peak = float(np.abs(flat).max())
        print(f"peak={peak:.4f}")
    except Exception as e:
        print(f"err: {e!r}")
