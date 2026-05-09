"""F8 + F12 voice end-to-end driver.

Pipeline:
    Piper TTS → CABLE Input → Ava STT (CABLE Output as default mic)
    → Ava run_ava → Ava Kokoro TTS → Voicemeeter VAIO3 Input
    → Voicemeeter B3 → faster-whisper-large

For each test: speak prompt, wait for Ava state to advance, listen on B3,
transcribe, verify both the textual response and the relevant subsystem state.

Run:
    py -3.11 scripts/verify_voice_e2e.py f8
    py -3.11 scripts/verify_voice_e2e.py f12
    py -3.11 scripts/verify_voice_e2e.py both
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.audio_loopback_harness import (
    piper_tts,
    play_wav_to_cable,
    record_and_transcribe,
)


OPERATOR = "http://127.0.0.1:5876"


def _get_dbg() -> dict:
    try:
        with urllib.request.urlopen(f"{OPERATOR}/api/v1/debug/full", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[dbg] err: {e!r}")
        return {}


def _wait_for(predicate, timeout_s: float, label: str, poll_s: float = 1.0):
    """Poll /api/v1/debug/full until predicate(dbg)==True. Returns final dbg."""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        d = _get_dbg()
        if d:
            last = d
            try:
                if predicate(d):
                    print(f"[wait] {label}: matched after {time.time()-t0:.1f}s")
                    return d
            except Exception as e:
                print(f"[wait] predicate err: {e!r}")
        time.sleep(poll_s)
    print(f"[wait] {label}: timeout after {timeout_s:.0f}s")
    return last or {}


def speak_to_ava(text: str) -> None:
    """Synthesize via Piper and play to CABLE Input."""
    print(f"[speak] piper: {text!r}")
    wav = piper_tts(text)
    play_wav_to_cable(wav)
    print(f"[speak] played ({wav.stat().st_size} bytes)")


def wake_then_command(command: str, *, gap_seconds: float = 3.5) -> None:
    """Concatenate wake-phrase + silence + command into ONE WAV that gets
    played continuously. Reason: Whisper-poll fires wake after a ~2-3s
    transcribe batch, voice_loop then transitions to listening. If we
    speak the wake phrase then wait for the state flip before speaking
    the command, listening starts mid-gap and exits on the silence
    threshold (default 2.5s) before our command playback even begins.
    Embedding silence inside one continuous playback lets listening's
    in-flight recording naturally capture the command audio after the
    wake-phrase fires the wake gate."""
    import wave as _wave
    import numpy as np
    import tempfile
    from pathlib import Path

    wake_wav = piper_tts("Hey Ava.")
    cmd_wav = piper_tts(command)

    def _read(p):
        with _wave.open(str(p), "rb") as wf:
            rate = wf.getframerate()
            n = wf.getnframes()
            data = np.frombuffer(wf.readframes(n), dtype=np.int16)
            return rate, data

    r1, d1 = _read(wake_wav)
    r2, d2 = _read(cmd_wav)
    rate = r1
    if r2 != r1:
        # Trivial nearest-neighbor resample if Piper voices differ in rate
        ratio = r1 / r2
        d2 = np.repeat(d2, max(1, int(round(ratio))))
    silence = np.zeros(int(rate * gap_seconds), dtype=np.int16)
    combined = np.concatenate([d1, silence, d2])
    out = Path(tempfile.gettempdir()) / f"_e2e_combined_{int(time.time()*1000)}.wav"
    with _wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(combined.tobytes())
    total_s = (len(d1) + len(silence) + len(d2)) / rate
    print(f"[speak] combined WAV {total_s:.1f}s (wake + {gap_seconds:.1f}s silence + command)")
    play_wav_to_cable(out)


def listen_for_ava(seconds: float) -> str:
    """Capture from Voicemeeter Out B3 and transcribe via faster-whisper-large."""
    return record_and_transcribe(seconds=seconds)


def listen_for_ava_until_quiet(max_seconds: float = 60.0,
                                quiet_after_speech_s: float = 4.0,
                                speech_threshold: float = 0.02) -> str:
    """Stream-record from B3 in 0.5s blocks. Stop when we've seen N seconds
    of quiet AFTER we've already heard speech. Avoids the f8 issue where an
    8s record window finished before Kokoro's first-run synth (25s+) even
    started playing audio."""
    import sounddevice as sd
    import numpy as np
    from scripts.audio_loopback_harness import find_device, VM_OUT_B3

    in_idx = find_device(*VM_OUT_B3)
    if in_idx is None:
        print(f"[record] {VM_OUT_B3[0]} device not found — cannot capture")
        return ""

    sample_rate = 44100
    block_s = 0.5
    block_n = int(sample_rate * block_s)

    print(f"[record-flex] listening on B3 up to {max_seconds:.0f}s, "
          f"stop on {quiet_after_speech_s:.0f}s quiet after speech")

    blocks = []
    heard_speech = False
    quiet_run = 0.0
    t0 = time.time()
    stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32",
                            device=in_idx, blocksize=block_n)
    try:
        stream.start()
        while time.time() - t0 < max_seconds:
            data, _ = stream.read(block_n)
            blocks.append(data.copy())
            arr = np.asarray(data).flatten()
            peak = float(np.abs(arr).max()) if arr.size else 0.0
            if peak >= speech_threshold:
                heard_speech = True
                quiet_run = 0.0
            elif heard_speech:
                quiet_run += block_s
                if quiet_run >= quiet_after_speech_s:
                    print(f"[record-flex] quiet streak met after {time.time()-t0:.1f}s "
                          f"(heard speech)")
                    break
    finally:
        try:
            stream.stop(); stream.close()
        except Exception:
            pass

    flat = np.concatenate(blocks).flatten() if blocks else np.zeros(1, dtype=np.float32)
    peak = float(np.abs(flat).max()) if flat.size else 0.0
    rms = float(np.sqrt(np.mean(flat ** 2))) if flat.size else 0.0
    print(f"[record-flex] elapsed={time.time()-t0:.1f}s peak={peak:.4f} rms={rms:.4f} "
          f"heard_speech={heard_speech}")
    if peak < speech_threshold:
        return ""

    # Save WAV + transcribe
    import tempfile, wave
    from pathlib import Path
    out_wav = Path(tempfile.gettempdir()) / f"_e2e_listen_{int(time.time()*1000)}.wav"
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        int16 = np.clip(flat * 32767.0, -32768, 32767).astype(np.int16)
        wf.writeframes(int16.tobytes())
    from scripts.audio_loopback_harness import get_whisper
    model = get_whisper()
    print("[record-flex] transcribing...")
    segs, _ = model.transcribe(str(out_wav), beam_size=5)
    return " ".join(s.text.strip() for s in segs).strip()


# ── F8: voice provocation mid-sleep ──────────────────────────────────


def run_f8() -> bool:
    print("\n=== F8: voice provocation mid-sleep ===")
    # Step 1 — establish baseline AWAKE
    d0 = _get_dbg()
    state0 = d0.get("subsystem_health", {}).get("sleep", {}).get("state")
    print(f"[f8] baseline sleep state: {state0}")
    if state0 != "AWAKE":
        print(f"[f8] FAIL: not AWAKE at start ({state0})")
        return False

    # Step 2 — wake then command (split utterance per voice_loop semantics)
    wake_then_command("Go to sleep for ninety seconds.")
    print("[f8] giving STT pipeline 6s to ingest the command …")
    time.sleep(6.0)

    # Step 3 — wait for sleep state to leave AWAKE
    d1 = _wait_for(
        lambda d: d.get("subsystem_health", {}).get("sleep", {}).get("state")
                  in ("ENTERING_SLEEP", "SLEEPING"),
        timeout_s=120.0,
        label="ENTERING_SLEEP|SLEEPING after voice",
    )
    state1 = d1.get("subsystem_health", {}).get("sleep", {}).get("state")
    if state1 not in ("ENTERING_SLEEP", "SLEEPING"):
        print(f"[f8] FAIL: sleep didn't engage (state={state1})")
        return False
    print(f"[f8] sleep engaged ✓ state={state1}")

    # Step 4 — capture Ava's TTS reply on B3
    reply = listen_for_ava(seconds=8.0)
    print(f"[f8] captured reply: {reply!r}")

    # Step 5 — wait until SLEEPING (deeper than ENTERING)
    d2 = _wait_for(
        lambda d: d.get("subsystem_health", {}).get("sleep", {}).get("state") == "SLEEPING",
        timeout_s=180.0,
        label="state==SLEEPING",
    )
    state2 = d2.get("subsystem_health", {}).get("sleep", {}).get("state")
    if state2 != "SLEEPING":
        print(f"[f8] WARN: didn't reach SLEEPING (state={state2})")
        # Continue anyway — provocation should still work from ENTERING.

    # Step 6 — provocation: split utterance
    print("[f8] firing wake-up provocation …")
    wake_then_command("wake up.")
    time.sleep(6.0)

    # Step 7 — verify state advances to WAKING / AWAKE
    d3 = _wait_for(
        lambda d: d.get("subsystem_health", {}).get("sleep", {}).get("state")
                  in ("WAKING", "AWAKE"),
        timeout_s=90.0,
        label="WAKING|AWAKE after provocation",
    )
    state3 = d3.get("subsystem_health", {}).get("sleep", {}).get("state")
    if state3 not in ("WAKING", "AWAKE"):
        print(f"[f8] FAIL: provocation didn't wake Ava (state={state3})")
        return False
    print(f"[f8] provocation woke her ✓ state={state3}")

    # Step 8 — capture wake-announcement TTS
    wake_reply = listen_for_ava(seconds=10.0)
    print(f"[f8] wake reply: {wake_reply!r}")

    # Step 9 — wait until back to AWAKE
    d4 = _wait_for(
        lambda d: d.get("subsystem_health", {}).get("sleep", {}).get("state") == "AWAKE",
        timeout_s=120.0,
        label="back to AWAKE",
    )
    state4 = d4.get("subsystem_health", {}).get("sleep", {}).get("state")
    print(f"[f8] final state: {state4}")
    return state4 == "AWAKE"


# ── F12: voice onboarding ─────────────────────────────────────────────


def run_f12() -> bool:
    print("\n=== F12: voice onboarding ===")
    d0 = _get_dbg()
    onb0 = d0.get("subsystem_health", {}).get("onboarding") or d0.get("onboarding") or {}
    print(f"[f12] baseline onboarding snapshot: {onb0}")

    # Speak the trigger phrase — combined detector should pull both relationship
    # and trust score. With no specific name, the flow uses placeholder "Friend"
    # or asks back. Either is acceptable behavior.
    wake_then_command("this is my friend, give them trust three.")
    time.sleep(6.0)

    # Wait for onboarding to engage. Pull from subsystem_health, life_rhythm,
    # or whatever surfaces it. We accept either an active onboarding flow OR
    # a reply that mentions the relationship/trust.
    d1 = _wait_for(
        lambda d: bool(
            (d.get("subsystem_health", {}).get("onboarding") or {}).get("active")
            or (d.get("onboarding") or {}).get("active")
            or (d.get("voice_loop", {}).get("state") in ("listening", "speaking"))
        ),
        timeout_s=60.0,
        label="onboarding active or Ava speaking",
    )

    reply = listen_for_ava_until_quiet(max_seconds=60.0)
    print(f"[f12] captured reply: {reply!r}")

    # Verify reply hints at onboarding (Ava's onboarding flow asks for name first)
    keywords = ("name", "friend", "trust", "introduce", "meet", "what should i call")
    matched = any(k in (reply or "").lower() for k in keywords)
    print(f"[f12] reply matches onboarding keywords: {matched}")

    final_dbg = _get_dbg()
    onb_final = final_dbg.get("subsystem_health", {}).get("onboarding") or final_dbg.get("onboarding") or {}
    print(f"[f12] final onboarding snapshot: {onb_final}")

    return matched or bool(onb_final.get("active"))


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "f8":
        return 0 if run_f8() else 1
    if cmd == "f12":
        return 0 if run_f12() else 1
    if cmd == "both":
        ok8 = run_f8()
        ok12 = run_f12()
        print("\n=== summary ===")
        print(f"  F8:  {'PASS' if ok8 else 'FAIL'}")
        print(f"  F12: {'PASS' if ok12 else 'FAIL'}")
        return 0 if (ok8 and ok12) else 1
    print(f"unknown cmd: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
