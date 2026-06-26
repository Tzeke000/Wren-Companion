"""wren_sensevoice.py — Wren's EARS, upgrade candidate: SenseVoice-Small.

Alternative ASR backend to faster-whisper (wren_listen.py / wren_voice_mcp.py).
SenseVoice-Small does multilingual ASR + speech-EMOTION recognition + audio-EVENT
detection (laugh, cough, applause...) in ONE pass, ~15x faster than whisper
(FunAudioLLM). Runs on the torch 2.11+cu128 GPU path in the isolated nextgen-venv.

Greenlit by Zeke 2026-06-12 (memory project_voice_model_upgrade_2026-06-12 +
reference_emotion_aware_ears_2026-06-12). The emotion/event tag is a HINT to reason
over in context, NOT gospel (Zeke: "you'll have to inference those labels eventually,
they may be incorrect in context") — but it's better-and-15x-faster than plain whisper
text. Whisper stays the live fallback; this is meant to wire in behind a flag.

API mirrors wren_listen so it can slot in as a backend:
    transcribe(path_or_ndarray) -> {text, emotion, events, lang, raw, latency_s}

Selftest (downloads ~900MB SenseVoiceSmall on first run, on cuda):
    & D:\Wren\voice\nextgen-venv\Scripts\python.exe experiments\wren_sensevoice.py --selftest
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Hub: 'hf' = HuggingFace (Cloudflare CDN, ~MB/s from Finland) — DEFAULT.
# 'ms' = ModelScope.cn (China-hosted, measured ~43kB/s / ~6h from Finland — avoid).
# Override with env WREN_SV_HUB.
HUB = os.environ.get("WREN_SV_HUB", "hf")
MODEL_ID = "FunAudioLLM/SenseVoiceSmall" if HUB == "hf" else "iic/SenseVoiceSmall"
REF_WAV = Path(r"D:\Wren\voice\wren_ref.wav")  # my own designed-voice seed = test sample

# SenseVoice emits special tokens; these are the ones we care about.
EMOTION_TAGS = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL",
                "DISGUSTED", "SURPRISED", "EMO_UNKNOWN"}
EVENT_TAGS = {"Speech", "BGM", "Applause", "Laughter", "Cry",
              "Sneeze", "Breath", "Cough"}

_MODEL = None


def _load(device: str = "cuda:0"):
    """Lazy-load SenseVoiceSmall once. disable_update=True so it never blocks on an
    online version check (important on hotel wifi)."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    from funasr import AutoModel
    t0 = time.time()
    print(f"[sensevoice] loading {MODEL_ID} on {device} "
          f"(first run downloads ~900MB)...", flush=True)
    _MODEL = AutoModel(model=MODEL_ID, hub=HUB, device=device, disable_update=True)
    print(f"[sensevoice] model ready in {time.time()-t0:.1f}s", flush=True)
    return _MODEL


def _unload() -> None:
    """Unload the SenseVoice model and free VRAM; keep the module importable.
    Next call to _load() or transcribe() will lazily reload. Called by the server
    on GET /cold (voice_call_end lifecycle)."""
    global _MODEL
    _MODEL = None
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    print("[sensevoice] _unload — model freed", flush=True)


def _parse_tags(raw: str):
    """Pull the emotion label, audio events, and language out of SenseVoice's raw
    tagged output, e.g. '<|en|><|HAPPY|><|Speech|><|woitn|>hello there'."""
    tags = re.findall(r"<\|([^|]+)\|>", raw or "")
    emotion = next((t for t in tags if t in EMOTION_TAGS), None)
    events = [t for t in tags if t in EVENT_TAGS and t != "Speech"]
    lang = next((t for t in tags if len(t) == 2 and t.islower()), None)
    return emotion, events, lang


def transcribe(audio, language: str = "en", device: str = "cuda:0") -> dict:
    """Transcribe a WAV path or float32 ndarray. Returns text + emotion/event hints.

    `emotion`/`events` are PERCEPTION HINTS to weigh in context, not ground truth."""
    from funasr.utils.postprocess_utils import rich_transcription_postprocess
    m = _load(device)
    inp = str(audio) if isinstance(audio, (str, Path)) else audio
    t0 = time.time()
    res = m.generate(input=inp, cache={}, language=language, use_itn=True)
    dt = time.time() - t0
    raw = res[0]["text"] if res else ""
    emotion, events, lang = _parse_tags(raw)
    text = rich_transcription_postprocess(raw)
    return {"text": text, "emotion": emotion, "events": events,
            "lang": lang, "raw": raw, "latency_s": round(dt, 3)}


def selftest(device: str = "cuda:0") -> int:
    import torch
    has_cuda = torch.cuda.is_available()
    print(f"[selftest] cuda={has_cuda} "
          f"dev={torch.cuda.get_device_name(0) if has_cuda else 'cpu'}")
    if not REF_WAV.is_file():
        print(f"[selftest] missing test sample {REF_WAV}")
        return 2
    if has_cuda:
        torch.cuda.reset_peak_memory_stats()
    dev = device if has_cuda else "cpu"
    out = transcribe(REF_WAV, device=dev)        # first pass = download + load + infer
    out2 = transcribe(REF_WAV, device=dev)       # warm pass = pure infer latency
    if has_cuda:
        print(f"[selftest] peak torch VRAM "
              f"{torch.cuda.max_memory_allocated()/1024**2:.0f} MiB")
    print(f"[selftest] text   : {out['text']!r}")
    print(f"[selftest] emotion: {out['emotion']}   events: {out['events']}   "
          f"lang: {out['lang']}")
    print(f"[selftest] raw    : {out['raw']!r}")
    print(f"[selftest] latency: cold {out['latency_s']}s | warm {out2['latency_s']}s")
    ok = bool(out["text"])
    print(f"[selftest] {'PASS — SenseVoice transcribes on this GPU' if ok else 'FAIL — empty'}")
    print("[selftest] (emotion/events are HINTS to reason over, not ground truth)")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Wren ears via SenseVoice-Small")
    p.add_argument("audio", nargs="?", default=None)
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--cpu", action="store_true", help="force CPU (offload test)")
    args = p.parse_args()
    dev = "cpu" if args.cpu else "cuda:0"
    if args.selftest or not args.audio:
        return selftest(dev)
    print(transcribe(args.audio, device=dev))
    return 0


if __name__ == "__main__":
    sys.exit(main())
