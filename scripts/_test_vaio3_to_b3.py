"""One-shot test: play 1s tone to Voicemeeter VAIO3 Input, record from
Voicemeeter Out B3, report peak. Verifies VM B3 routing without restarting Ava."""
import numpy as np
import sounddevice as sd

def find_idx(name_substr, kind):
    key = f"max_{kind}_channels"
    nl = name_substr.lower()
    for i, d in enumerate(sd.query_devices()):
        if nl in (d.get("name") or "").lower() and (d.get(key) or 0) > 0:
            return i
    return None

vaio3 = find_idx("Voicemeeter VAIO3 Input", "output")
b3 = find_idx("Voicemeeter Out B3", "input")
print(f"VAIO3 In idx={vaio3}, B3 Out idx={b3}")
if vaio3 is None or b3 is None:
    raise SystemExit(1)

sample_rate = 48000  # Voicemeeter usually runs at 48k
dur = 1.0
t = np.linspace(0, dur, int(sample_rate * dur), endpoint=False)
tone = (0.4 * np.sin(2 * np.pi * 660.0 * t)).reshape(-1, 1).astype(np.float32)

# Pre-arm capture
captured = sd.playrec(
    tone, samplerate=sample_rate, channels=1, dtype="float32",
    device=(b3, vaio3),
)
sd.wait()
flat = np.asarray(captured).flatten()
peak = float(np.abs(flat).max())
print(f"capture peak from B3: {peak:.4f}")
if peak >= 0.05:
    print("PASS — Voicemeeter VAIO3 → B3 routing works")
else:
    print("FAIL — VAIO3 not routed to B3 (peak too low)")
    print("Action needed: open Voicemeeter Potato, on the VAIO3 input strip enable the 'B3' button")
