"""scripts/verify_tts_b4.py — Task B4: TTS end-to-end verification.

Confirms Ava's TTS works:
    1. Kokoro reports loaded via /api/v1/debug/full subsystem_health.kokoro_loaded
    2. Trigger inject_transcript with speak=true
    3. Capture from CABLE Output (Ava's TTS plays to both speakers and CABLE Input)
    4. Verify the recording has audible content (peak > 0.05)

Usage:
    py -3.11 scripts/verify_tts_b4.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("AVA_OPERATOR_URL", "http://127.0.0.1:5876").rstrip("/")


def _post(path: str, body: dict, timeout: float = 90.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_device(name_substr: str, kind: str) -> int | None:
    target_key = f"max_{kind}_channels"
    name_lower = name_substr.lower()
    for i, dev in enumerate(sd.query_devices()):
        if name_lower in dev.get("name", "").lower() and dev.get(target_key, 0) > 0:
            return i
    return None


def main() -> int:
    print("[B4] === TTS end-to-end verification ===")

    # Step 1: kokoro_loaded flag
    print("\n[B4-1] Checking kokoro_loaded snapshot flag...")
    try:
        full = _get("/api/v1/debug/full")
    except Exception as e:
        print(f"  FAIL: cannot reach Ava operator HTTP: {e!r}")
        return 1
    health = full.get("subsystem_health", {})
    kokoro = health.get("kokoro_loaded")
    print(f"  subsystem_health.kokoro_loaded = {kokoro}")
    if not kokoro:
        print(f"  FAIL: kokoro_loaded flag not True. Bug fix didn't take effect, or Kokoro init failed.")
        return 1
    print(f"  PASS: flag publishes correctly")

    # Step 2: Find CABLE Output device
    print("\n[B4-2] Finding CABLE Output input device...")
    in_idx = _find_device("CABLE Output", "input")
    if in_idx is None:
        print("  FAIL: CABLE Output device not present. Voicemeeter Potato includes basic VB-CABLE pair — check install.")
        return 1
    dev = sd.query_devices(in_idx)
    print(f"  device {in_idx}: {dev['name']!r}  rate={dev.get('default_samplerate', '?')}")

    # Step 3: Start recording in a thread, then trigger TTS
    # Long capture window — Ava on cold-load + dual_brain swap can take 60–90 s
    # before TTS even starts.
    print("\n[B4-3] Recording CABLE Output for 30s while triggering TTS...")
    sample_rate = 44100
    duration_s = 30.0
    captured: list[float] = []

    def _record():
        try:
            data = sd.rec(int(sample_rate * duration_s), samplerate=sample_rate, channels=1, dtype="float32", device=in_idx)
            sd.wait()
            captured.extend(np.asarray(data).flatten().tolist())
        except Exception as e:
            print(f"  record thread error: {e!r}")

    rec_thread = threading.Thread(target=_record, daemon=True)
    rec_thread.start()
    time.sleep(0.5)  # let recording settle

    print("  triggering inject_transcript with speak=True...")
    t0 = time.time()
    r = None
    try:
        r = _post(
            "/api/v1/debug/inject_transcript",
            {
                "text": "say a short test sentence please",
                "wake_source": "test_b4",
                "wait_for_audio": True,
                "speak": True,
                "as_user": "claude_code",
                "timeout_seconds": 110.0,
            },
            timeout=180.0,
        )
    except Exception as e:
        # HTTP may time out even though Ava's TTS still plays out via the
        # capture buffer. Don't bail — let the recording finish, then check
        # for audio. The recording is the actual proof of TTS working.
        print(f"  WARN: inject_transcript timed out: {e!r} — letting capture finish anyway")

    if r is not None:
        print(f"  reply: {(r.get('reply_text') or '')[:120]!r}")
        print(f"  total_ms={r.get('total_ms')}  tts.attempted={r.get('tts',{}).get('attempted')}  tts.played={r.get('tts',{}).get('played')}")
    else:
        print(f"  HTTP did not complete; relying on capture-buffer evidence")

    rec_thread.join(timeout=duration_s + 5.0)

    # Step 4: Analyze capture
    print("\n[B4-4] Analyzing captured audio...")
    if not captured:
        print("  FAIL: empty capture buffer")
        return 1
    arr = np.asarray(captured)
    peak = float(np.abs(arr).max())
    rms = float(np.sqrt(np.mean(arr ** 2)))
    print(f"  peak={peak:.4f}  rms={rms:.6f}  samples={arr.size}")

    verdict = "PASS" if peak >= 0.05 else "FAIL"
    print(f"\n[B4] {verdict}  (peak {peak:.4f} {'>=' if peak >= 0.05 else '<'} 0.05 threshold)")
    if verdict == "FAIL":
        print("  Diagnostic: TTS may have played but didn't reach CABLE Input. Check tts_worker dual-route config.")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
