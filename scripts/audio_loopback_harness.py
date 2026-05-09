"""scripts/audio_loopback_harness.py — Phase C real-audio harness.

Routes audio to/from Ava through Voicemeeter Potato + the basic VB-CABLE
pair. Independent driver-side TTS (Piper) and STT (faster-whisper-large)
per docs/AUTONOMOUS_TESTING.md "don't test models against themselves".

Routing assumed:
    Claude → Ava direction:
        Claude plays TTS → "CABLE Input"  (sounddevice output)
        Ava records mic ← "CABLE Output"  (must be set as Ava's mic)

    Ava → Claude direction:
        Ava plays TTS → "Voicemeeter VAIO3 Input"  (must be set as Ava's TTS device)
        Claude records ← "Voicemeeter Out B3"      (sounddevice input)

Subcommands:
    probe              — enumerate devices, run tone test on Claude→Ava cable
    speak <text>       — synthesize text via Piper, play to CABLE Input
    listen [seconds]   — record from Voicemeeter Out B3 for N seconds, transcribe
    drive <prompt>     — speak prompt, wait, listen, transcribe — full round-trip

Usage:
    py -3.11 scripts/audio_loopback_harness.py probe
    py -3.11 scripts/audio_loopback_harness.py drive "what time is it"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

try:
    import numpy as np
    import sounddevice as sd
except ImportError as e:
    print(f"missing dep: {e!r}; pip install sounddevice numpy")
    sys.exit(2)


# ── Device resolution ──────────────────────────────────────────────


def find_device(name_substr: str, kind: str) -> int | None:
    """kind in ('input','output'). Returns first matching device index."""
    target_key = f"max_{kind}_channels"
    name_lower = name_substr.lower()
    for i, dev in enumerate(sd.query_devices()):
        if name_lower in dev.get("name", "").lower() and dev.get(target_key, 0) > 0:
            return i
    return None


CABLE_IN = ("CABLE Input", "output")        # Claude plays TTS here
CABLE_OUT = ("CABLE Output", "input")       # (verify cable: Claude records here)
VAIO3_IN = ("Voicemeeter VAIO3 Input", "output")  # Ava plays TTS here (must configure in Windows)
VM_OUT_B3 = ("Voicemeeter Out B3", "input")        # Claude records Ava's TTS here


def resolve_devices() -> dict:
    return {
        "cable_input_idx":  find_device(*CABLE_IN),
        "cable_output_idx": find_device(*CABLE_OUT),
        "vaio3_input_idx":  find_device(*VAIO3_IN),
        "vm_out_b3_idx":    find_device(*VM_OUT_B3),
    }


# ── Tone test ──────────────────────────────────────────────────────


def tone_test_cable(duration_s: float = 1.0, freq: float = 440.0) -> bool:
    """Play a tone on CABLE Input, capture from CABLE Output. Verify peak."""
    out_idx = find_device(*CABLE_IN)
    in_idx = find_device(*CABLE_OUT)
    if out_idx is None or in_idx is None:
        print(f"[tone] FAIL: CABLE devices missing (out={out_idx}, in={in_idx})")
        return False
    sample_rate = 44100
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    tone = (0.3 * np.sin(2 * np.pi * freq * t)).reshape(-1, 1).astype(np.float32)
    try:
        captured = sd.playrec(
            tone, samplerate=sample_rate, channels=1,
            dtype="float32", device=(in_idx, out_idx),
        )
        sd.wait()
    except Exception as e:
        print(f"[tone] playrec error: {e!r}")
        return False
    flat = np.asarray(captured).flatten()
    if flat.size == 0 or not np.all(np.isfinite(flat)):
        print(f"[tone] FAIL: bad capture (size={flat.size})")
        return False
    peak = float(np.abs(flat).max())
    print(f"[tone] CABLE peak amplitude={peak:.4f} (expected ~0.3)")
    return 0.05 <= peak <= 1.5


# ── Driver-side TTS (Piper) ────────────────────────────────────────


def piper_tts(text: str, model_path: str | None = None) -> Path:
    """Synthesize text via Piper. Returns path to WAV file. Lazy install
    of piper-tts if missing.

    Piper's synthesize() returns a generator yielding AudioChunk objects per
    sentence; concatenate their `.audio_int16_bytes` (or fall back to
    `.audio_float_array` if needed) into a single WAV file at the model's
    native sample rate (config.sample_rate, typically 22050)."""
    try:
        from piper import PiperVoice  # type: ignore
    except ImportError:
        print("Piper not installed. Install: py -3.11 -m pip install piper-tts")
        print("Then download a voice model (e.g. en_US-amy-medium) to models/piper/")
        sys.exit(2)
    if model_path is None:
        # Wren's voice signature is en_US-amy-medium — committed 2026-05-08
        # so Ava (also Piper, en_US-lessac-high) and Wren are distinguishable
        # to the listener. Lessac is reserved for Ava. Don't change without
        # checking with Zeke; voice identity matters here.
        models_dir = Path("models/piper")
        preferred_order = [
            "en_US-amy-medium.onnx",
            "en_US-libritts_r-medium.onnx",
            "en_US-lessac-high.onnx",
        ]
        chosen = None
        for name in preferred_order:
            p = models_dir / name
            if p.is_file():
                chosen = p
                break
        if chosen is None:
            candidates = list(models_dir.glob("*.onnx")) if models_dir.exists() else []
            if not candidates:
                print("No Piper voice model found at models/piper/*.onnx")
                print("Download from https://github.com/rhasspy/piper/releases (e.g. en_US-amy-medium)")
                sys.exit(2)
            chosen = candidates[0]
        model_path = str(chosen)
    voice = PiperVoice.load(model_path)
    out_wav = Path(tempfile.gettempdir()) / f"piper_out_{int(time.time()*1000)}.wav"
    sample_rate = int(getattr(voice.config, "sample_rate", 22050))
    audio_bytes = b""
    for chunk in voice.synthesize(text):
        # AudioChunk has audio_int16_bytes (16-bit PCM) — use it directly.
        b = getattr(chunk, "audio_int16_bytes", None)
        if b is None:
            # Fallback: convert float array → int16
            arr = np.asarray(getattr(chunk, "audio_float_array", []))
            b = (np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)).tobytes()
        audio_bytes += b
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_bytes)
    return out_wav


def play_wav_to_cable(wav_path: Path, *, also_to_speakers: bool = True) -> None:
    """Play WAV to CABLE Input (Ava's mic path). If `also_to_speakers` is True,
    also mirror the audio to Realtek speakers so Zeke can hear Claude Code's
    voice during testing — same monitor pattern Voicemeeter would provide,
    but implemented in software without requiring Voicemeeter routing
    changes. Set the AVA_HARNESS_NO_MONITOR=1 env var to suppress (e.g.
    when Zeke's mic isn't muted and acoustic feedback is a risk)."""
    import os as _os
    import threading as _threading

    out_idx = find_device(*CABLE_IN)
    if out_idx is None:
        raise RuntimeError("CABLE Input device not found")
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    # Force 44100 Hz for CABLE Input (Piper outputs at 22050; CABLE expects
    # 44100 by default and a rate mismatch silently emits no audio on this
    # hardware). Linear upsample by repeat — voice quality stays acceptable.
    target_rate = 44100
    if rate != target_rate:
        # Simple nearest-neighbor upsample (factor = target_rate / rate)
        factor = target_rate // rate if rate <= target_rate else 1
        if factor > 1:
            data = np.repeat(data, factor)
        rate = target_rate

    monitor = also_to_speakers and not _os.environ.get("AVA_HARNESS_NO_MONITOR")
    speaker_idx = find_device("Speakers (Realtek", "output") if monitor else None

    if speaker_idx is None:
        print(f"[harness] no speaker monitor (monitor={monitor}, speaker_idx={speaker_idx})", flush=True)
        sd.play(data, samplerate=rate, device=out_idx)
        sd.wait()
        return
    print(f"[harness] dual-play: CABLE Input idx={out_idx} + Speakers idx={speaker_idx}", flush=True)

    # Two parallel sd.play calls — one to CABLE Input (Ava's mic), one to
    # Realtek speakers (Zeke's monitor). Done via threads so they start
    # simultaneously; both block until done. Same audio data, same rate.
    err_holder = {}
    def _play_speaker():
        try:
            sd.play(data, samplerate=rate, device=speaker_idx)
            sd.wait()
        except Exception as e:
            err_holder["speaker"] = repr(e)
    t = _threading.Thread(target=_play_speaker, daemon=True)
    t.start()
    sd.play(data, samplerate=rate, device=out_idx)
    sd.wait()
    t.join(timeout=5.0)
    if err_holder.get("speaker"):
        print(f"[harness] speaker monitor error: {err_holder['speaker']}")


# ── Driver-side STT (faster-whisper-large) ─────────────────────────


_WHISPER_CACHE: object | None = None


def get_whisper():
    global _WHISPER_CACHE
    if _WHISPER_CACHE is not None:
        return _WHISPER_CACHE
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        print("faster-whisper not installed. Install: py -3.11 -m pip install faster-whisper")
        sys.exit(2)
    # Force CPU int8 — Ava holds the GPU (InsightFace + Kokoro) and Whisper's
    # CUDA path needs cublas64_12.dll which lives in the nvidia-*-cu12 packages.
    # Those DLLs are added to the search path only inside Ava's process via
    # brain.insight_face_engine._add_cuda_paths(); standalone scripts hit
    # "cublas64_12.dll not found". CPU int8 is slower (~2-3× real-time on this
    # CPU) but reliable and isolated from Ava's GPU work.
    print("[whisper] loading faster-whisper-large on CPU int8 (first load: 30-60s)...")
    _WHISPER_CACHE = WhisperModel("large-v3", device="cpu", compute_type="int8")
    return _WHISPER_CACHE


def record_and_transcribe(seconds: float = 5.0, capture_device: str | None = None) -> str:
    """Record from `capture_device` (default Voicemeeter Out B3) and transcribe.
    Pass capture_device='CABLE Output' for harness self-test mode (Piper to
    CABLE Input loops back to CABLE Output without needing Ava in the path)."""
    cap_name = capture_device or VM_OUT_B3[0]
    in_idx = find_device(cap_name, "input")
    if in_idx is None:
        print(f"[record] {cap_name} device not found — cannot capture")
        return ""
    sample_rate = 44100
    print(f"[record] capturing {seconds}s from {cap_name}...")
    captured = sd.rec(int(sample_rate * seconds), samplerate=sample_rate, channels=1, dtype="float32", device=in_idx)
    sd.wait()
    flat = np.asarray(captured).flatten()
    peak = float(np.abs(flat).max()) if flat.size else 0.0
    print(f"[record] peak={peak:.4f} (silence threshold 0.01)")
    if peak < 0.01:
        print("[record] WARN: capture is essentially silent — check Ava's TTS device + Voicemeeter routing")
        return ""
    # Transcribe
    model = get_whisper()
    out_wav = Path(tempfile.gettempdir()) / f"recorded_{int(time.time()*1000)}.wav"
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # Convert float32 to int16
        int16 = np.clip(flat * 32767.0, -32768, 32767).astype(np.int16)
        wf.writeframes(int16.tobytes())
    print("[whisper] transcribing...")
    segments, info = model.transcribe(str(out_wav), beam_size=5)
    text = " ".join(seg.text.strip() for seg in segments)
    return text.strip()


# ── Driver flow: speak → wait → listen → transcribe ────────────────


def drive(prompt: str, listen_seconds: float = 8.0) -> dict:
    """Full round-trip:
       1. Synthesize prompt via Piper
       2. Play to CABLE Input (Ava records via her mic = CABLE Output)
       3. Wait briefly so Ava's reply lands
       4. Record Ava's TTS from Voicemeeter Out B3
       5. Transcribe via faster-whisper-large
    """
    t0 = time.time()
    print(f"[drive] synthesizing prompt: {prompt!r}")
    wav = piper_tts(prompt)
    t_synth = time.time()
    print(f"[drive] synth time={t_synth - t0:.2f}s; playing to CABLE Input")
    # Start recording on Ava's TTS bus BEFORE playing the prompt — Ava's
    # reply may begin during/right after our prompt finishes.
    # For simplicity in v1: play first, then start listening with a generous
    # window. v2 should overlap.
    play_wav_to_cable(wav)
    t_play_done = time.time()
    print(f"[drive] played in {t_play_done - t_synth:.2f}s; listening for {listen_seconds}s")
    transcript = record_and_transcribe(listen_seconds)
    t_total = time.time() - t0
    return {
        "prompt": prompt,
        "transcript": transcript,
        "synth_s": round(t_synth - t0, 3),
        "play_s": round(t_play_done - t_synth, 3),
        "listen_s": round(listen_seconds, 3),
        "total_s": round(t_total, 3),
    }


# ── CLI ────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["probe", "speak", "listen", "drive", "selfloop"])
    p.add_argument("text", nargs="?", default=None)
    p.add_argument("--seconds", type=float, default=5.0)
    args = p.parse_args()

    devs = resolve_devices()
    print(f"[devices] {json.dumps(devs, indent=2)}")
    missing = [k for k, v in devs.items() if v is None]
    if missing:
        print(f"[devices] WARN missing: {missing}")
        if args.cmd != "probe":
            return 1

    if args.cmd == "probe":
        ok = tone_test_cable()
        print(f"[probe] {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1

    if args.cmd == "speak":
        if not args.text:
            print("speak requires text argument")
            return 2
        wav = piper_tts(args.text)
        print(f"[speak] WAV at {wav}")
        play_wav_to_cable(wav)
        print("[speak] played to CABLE Input")
        return 0

    if args.cmd == "listen":
        text = record_and_transcribe(args.seconds)
        print(f"[listen] transcript: {text!r}")
        return 0

    if args.cmd == "selfloop":
        # Self-test: Piper → CABLE Input → CABLE Output → faster-whisper.
        # Verifies the entire harness without needing Ava in the path.
        # Uses sd.playrec to avoid threading conflicts with Ava's audio devices.
        if not args.text:
            print("selfloop requires text")
            return 2
        timings: dict = {"text": args.text}
        t0 = time.time()
        print("[selfloop] synth first…")
        wav = piper_tts(args.text)
        timings["synth_s"] = round(time.time() - t0, 3)
        # Read wav and upsample to 44100
        with wave.open(str(wav), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        target_rate = 44100
        if rate != target_rate:
            factor = target_rate // rate if rate <= target_rate else 1
            if factor > 1:
                data = np.repeat(data, factor)
        # Pad/trim to exactly args.seconds duration
        wanted_samples = int(target_rate * args.seconds)
        if len(data) < wanted_samples:
            data = np.concatenate([data, np.zeros(wanted_samples - len(data), dtype=np.float32)])
        else:
            data = data[:wanted_samples]
        out_idx = find_device(*CABLE_IN)
        in_idx = find_device("CABLE Output", "input")
        print(f"[selfloop] playrec on CABLE Input(idx={out_idx}) → CABLE Output(idx={in_idx}), {args.seconds:.0f}s")
        t1 = time.time()
        captured = sd.playrec(data.reshape(-1, 1), samplerate=target_rate, channels=1, dtype="float32", device=(in_idx, out_idx))
        sd.wait()
        timings["playrec_s"] = round(time.time() - t1, 3)
        flat = np.asarray(captured).flatten()
        peak = float(np.abs(flat).max())
        print(f"[selfloop] capture peak={peak:.4f}")
        if peak < 0.01:
            print(f"[selfloop] FAIL: silent capture")
            return 1
        # Save and transcribe
        out_wav = Path(tempfile.gettempdir()) / f"_selfloop_{int(time.time()*1000)}.wav"
        with wave.open(str(out_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(target_rate)
            int16 = np.clip(flat * 32767, -32768, 32767).astype(np.int16)
            wf.writeframes(int16.tobytes())
        model = get_whisper()
        print(f"[selfloop] transcribing…")
        t2 = time.time()
        segs, info = model.transcribe(str(out_wav), beam_size=5)
        transcript = " ".join(s.text.strip() for s in segs).strip()
        timings["transcribe_s"] = round(time.time() - t2, 3)
        timings["total_s"] = round(time.time() - t0, 3)
        print(f"[selfloop] sent:    {args.text!r}")
        print(f"[selfloop] heard:   {transcript!r}")
        # Word-overlap match — Whisper transcription won't be exact but
        # should share most words.
        sent_words = set(w.strip(".,!?").lower() for w in args.text.split())
        heard_words = set(w.strip(".,!?").lower() for w in transcript.split())
        overlap = len(sent_words & heard_words) / max(len(sent_words), 1)
        verdict = "PASS" if overlap >= 0.5 else "FAIL"
        print(f"[selfloop] word_overlap={overlap*100:.0f}%  verdict={verdict}")
        timings["word_overlap_pct"] = round(overlap * 100, 1)
        timings["verdict"] = verdict
        print(f"[selfloop] timings: {json.dumps(timings, indent=2)}")
        return 0 if verdict == "PASS" else 1

    if args.cmd == "drive":
        if not args.text:
            print("drive requires prompt")
            return 2
        result = drive(args.text, listen_seconds=args.seconds)
        print(f"[drive] result: {json.dumps(result, indent=2)}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
