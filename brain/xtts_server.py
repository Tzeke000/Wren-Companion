"""Persistent XTTS-v2 synthesis server.

Runs as a long-lived child process under .venv_xtts (because XTTS deps
conflict hard with the main venv's torch/transformers). Parent process
(tts_worker) talks to it via newline-delimited JSON on stdin/stdout.

Request format (one per line on stdin):
  {"id": <int>, "text": "...", "out": "C:/abs/path.wav",
   "speaker_wav": "C:/abs/ref.wav" (optional, defaults to bundled reference),
   "language": "en" (optional)}

Response format (one per line on stdout):
  {"id": <int>, "ok": true, "duration_s": <float>, "out": "..."}
  or
  {"id": <int>, "ok": false, "error": "..."}

Special:
  {"id": <int>, "cmd": "ping"}  -> {"id": <int>, "ok": true, "pong": true}
  {"id": <int>, "cmd": "shutdown"} -> exits

T-clip preprocessor: input text ending in T, K, P (voiceless stops) at
utterance-end gets a trailing " ..." appended to coax XTTS into emitting
the full release burst. Diagnosed 2026-05-17 — see xtts_samples test.
"""
from __future__ import annotations
import json
import os
import sys
import time
import io
import traceback

# Force UTF-8 on stdio so stop-hook-style unicode crashes can't happen
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
os.environ["COQUI_TOS_AGREED"] = "1"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _log(msg: str) -> None:
    """Stderr is the log channel; stdout is reserved for protocol JSON."""
    print(f"[xtts_server] {msg}", file=sys.stderr, flush=True)


def _t_clip_fix(text: str) -> str:
    """Append soft trailing context if text ends in a voiceless stop.

    Per diagnostic 2026-05-17: XTTS-v2 vocoder under-emits the release
    burst on word-final T/K/P at utterance end. Adding trailing pauses
    (variant A from the test) gets cutoff_ratio from 0.26 -> 0.41,
    halving the audible clip without changing the words spoken.
    """
    if not text:
        return text
    stripped = text.rstrip(" .!?\n\r\t")
    if not stripped:
        return text
    last_char = stripped[-1].lower()
    # voiceless stops most prone to clipping
    if last_char in ("t", "k", "p"):
        # Preserve original terminal punctuation if present
        if text.rstrip()[-1:] in (".", "!", "?"):
            term = text.rstrip()[-1]
            base = text.rstrip()[:-1].rstrip()
            return f"{base}{term}.."
        return text + "..."
    return text


def _load_model():
    import torch
    _orig_load = torch.load
    def _patched_load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return _orig_load(*args, **kwargs)
    torch.load = _patched_load
    from TTS.api import TTS
    device = "cuda" if torch.cuda.is_available() else "cpu"
    _log(f"loading XTTS-v2 on {device}...")
    t0 = time.time()
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
    _log(f"model ready in {time.time()-t0:.1f}s")
    return tts


# Cache (gpt_cond_latent, speaker_embedding) keyed by speaker_wav path. The
# default reference doesn't change, so we compute conditioning latents once
# and reuse them across every synth. The latents are torch tensors held on
# the device; cache size stays at ~1 entry for our use.
_LATENT_CACHE: dict[str, tuple] = {}


def _get_cached_latents(tts, speaker_wav: str):
    """Return cached (gpt_cond_latent, speaker_embedding) for the given
    speaker_wav, computing once on first call.

    The underlying call is expensive (loads audio, runs reference encoder).
    Doing it once at server startup and caching after avoids paying it per
    synth — which the tts_to_file path was doing implicitly every call.
    """
    if speaker_wav in _LATENT_CACHE:
        return _LATENT_CACHE[speaker_wav]
    model = tts.synthesizer.tts_model
    gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
        audio_path=speaker_wav,
        gpt_cond_len=6,
        gpt_cond_chunk_len=6,
        max_ref_length=30,
    )
    _LATENT_CACHE[speaker_wav] = (gpt_cond_latent, speaker_embedding)
    return _LATENT_CACHE[speaker_wav]


