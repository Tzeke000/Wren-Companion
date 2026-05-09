"""Trigger Ava TTS via /api/v1/tool_call (tts_say) and concurrently
capture from Voicemeeter Out B3. Diagnoses the f8 silent-capture issue."""
import json
import threading
import time
import urllib.request

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
    b3_idx = find_idx("Voicemeeter Out B3", "input")
    print(f"B3 idx={b3_idx}")
    if b3_idx is None:
        return 1

    # Start recording in a thread BEFORE triggering TTS
    rec_seconds = 12.0
    sample_rate = 44100
    print(f"recording {rec_seconds}s at {sample_rate}Hz from B3...")

    captured_holder = {}
    def _record():
        captured_holder["data"] = sd.rec(int(sample_rate * rec_seconds),
                                         samplerate=sample_rate,
                                         channels=1, dtype="float32",
                                         device=b3_idx)
        sd.wait()

    rec_thread = threading.Thread(target=_record, daemon=True)
    rec_thread.start()
    time.sleep(0.5)  # let recording prime

    # Trigger TTS via inject_transcript (simulates user saying "what time is it")
    payload = json.dumps({"text": "Hello Zeke, this is a TTS routing test for Voicemeeter B3 capture.",
                          "_tts_test": True}).encode("utf-8")
    req = urllib.request.Request(f"{OPERATOR}/api/v1/tts/say",
                                  data=payload,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"tts/say: {r.status} {r.read()[:200]}")
    except Exception as e:
        # Fallback: try inject_transcript so Ava generates a reply
        print(f"tts/say not available: {e!r}; falling back to inject_transcript")
        payload2 = json.dumps({"text": "what time is it", "wake_source": "manual"}).encode("utf-8")
        req2 = urllib.request.Request(f"{OPERATOR}/api/v1/inject_transcript",
                                       data=payload2,
                                       headers={"Content-Type": "application/json"},
                                       method="POST")
        try:
            with urllib.request.urlopen(req2, timeout=20) as r:
                print(f"inject: {r.status} {r.read()[:300]}")
        except Exception as e2:
            print(f"inject failed: {e2!r}")

    rec_thread.join()
    flat = np.asarray(captured_holder.get("data", np.zeros(1))).flatten()
    peak = float(np.abs(flat).max())
    rms = float(np.sqrt(np.mean(flat ** 2))) if flat.size else 0.0
    print(f"capture peak={peak:.4f} rms={rms:.4f} samples={flat.size}")
    if peak < 0.01:
        print("FAIL: silent capture")
        return 1
    print("PASS: TTS routing reaches B3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
