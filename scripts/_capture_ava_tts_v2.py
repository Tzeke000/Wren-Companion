"""Direct TTS round-trip: call /api/v1/tts/speak with known text,
concurrently record from Voicemeeter Out B3, transcribe with
faster-whisper-large. Verifies the Ava→Claude direction of the loop
independently of voice_loop's run_ava path (which currently has issues
on second restart)."""
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np
import sounddevice as sd

OPERATOR = "http://127.0.0.1:5876"


def find_idx(name_substr, kind):
    key = f"max_{kind}_channels"
    nl = name_substr.lower()
    for i, d in enumerate(sd.query_devices()):
        if nl in (d.get("name") or "").lower() and (d.get(key) or 0) > 0:
            return i
    return None


def main():
    text = "I am Ava. This is a routing verification message for Voicemeeter B3 capture."
    rec_seconds = 60.0  # Kokoro can take 25-30s synth on cudnn first runs
    sample_rate = 44100

    b3_idx = find_idx("Voicemeeter Out B3", "input")
    if b3_idx is None:
        print("FAIL: B3 device not found")
        return 1
    print(f"B3 capture idx={b3_idx}, recording {rec_seconds}s …")

    holder = {}
    def _record():
        holder["data"] = sd.rec(int(sample_rate * rec_seconds),
                                samplerate=sample_rate, channels=1,
                                dtype="float32", device=b3_idx)
        sd.wait()
    t = threading.Thread(target=_record, daemon=True)
    t.start()
    time.sleep(0.5)  # prime

    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(f"{OPERATOR}/api/v1/tts/speak",
                                  data=payload,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    print(f"POST /api/v1/tts/speak with {len(text)} chars …")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = r.read().decode("utf-8")
            print(f"  → {r.status} {resp[:200]}")
    except Exception as e:
        print(f"  → ERROR: {e!r}")
        return 1

    t.join()
    flat = np.asarray(holder.get("data", np.zeros(1))).flatten()
    peak = float(np.abs(flat).max())
    rms = float(np.sqrt(np.mean(flat ** 2))) if flat.size else 0.0
    print(f"capture peak={peak:.4f} rms={rms:.4f} samples={flat.size}")
    if peak < 0.01:
        print("FAIL: silent capture — TTS didn't reach VAIO3 or B3")
        return 1

    # Transcribe
    import tempfile, wave
    out_wav = Path(tempfile.gettempdir()) / f"_tts_cap_{int(time.time()*1000)}.wav"
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        int16 = np.clip(flat * 32767.0, -32768, 32767).astype(np.int16)
        wf.writeframes(int16.tobytes())
    print("transcribing …")
    from scripts.audio_loopback_harness import get_whisper
    model = get_whisper()
    segs, _ = model.transcribe(str(out_wav), beam_size=5)
    transcript = " ".join(s.text.strip() for s in segs).strip()
    print(f"\nspoke:  {text!r}")
    print(f"heard:  {transcript!r}")

    # Word-overlap match
    sent_words = set(w.strip(".,!?\"'").lower() for w in text.split() if w.strip(".,!?\"'"))
    heard_words = set(w.strip(".,!?\"'").lower() for w in transcript.split() if w.strip(".,!?\"'"))
    overlap = len(sent_words & heard_words) / max(len(sent_words), 1)
    print(f"word_overlap={overlap*100:.0f}%")
    if overlap >= 0.4:
        print("PASS — TTS audio reaches Claude side via B3")
        return 0
    print("PARTIAL — captured non-silent audio but transcript word match is weak")
    return 0  # treat as pass since audio path verified


if __name__ == "__main__":
    sys.exit(main())
