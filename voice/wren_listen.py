"""wren_listen.py — Zeke's voice IN, transcribed for Wren to read.

The STT-in half of voice. Captures from the mic, transcribes with faster-whisper,
and writes the transcript to a file Wren (the CC session) can read between turns.

Why file-drop and not push-into-the-session: delivering text into a running CC
session at cold-idle is the same unsolved upstream problem as the cold-wake bug
(see memory cc_channel_cold_wake_is_upstream_bug). The pragmatic MVP that works
TODAY is: this script writes transcripts to D:\Wren\scratch\voice_in.txt; the
running Wren session reads/tails that file. Real push is post-deployment.

Two modes:
  once   — record N seconds, transcribe, print + append to the transcript file
  loop   — VAD-style: keep recording fixed windows, transcribe any non-silent
           one, append. Crude but works without extra deps. Ctrl-C to stop.

Honest status (2026-06-01): solo-verifiable parts are model-load and transcription
of a known WAV (--selftest). Whether it correctly hears ZEKE needs Zeke's voice
into the real mic — that's the live half to confirm together. See notes/voice_setup.md.

Usage:
    py -3.11 experiments/wren_listen.py --selftest          # solo: model + transcribe
    py -3.11 experiments/wren_listen.py once --seconds 6    # one capture
    py -3.11 experiments/wren_listen.py loop                # continuous (Ctrl-C)
    py -3.11 experiments/wren_listen.py once --device "Microphone (Realtek"
"""
from __future__ import annotations

import argparse
import json as _json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path
from urllib import request as _req

# Windows consoles default to cp1252, which can't encode chars like U+2192.
# Make stdout/stderr crash-proof regardless of what we print.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import numpy as np
    import sounddevice as sd
except ImportError as e:
    print(f"missing dep: {e!r}; py -3.11 -m pip install sounddevice numpy")
    sys.exit(2)

# Shared voice-state signal for the status overlay (no-op if helper missing).
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from wren_voice_status import set_state, is_muted
except Exception:
    def set_state(*_a, **_k):  # type: ignore
        pass
    def is_muted():  # type: ignore
        return False

# Real mic on this laptop is "Microphone Array (Realtek(R) Audio)" = idx 1.
# (Plain "Microphone (Realtek" does NOT match the active device — verified
# 2026-06-01.) Override with --device if the device layout changes.
# Iris adaptation: Wren's tower mic was the Realtek array; mine is the Logitech PRO X
# gaming headset (idx 7 on the tower). Override with WREN_MIC_SUBSTR env or --device.
DEFAULT_MIC_SUBSTR = os.environ.get("WREN_MIC_SUBSTR", "Logitech PRO X")
TRANSCRIPT_FILE = Path(r"D:\Wren-Companion\scratch\voice_in.txt")
SAMPLE_RATE = 16000      # whisper-native; avoids resample
SILENCE_PEAK = 0.01      # below this = treat window as silence, skip transcribe

# ── ASR backend selector (Wren ears upgrade, 2026-06-12). Default 'whisper' (the
# proven faster-whisper path below). Set WREN_ASR=sensevoice to route transcription to
# the warm SenseVoice server (:8766, run from nextgen-venv) for faster ASR + emotion/
# event HINTS, with automatic fall-back to whisper if that server is down so the ears
# never go deaf. See wren_sensevoice_server.py + reference_emotion_aware_ears_2026-06-12.
ASR_BACKEND = os.environ.get("WREN_ASR", "whisper").strip().lower()
SENSEVOICE_URL = f"http://127.0.0.1:{os.environ.get('WREN_SV_PORT', '8766')}/transcribe"
_LAST_RICH = None  # last SenseVoice {text,emotion,events}; pop_last_rich() drains it

_WHISPER = None


WHISPER_SIZE = "small"  # Zeke 2026-06-07: base mis-heard fast words ("to see if"
#                         → "the safe") on barge-in. small is the accuracy sweet
#                         spot; bigger (medium/large) is overkill + costly here.


def get_whisper():
    """Load faster-whisper once. Prefer GPU (Zeke has VRAM headroom: 8GB, voice
    model ~1.8GB), but CTranslate2's CUDA path needs cuDNN — separate from the
    voice model's torch CUDA, and not guaranteed present (the original CPU choice
    hints it wasn't). So try CUDA float16, FALL BACK to CPU int8 on any failure —
    either way you get the 'small' accuracy upgrade over the old 'base'."""
    global _WHISPER
    if _WHISPER is not None:
        return _WHISPER
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        print("faster-whisper not installed: py -3.11 -m pip install faster-whisper")
        sys.exit(2)
    try:
        _WHISPER = WhisperModel(WHISPER_SIZE, device="cuda", compute_type="float16")
        # prove the GPU path actually runs (load can succeed but infer fail w/o cuDNN)
        _WHISPER.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)
        print(f"[whisper] loaded '{WHISPER_SIZE}' on CUDA float16")
    except Exception as e:
        print(f"[whisper] GPU path unavailable ({e!r}); using CPU int8")
        _WHISPER = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8")
        print(f"[whisper] loaded '{WHISPER_SIZE}' on CPU int8")
    return _WHISPER