def _synth_streaming_to_wav(tts, text: str, speaker_wav: str, language: str,
                            out_path: str) -> float:
    """Synthesize via inference_stream, concat chunks, write WAV.

    Differences from tts.tts_to_file:
    - Conditioning latents are cached (see _get_cached_latents) — saves ~150-300ms
      per call after the first one on the same speaker_wav.
    - Inference happens chunk-by-chunk via the lower-level inference_stream API,
      which gives us access to stream_chunk_size and other tuning knobs.
    - We still write the full WAV at the end. The protocol with the parent
      doesn't change. Future upgrade: emit per-chunk progress events back to
      the parent so playback can start mid-synth (would need parent-side
      changes in tts_worker._speak_xtts).

    Returns synth wall-clock seconds.
    """
    import torch
    import numpy as np
    import soundfile as sf

    gpt_cond_latent, speaker_embedding = _get_cached_latents(tts, speaker_wav)
    model = tts.synthesizer.tts_model

    t0 = time.time()
    chunks_iter = model.inference_stream(
        text, language,
        gpt_cond_latent, speaker_embedding,
        stream_chunk_size=20,  # ~200ms per chunk first-byte latency
        overlap_wav_len=1024,
        temperature=0.75,
        repetition_penalty=10.0,
        top_k=50,
        top_p=0.85,
        enable_text_splitting=False,
    )

    pcm_parts: list = []
    for wav_chunk in chunks_iter:
        if hasattr(wav_chunk, "cpu"):
            arr = wav_chunk.cpu().numpy()
        else:
            arr = np.asarray(wav_chunk)
        if arr.ndim > 1:
            arr = arr.squeeze()
        pcm_parts.append(arr.astype(np.float32, copy=False))

    if not pcm_parts:
        # Nothing generated — fall back to original path so we don't write
        # an empty WAV. Caller will surface the failure.
        raise RuntimeError("inference_stream yielded no chunks")

    full = np.concatenate(pcm_parts)
    # XTTS-v2 output sample rate is 24000 (not 22050; that's the reference-
    # audio resample rate inside get_conditioning_latents).
    sf.write(out_path, full, samplerate=24000)
    return time.time() - t0


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _default_reference() -> str:
    # Bundled reference: Kokoro Bella's phonetic-rich sample we used for testing
    candidate = r"D:\Wren-Companion\.tmp\voice_test\kokoro_samples\reference.wav"
    return candidate if os.path.exists(candidate) else ""


def main() -> int:
    try:
        tts = _load_model()
    except Exception as e:
        _log(f"FATAL: model load failed: {e!r}")
        _send({"id": -1, "ok": False, "error": f"model_load_failed: {e!r}",
               "traceback": traceback.format_exc()})
        return 2

    # Warmup: synth a representative-length phrase so first real request
    # isn't slow. Short utterances ("Ready.") don't fully warm cuDNN — algo
    # selection re-runs on each new sequence length. A ~12-15 word phrase
    # covers the typical reply shape that gets used immediately after boot.
    # Diagnosed 2026-05-17 (first-call-airy after restart, fix here).
    default_ref = _default_reference()
    warmup_ok = False
    if default_ref:
        try:
            warmup_path = os.path.join(
                os.environ.get("TEMP", os.getcwd()), "xtts_warmup.wav"
            )
            t0 = time.time()
            tts.tts_to_file(
                text="Warming up the voice path so the first real reply doesn't sound airy or cold.",
                speaker_wav=default_ref,
                language="en",
                file_path=warmup_path,
            )
            _log(f"warmup synth done in {time.time()-t0:.1f}s")
            warmup_ok = True
            try:
                os.remove(warmup_path)
            except OSError:
                pass
        except Exception as e:
            _log(f"warmup failed (non-fatal): {e!r}")

    # Signal ready (parent sees warmup_ok so cold-first-call is observable)
    _send({"id": 0, "ok": True, "ready": True, "warmup_ok": warmup_ok})
    _log(f"ready for requests (warmup_ok={warmup_ok})")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _send({"id": -1, "ok": False, "error": f"bad_json: {e!s}"})
            continue
        rid = req.get("id", -1)
        cmd = req.get("cmd")
        if cmd == "ping":
            _send({"id": rid, "ok": True, "pong": True})
            continue
        if cmd == "shutdown":
            _send({"id": rid, "ok": True, "bye": True})
            _log("shutdown requested, exiting")
            return 0
        text = req.get("text") or ""
        out_path = req.get("out")
        if not text or not out_path:
            _send({"id": rid, "ok": False,
                   "error": "missing 'text' or 'out'"})
            continue
        speaker_wav = req.get("speaker_wav") or default_ref
        if not speaker_wav or not os.path.exists(speaker_wav):
            _send({"id": rid, "ok": False,
                   "error": f"speaker_wav not found: {speaker_wav!r}"})
            continue
        language = req.get("language") or "en"
        fixed_text = _t_clip_fix(text)
        try:
            # Use streaming inference with cached latents. The latent cache
            # saves ~150-300ms per call after the first one on each unique
            # speaker_wav (almost always default_ref in our setup, so cache
            # hit ratio is ~99%). Falls back to the legacy tts_to_file path
            # if streaming raises — defense against the inference_stream
            # API surface changing under us.
            try:
                dur = _synth_streaming_to_wav(
                    tts, fixed_text, speaker_wav, language, out_path,
                )
                path_used = "stream"
            except Exception as _se:
                _log(f"streaming synth failed rid={rid}, falling back to tts_to_file: {_se!r}")
                t0 = time.time()
                tts.tts_to_file(
                    text=fixed_text,
                    speaker_wav=speaker_wav,
                    language=language,
                    file_path=out_path,
                )
                dur = time.time() - t0
                path_used = "legacy"
            _send({"id": rid, "ok": True, "out": out_path,
                   "duration_s": round(dur, 3),
                   "text_modified": fixed_text != text,
                   "path": path_used})
        except Exception as e:
            _log(f"synth error rid={rid}: {e!r}")
            _send({"id": rid, "ok": False, "error": str(e),
                   "traceback": traceback.format_exc()})
    _log("stdin closed, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
