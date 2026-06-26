"""wren_speaker_id.py — CAM++ speaker embeddings (the voiceprint layer).

CAM++ (3D-Speaker, Apache-2.0): 7.2M params, ~29MB, 0.65% EER on VoxCeleb1-O,
runs on CPU in ~20-50ms. Picked 2026-06-13 for Wren's voice-recognition build
(notes/voice_ears_research_2026-06-13.md). Two intended uses:
  A) SELF-VOICE suppression — recognize Wren's own TTS so barge-in ignores it on
     SPEAKERS (cosine vs an enrolled wren-voiceprint; AEC stays the primary gate).
  B) Open-set speaker LABELING — enroll people, label per-utterance by cosine.

Runs in nextgen-venv (has torch + modelscope + funasr, same env as SenseVoice ears).
This module is just the embed/cosine primitive + a smoke test; enrollment + the
SenseVoice-server integration are the NEXT build step (not built yet).

  embed(wav_path) -> np.ndarray (L2-normalized speaker embedding)
  cosine(a, b)    -> float in [-1, 1]

CLI:
  # smoke test: same-speaker cosine on the two Wren ref clips (expect HIGH)
  & D:\\Wren\\voice\\nextgen-venv\\Scripts\\python.exe experiments\\wren_speaker_id.py
  # compare any two clips
  ... experiments\\wren_speaker_id.py <wav1> <wav2>
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np

# English VoxCeleb CAM++ on ModelScope (first load downloads ~29MB to the MS cache).
MODEL_ID = "iic/speech_campplus_sv_en_voxceleb_16k"

_PIPELINE = None


def _get_pipeline():
    """Load the CAM++ speaker-verification pipeline once (downloads on first call)."""
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    from modelscope.pipelines import pipeline
    from modelscope.utils.constant import Tasks
    _PIPELINE = pipeline(task=Tasks.speaker_verification, model=MODEL_ID)
    return _PIPELINE


def _to_16k_wav(wav_path: str) -> str:
    """Resample any wav to 16k mono and return a temp path. modelscope's CAM++ pipeline
    resamples non-16k input via torchaudio.sox_effects, which does NOT exist on Windows
    torchaudio (no libsox) -> we pre-resample with torchaudio.functional.resample (pure,
    no sox) so the pipeline always sees 16k and never takes its sox path."""
    import tempfile
    import soundfile as sf
    import torch
    import torchaudio
    data, sr = sf.read(str(wav_path), dtype="float32")
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)            # mono
    t = torch.from_numpy(np.ascontiguousarray(data)).unsqueeze(0)
    if sr != 16000:
        t = torchaudio.functional.resample(t, sr, 16000)
    out = Path(tempfile.gettempdir()) / ("wren_sid_%d.wav" % (abs(hash(str(wav_path))) % 1_000_000))
    sf.write(str(out), t.squeeze(0).numpy(), 16000, subtype="PCM_16")
    return str(out)


def embed(wav_path: str) -> "np.ndarray":
    """Return the L2-normalized CAM++ speaker embedding for a wav (1-D float array).
    Uses the pipeline's output_emb mode; the result is the voiceprint to enroll/compare."""
    sv = _get_pipeline()
    wav16 = _to_16k_wav(wav_path)
    out = sv([wav16], output_emb=True)
    # 3D-Speaker modelscope pipeline returns {'embs': ndarray[N, D], 'outputs': ...}
    embs = out.get("embs") if isinstance(out, dict) else None
    if embs is None:
        raise RuntimeError(f"no embeddings in pipeline output: keys={list(out) if isinstance(out, dict) else type(out)}")
    v = np.asarray(embs)[0].astype(np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def cosine(a: "np.ndarray", b: "np.ndarray") -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _smoke(w1: str, w2: str) -> int:
    print(f"[speaker-id] loading CAM++ ({MODEL_ID}) — downloads on first run...", flush=True)
    sv = _get_pipeline()
    print("[speaker-id] model ready. embedding two clips...", flush=True)
    e1, e2 = embed(w1), embed(w2)
    cs = cosine(e1, e2)
    # Verdict from the cosine we computed (modelscope's raw 2-wav verify call hits the
    # Windows sox limit; embed()+cosine is the path that works). CAM++ EER threshold ~0.5.
    verdict = "SAME speaker" if cs >= 0.5 else "DIFFERENT speakers"
    print(f"[speaker-id] embedding dim = {e1.shape[0]}")
    print(f"[speaker-id] cosine({Path(w1).name}, {Path(w2).name}) = {cs:.4f}")
    print(f"[speaker-id] verdict (cosine>=0.5) = {verdict}")
    print("[speaker-id] (same speaker scores HIGH cosine)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        sys.exit(_smoke(sys.argv[1], sys.argv[2]))
    # default smoke test: the two Wren reference clips (same speaker)
    a = r"D:\Wren\voice\wren_ref.wav"
    b = r"D:\Wren\voice\wren_ref_short.wav"
    sys.exit(_smoke(a, b))