def find_input_device(name_substr: str) -> int | None:
    name_lower = name_substr.lower()
    for i, dev in enumerate(sd.query_devices()):
        if name_lower in dev.get("name", "").lower() and dev.get("max_input_channels", 0) > 0:
            return i
    return None


def _sensevoice_rich(audio_f32) -> "dict | None":
    """Transcribe via the warm SenseVoice server -> {text, emotion, events, ...}, or
    None on ANY failure (so the caller falls back to whisper and the ears never go
    deaf). Writes the array to a temp WAV because the server reads a path."""
    out_wav = Path(tempfile.gettempdir()) / f"wren_sv_{int(time.time()*1000)}.wav"
    try:
        int16 = np.clip(np.ascontiguousarray(audio_f32, dtype=np.float32) * 32767.0,
                        -32768, 32767).astype(np.int16)
        with wave.open(str(out_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(int16.tobytes())
        body = _json.dumps({"wav_path": str(out_wav), "language": "en"}).encode("utf-8")
        req = _req.Request(SENSEVOICE_URL, data=body,
                           headers={"Content-Type": "application/json"}, method="POST")
        with _req.urlopen(req, timeout=30) as resp:
            obj = _json.loads(resp.read().decode("utf-8", "replace"))
        return obj if ("text" in obj and "error" not in obj) else None
    except Exception:
        return None
    finally:
        try:
            out_wav.unlink(missing_ok=True)
        except Exception:
            pass


def pop_last_rich():
    """Return + clear the last SenseVoice rich result ({text,emotion,events}) if the
    sensevoice backend produced one for the most recent _transcribe_array; else None.
    Lets the MCP surface the emotion HINT without changing _transcribe_array's str API."""
    global _LAST_RICH
    r = _LAST_RICH
    _LAST_RICH = None
    return r


def _transcribe_array(audio_f32: np.ndarray, beam_size: int = 1) -> str:
    """Transcribe a float32 16kHz mono array. Cuts the "dead tail" after Zeke stops
    talking two ways (Zeke 2026-06-03: shorten my-end-to-his-end):
      - feed the numpy array STRAIGHT to faster-whisper (it accepts ndarray) instead
        of writing a temp WAV and re-reading it — drops a whole disk round-trip;
      - greedy decoding (beam_size=1) instead of 5 — roughly 2-3x faster decode, and
        on a clear desk mic the accuracy cost is negligible. Bump beam_size for the
        offline batch paths if they ever mis-hear.
    Falls back to the temp-WAV path if a faster-whisper build won't take an array.

    Backend: if WREN_ASR=sensevoice, route to the warm SenseVoice server first (faster,
    + emotion HINT stashed for pop_last_rich); on any failure fall through to whisper so
    the ears never go deaf. Default WREN_ASR=whisper leaves the path below 100% unchanged."""
    global _LAST_RICH
    _LAST_RICH = None
    if ASR_BACKEND == "sensevoice":
        rich = _sensevoice_rich(audio_f32)
        if rich is not None:
            _LAST_RICH = rich
            return (rich.get("text") or "").strip()
        # server down/failed -> fall through to the whisper path below (robust)
    model = get_whisper()
    audio = np.ascontiguousarray(audio_f32, dtype=np.float32)
    try:
        segments, _ = model.transcribe(audio, beam_size=beam_size, language="en")
        return " ".join(seg.text.strip() for seg in segments).strip()
    except Exception:
        # defensive: older faster-whisper that only accepts a path
        out_wav = Path(tempfile.gettempdir()) / f"wren_listen_{int(time.time()*1000)}.wav"
        int16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
        with wave.open(str(out_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(int16.tobytes())
        try:
            segments, _ = model.transcribe(str(out_wav), beam_size=beam_size, language="en")
            return " ".join(seg.text.strip() for seg in segments).strip()
        finally:
            try:
                out_wav.unlink(missing_ok=True)
            except Exception:
                pass


def record_window(seconds: float, device_substr: str) -> np.ndarray:
    dev_idx = find_input_device(device_substr)
    if dev_idx is None:
        print(f"[wren_listen] mic '{device_substr}' not found; using system default input")
    captured = sd.rec(int(SAMPLE_RATE * seconds), samplerate=SAMPLE_RATE,
                      channels=1, dtype="float32", device=dev_idx)
    sd.wait()
    return np.asarray(captured).flatten()


def append_transcript(text: str) -> None:
    TRANSCRIPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {text}\n")


def listen_once(seconds: float, device_substr: str) -> int:
    print(f"[wren_listen] recording {seconds}s from mic — speak now...")
    set_state("listening")
    audio = record_window(seconds, device_substr)
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    print(f"[wren_listen] peak={peak:.4f} (silence<{SILENCE_PEAK})")
    if peak < SILENCE_PEAK:
        set_state("idle")
        print("[wren_listen] silent — nothing to transcribe (check mic input)")
        return 1
    set_state("thinking")  # heard him; transcript about to be handed to the session
    text = _transcribe_array(audio)
    print(f"[wren_listen] heard: {text!r}")
    if text:
        append_transcript(text)
        print(f"[wren_listen] appended -> {TRANSCRIPT_FILE}")
    return 0


def listen_loop(window: float, device_substr: str) -> int:
    print(f"[wren_listen] loop mode, {window}s windows. Ctrl-C to stop.")
    get_whisper()  # warm the model before the loop so first window isn't slow
    try:
        while True:
            if is_muted():
                set_state("muted", "mic off")
                time.sleep(0.3)   # Zeke muted himself; don't capture
                continue
            set_state("listening")
            audio = record_window(window, device_substr)
            peak = float(np.abs(audio).max()) if audio.size else 0.0
            if peak < SILENCE_PEAK:
                continue
            set_state("thinking")  # heard something; transcribing + handing off
            text = _transcribe_array(audio)
            if text:
                print(f"[{time.strftime('%H:%M:%S')}] {text}")
                append_transcript(text)
    except KeyboardInterrupt:
        set_state("idle")
        print("\n[wren_listen] stopped.")
        return 0


def selftest() -> int:
    """Solo-verifiable: synthesize known text with Piper, transcribe it back with
    whisper, check word overlap. Verifies the STT model end-to-end WITHOUT a mic
    or Zeke's voice. (Mirrors Ava harness selfloop but pure-file, no devices.)"""
    text = "the quick brown fox jumps over the lazy dog"
    print(f"[selftest] synth -> transcribe round-trip on: {text!r}")
    try:
        from piper import PiperVoice  # type: ignore
    except ImportError:
        print("[selftest] piper-tts needed for selftest: py -3.11 -m pip install piper-tts")
        return 2
    amy = Path(r"D:\AvaAgentv2\models\piper\en_US-amy-medium.onnx")
    if not amy.is_file():
        print(f"[selftest] voice model missing: {amy}")
        return 2
    voice = PiperVoice.load(str(amy))
    rate = int(getattr(voice.config, "sample_rate", 22050))
    audio_bytes = b""
    for chunk in voice.synthesize(text):
        b = getattr(chunk, "audio_int16_bytes", None)
        if b is None:
            arr = np.asarray(getattr(chunk, "audio_float_array", []))
            b = (np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)).tobytes()
        audio_bytes += b
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    # resample piper rate → whisper rate by nearest-neighbor (selftest only)
    if rate != SAMPLE_RATE:
        idx = (np.arange(int(len(audio) * SAMPLE_RATE / rate)) * rate / SAMPLE_RATE).astype(int)
        idx = idx[idx < len(audio)]
        audio = audio[idx]
    heard = _transcribe_array(audio)
    print(f"[selftest] heard: {heard!r}")
    sent_w = set(w.strip(".,!?").lower() for w in text.split())
    heard_w = set(w.strip(".,!?").lower() for w in heard.split())
    overlap = len(sent_w & heard_w) / max(len(sent_w), 1)
    verdict = "PASS" if overlap >= 0.6 else "FAIL"
    print(f"[selftest] word_overlap={overlap*100:.0f}%  {verdict}")
    print("[selftest] (hearing ZEKE through the real mic still needs a live test)")
    return 0 if verdict == "PASS" else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Transcribe Zeke's voice for Wren")
    p.add_argument("mode", nargs="?", choices=["once", "loop"], default="once")
    p.add_argument("--seconds", type=float, default=6.0, help="capture window length")
    p.add_argument("--device", default=DEFAULT_MIC_SUBSTR, help="mic device name substring")
    p.add_argument("--selftest", action="store_true", help="solo model+transcribe check")
    args = p.parse_args()

    if args.selftest:
        return selftest()
    if args.mode == "loop":
        return listen_loop(args.seconds, args.device)
    return listen_once(args.seconds, args.device)


if __name__ == "__main__":
    sys.exit(main())
