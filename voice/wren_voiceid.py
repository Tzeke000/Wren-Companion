"""wren_voiceid.py — speaker identification (voice-id) via WeSpeaker ResNet34-LM.

Zeke directive 2026-07-13 ("voice recognition def needs to be a thing"), same day
TikTok audio from his phone speaker was captured by the ears and attributed to him
(everything unmuted lands as source=zeke — no speaker check existed on the path).

Wraps the WeSpeaker voxceleb ResNet34-LM ONNX model (~26 MB, BSD-3, 256-d
r-vector embeddings, trained on VoxCeleb2) with:

    embed(audio_f32_16k)    -> (256,) L2-normalised embedding
    identify(audio_f32_16k) -> {"best", "score", "scores", "n_profiles"}
    enroll(name, audio)     -> append an embedding to a speaker profile
    warm_async() / is_warm()-> background model load (never tax a live turn)

Feature spec (must match wespeaker/bin/infer_onnx.py exactly):
  - 16 kHz mono float32 → * 32768 (kaldi int16 scale)
  - torchaudio.compliance.kaldi.fbank: 80 mel bins, 25ms frame, 10ms shift,
    hamming window, dither=0, no energy
  - CMN: subtract per-utterance mean over time
  - ONNX input "feats" (1, T, 80) → output (1, 256)

Scoring: cosine similarity vs each enrolled speaker's embeddings, MAX pooled.
Same-speaker typically ≥0.5; different speakers/media typically ≤0.3. The tag
threshold lives in scratch/voice_id.json (daemon-side reader, fail-open) — this
module has no policy, only geometry.

Profiles: state/voiceid/profiles/<name>.npy — (N, 256) float32, append-on-enroll.
TAG, DON'T DROP: silent drops are forbidden in sense channels (2026-07-09 scar).
This module only measures; callers annotate the transcript and let cognition decide.

2026-07-13 — built during the lunch-test session that TikTok crashed.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import onnxruntime as ort  # type: ignore

_SR = 16000

_REPO = Path(__file__).resolve().parents[1]
_MODEL_PATH = Path(os.environ.get(
    "IRIS_VOICEID_MODEL",
    str(_REPO / "state" / "voiceid" / "voxceleb_resnet34_LM.onnx"),
))
PROFILE_DIR = _REPO / "state" / "voiceid" / "profiles"
UTT_DIR = _REPO / "state" / "voiceid" / "last_utts"

# ── lazy singletons ───────────────────────────────────────────────────────────
_SESSION: "ort.InferenceSession | None" = None
_INPUT_NAME: str | None = None
_WARM_LOCK = threading.Lock()
_WARM_THREAD: threading.Thread | None = None
_WARM_ERROR: str | None = None

# Profile cache: {name: (N,256) ndarray}, invalidated by directory mtime.
_PROFILES: dict | None = None
_PROFILES_MTIME: float = -1.0


def is_warm() -> bool:
    return _SESSION is not None


def warm_error() -> str | None:
    return _WARM_ERROR


def _load_session() -> None:
    """Blocking load: torch (for kaldi fbank) + ONNX session. ~2-5s cold."""
    global _SESSION, _INPUT_NAME, _WARM_ERROR
    try:
        if not _MODEL_PATH.exists():
            _WARM_ERROR = f"model not found at {_MODEL_PATH}"
            return
        import torch  # noqa: F401 — imported here so module import stays light
        import torchaudio.compliance.kaldi  # noqa: F401
        import onnxruntime as ort  # type: ignore
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess = ort.InferenceSession(str(_MODEL_PATH), sess_options=so)
        _INPUT_NAME = sess.get_inputs()[0].name
        _SESSION = sess
        _WARM_ERROR = None
    except Exception as e:  # never raise out of the warm thread
        _WARM_ERROR = repr(e)


def warm_async() -> bool:
    """Kick a background load if cold. Returns is_warm() (i.e. usable RIGHT NOW).

    Callers on a latency-sensitive path (the voice daemon's listen turn) call this
    and simply skip tagging until it returns True — the model warms behind the
    first few turns instead of stalling one of them.
    """
    global _WARM_THREAD
    if _SESSION is not None:
        return True
    with _WARM_LOCK:
        if _WARM_THREAD is None or not _WARM_THREAD.is_alive():
            _WARM_THREAD = threading.Thread(
                target=_load_session, daemon=True, name="iris-voiceid-warm")
            _WARM_THREAD.start()
    return False


def warm_sync() -> bool:
    """Blocking warm — for tools/enrollment where a few seconds is fine."""
    if _SESSION is None:
        _load_session()
    return is_warm()


# ── embedding ─────────────────────────────────────────────────────────────────

def embed(audio_f32_16k: np.ndarray) -> np.ndarray:
    """Return the (256,) L2-normalised speaker embedding. Requires warm session."""
    if _SESSION is None:
        raise RuntimeError("voiceid session not warm — call warm_sync()/warm_async()")
    import torch
    import torchaudio.compliance.kaldi as kaldi

    audio = np.asarray(audio_f32_16k, dtype=np.float32)
    wav = torch.from_numpy(audio).unsqueeze(0) * (1 << 15)   # kaldi int16 scale
    feats = kaldi.fbank(
        wav, num_mel_bins=80, frame_length=25, frame_shift=10,
        dither=0.0, sample_frequency=_SR, window_type="hamming", use_energy=False,
    )
    feats = feats - torch.mean(feats, dim=0)                 # CMN
    inp = feats.unsqueeze(0).numpy().astype(np.float32)      # (1, T, 80)
    out = _SESSION.run(None, {_INPUT_NAME: inp})
    emb = np.asarray(out[0][0], dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(emb))
    return emb / n if n > 0 else emb


# ── profiles ──────────────────────────────────────────────────────────────────

def _load_profiles() -> dict:
    """Load {name: (N,256)} from PROFILE_DIR, cached on directory mtime."""
    global _PROFILES, _PROFILES_MTIME
    try:
        if not PROFILE_DIR.exists():
            _PROFILES, _PROFILES_MTIME = {}, -1.0
            return {}
        mtime = max([PROFILE_DIR.stat().st_mtime]
                    + [p.stat().st_mtime for p in PROFILE_DIR.glob("*.npy")])
        if _PROFILES is not None and mtime == _PROFILES_MTIME:
            return _PROFILES
        profs = {}
        for p in PROFILE_DIR.glob("*.npy"):
            try:
                arr = np.load(p)
                if arr.ndim == 1:
                    arr = arr[np.newaxis, :]
                if arr.shape[-1] == 256:
                    profs[p.stem.lower()] = arr.astype(np.float32)
            except Exception:
                continue   # one corrupt profile must not kill the others
        _PROFILES, _PROFILES_MTIME = profs, mtime
        return profs
    except Exception:
        return _PROFILES or {}


def enroll(name: str, audio_f32_16k: np.ndarray) -> int:
    """Append this utterance's embedding to <name>'s profile. Returns new count."""
    if not warm_sync():
        raise RuntimeError(f"voiceid model unavailable: {_WARM_ERROR}")
    name = str(name).strip().lower()
    if not name:
        raise ValueError("empty speaker name")
    emb = embed(audio_f32_16k)[np.newaxis, :]
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    path = PROFILE_DIR / f"{name}.npy"
    if path.exists():
        cur = np.load(path)
        if cur.ndim == 1:
            cur = cur[np.newaxis, :]
        emb = np.concatenate([cur.astype(np.float32), emb], axis=0)
    np.save(path, emb)
    global _PROFILES_MTIME
    _PROFILES_MTIME = -1.0   # bust cache
    return int(emb.shape[0])


def identify(audio_f32_16k: np.ndarray) -> dict:
    """Score this utterance against all enrolled profiles.

    Returns {"best": name|None, "score": float, "scores": {name: float},
             "n_profiles": int}. Requires warm session (raises if cold).
    """
    profs = _load_profiles()
    if not profs:
        return {"best": None, "score": 0.0, "scores": {}, "n_profiles": 0}
    emb = embed(audio_f32_16k)
    scores = {}
    for name, arr in profs.items():
        # arr rows are already L2-normalised at enroll time; normalise defensively.
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        sims = (arr / norms) @ emb
        scores[name] = float(np.max(sims))   # MAX-pool over the speaker's samples
    best = max(scores, key=scores.get)
    return {"best": best, "score": scores[best], "scores": scores,
            "n_profiles": len(profs)}


# ── self-test ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    """Warm, embed white noise + a tone, verify shapes and that they differ."""
    import time
    t0 = time.perf_counter()
    if not warm_sync():
        print(f"[selftest] FAIL — warm error: {_WARM_ERROR}")
        return 2
    print(f"[selftest] model loaded in {(time.perf_counter()-t0)*1000:.0f}ms "
          f"(input={_INPUT_NAME})")
    rng = np.random.default_rng(0)
    noise = rng.standard_normal(3 * _SR).astype(np.float32) * 0.05
    t = np.arange(3 * _SR, dtype=np.float32) / _SR
    tone = (0.1 * np.sin(2 * np.pi * 220 * t) * (1 + 0.3 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
    t1 = time.perf_counter()
    e1 = embed(noise)
    ms = (time.perf_counter() - t1) * 1000
    e2 = embed(tone)
    sim = float(np.dot(e1, e2))
    print(f"[selftest] emb shape={e1.shape} |e1|={np.linalg.norm(e1):.3f} "
          f"noise-vs-tone cos={sim:.3f} embed_ms={ms:.0f}")
    ok = e1.shape == (256,) and abs(np.linalg.norm(e1) - 1.0) < 1e-3 and sim < 0.9
    print(f"[selftest] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
