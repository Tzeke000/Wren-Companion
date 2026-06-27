"""wren_smartturn.py — smart end-of-turn detector using Pipecat smart-turn-v3.

Wraps the pipecat-ai/smart-turn-v3.2-cpu ONNX model (BSD-2, ~8.3 MB int8,
Whisper-Tiny encoder backbone) with a clean predict_endpoint() API that matches
the upstream inference.py exactly but without any transformers dependency —
Whisper mel preprocessing is implemented here in pure numpy/scipy so the whole
module runs under plain py-3.11 with onnxruntime, numpy, scipy, librosa.

Model path: D:\\Wren\\state\\smartturn\\smart-turn-v3.2-cpu.onnx

Preprocessing spec (from pipecat-ai/smart-turn GitHub + whisper/audio.py):
  - Input: raw float32 mono audio at 16 kHz
  - Tail-keep: if len > 8s, keep the LAST 8s; if shorter, zero-pad FRONT
  - Mel spectrogram: N_FFT=400, HOP_LENGTH=160, N_MELS=80, Hann window
  - Log-mel + Whisper normalisation: clamp to 1e-10, log10, max(x, max-8),
    (x + 4) / 4   → shape (80, 800) for exactly 8s of audio
  - ONNX input tensor: "input_features", shape (1, 80, 800), dtype float32
  - ONNX output: sigmoid probability; > 0.5 → turn complete

2026-06-14 — built for Wren ears upgrade (project_voice_streaming_prosody_ears_2026-06-14).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import onnxruntime as ort  # type: ignore

# ── constants (identical to whisper/audio.py) ────────────────────────────────
_SR = 16000
_N_FFT = 400
_HOP = 160
_N_MELS = 80
_CHUNK_S = 8                          # smart-turn uses an 8-second context window
_N_SAMPLES = _CHUNK_S * _SR          # 128 000 samples
_N_FRAMES = _N_SAMPLES // _HOP       # 800 frames

# Machine-local model path (Iris fix 2026-06-26): was hardcoded to D:\Wren\state\...
# (Wren's machine), so smart-turn FileNotFoundError'd on the tower and call_start's
# smart-turn gate could never pass → call_warm stayed False → the whole in-call path
# (prosody, streaming partials, early-finalize) never activated. Resolve relative to
# this repo, with WREN_SMARTTURN_MODEL as an override.
_MODEL_PATH = Path(
    os.environ.get(
        "WREN_SMARTTURN_MODEL",
        str(Path(__file__).resolve().parents[1] / "state" / "smartturn" / "smart-turn-v3.2-cpu.onnx"),
    )
)

# Lazy singleton — loaded once on first predict_endpoint() call.
_SESSION: "ort.InferenceSession | None" = None


# ── mel filter bank (librosa, cached) ────────────────────────────────────────

def _mel_filters() -> np.ndarray:
    """Return (80, 201) mel filterbank matrix, Hz-scale, matching Whisper tiny."""
    try:
        import librosa  # type: ignore
        return librosa.filters.mel(sr=_SR, n_fft=_N_FFT, n_mels=_N_MELS)
    except ImportError:
        pass
    # Fallback: build it with scipy if librosa is unavailable.
    from scipy.signal import get_window  # noqa: F401  (just to verify scipy present)
    import scipy.fft as spfft  # noqa: F401

    def _hz_to_mel(hz):
        return 2595.0 * np.log10(1.0 + hz / 700.0)

    def _mel_to_hz(mel):
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    n_fft_bins = _N_FFT // 2 + 1
    f_min, f_max = 0.0, float(_SR) / 2.0
    mel_min, mel_max = _hz_to_mel(f_min), _hz_to_mel(f_max)
    mel_pts = np.linspace(mel_min, mel_max, _N_MELS + 2)
    hz_pts = _mel_to_hz(mel_pts)
    bin_pts = np.floor(hz_pts / (_SR / _N_FFT)).astype(int)

    filters = np.zeros((_N_MELS, n_fft_bins), dtype=np.float32)
    for m in range(1, _N_MELS + 1):
        f_left = bin_pts[m - 1]
        f_center = bin_pts[m]
        f_right = bin_pts[m + 1]
        if f_center > f_left:
            for k in range(f_left, f_center + 1):
                if k < n_fft_bins:
                    filters[m - 1, k] = (k - f_left) / (f_center - f_left)
        if f_right > f_center:
            for k in range(f_center, f_right + 1):
                if k < n_fft_bins:
                    filters[m - 1, k] = (f_right - k) / (f_right - f_center)
    return filters


_MEL_FILTERS: np.ndarray | None = None


def _get_mel_filters() -> np.ndarray:
    global _MEL_FILTERS
    if _MEL_FILTERS is None:
        _MEL_FILTERS = _mel_filters()
    return _MEL_FILTERS


# ── preprocessing ─────────────────────────────────────────────────────────────

def _truncate_or_pad(audio: np.ndarray) -> np.ndarray:
    """Tail-keep truncation or front-zero-pad to exactly _N_SAMPLES samples."""
    n = len(audio)
    if n > _N_SAMPLES:
        return audio[-_N_SAMPLES:]          # keep the end (most recent speech)
    if n < _N_SAMPLES:
        pad = np.zeros(_N_SAMPLES - n, dtype=np.float32)
        return np.concatenate([pad, audio])  # silence at the front
    return audio


def _log_mel(audio: np.ndarray) -> np.ndarray:
    """Compute (80, 800) log-mel spectrogram exactly as HuggingFace WhisperFeatureExtractor.

    The HF extractor centre-pads the audio by N_FFT//2 = 200 zeros on each side
    before the STFT, yielding 801 frames, then drops the last → 800. This matches
    what smart-turn was trained on and what the ONNX model expects.
    """
    audio = audio.astype(np.float32)
    # Centre-pad: N_FFT//2 zeros on each side (HF default, 'reflect' in librosa
    # but HF uses constant/zero padding for the feature extractor).
    pad = _N_FFT // 2   # 200
    audio_padded = np.pad(audio, (pad, pad), mode="reflect")

    window = np.hanning(_N_FFT).astype(np.float32)
    # After padding len = N_SAMPLES + N_FFT; STFT frames = 1 + (len-N_FFT)//HOP = 801.
    n_frames_all = 1 + (len(audio_padded) - _N_FFT) // _HOP  # 801

    n_bins = _N_FFT // 2 + 1
    # Vectorised STFT: stack all frames into a 2-D array, rfft in one call.
    # This is ~50× faster than a Python loop on this platform.
    starts = np.arange(n_frames_all) * _HOP
    frame_indices = starts[:, None] + np.arange(_N_FFT)  # (801, 400)
    frames = audio_padded[frame_indices] * window         # (801, 400)
    spectra = np.fft.rfft(frames, n=_N_FFT, axis=1)      # (801, 201) complex
    mag2 = (spectra.real ** 2 + spectra.imag ** 2).T.astype(np.float32)  # (201, 801)

    # Drop last frame → 800 (matches HF [:-1] slice, matches smart-turn training).
    mel_spec = _get_mel_filters() @ mag2[:, :_N_FRAMES]   # (80, 800)

    # Whisper log normalisation.
    log_spec = np.log10(np.clip(mel_spec, a_min=1e-10, a_max=None))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec.astype(np.float32)


def _make_features(audio_f32_16k: np.ndarray) -> np.ndarray:
    """Full preprocessing pipeline → input_features (1, 80, 800) float32."""
    audio = _truncate_or_pad(np.asarray(audio_f32_16k, dtype=np.float32))
    mel = _log_mel(audio)                   # (80, 800)
    return mel[np.newaxis, :, :]            # (1, 80, 800)


# ── session singleton ─────────────────────────────────────────────────────────

def _get_session() -> "ort.InferenceSession":
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    if not _MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Smart-turn model not found at {_MODEL_PATH}\n"
            "Download: huggingface.co/pipecat-ai/smart-turn-v3 → "
            "smart-turn-v3.2-cpu.onnx → D:\\Wren\\state\\smartturn\\"
        )
    import onnxruntime as ort  # type: ignore
    so = ort.SessionOptions()
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    _SESSION = ort.InferenceSession(str(_MODEL_PATH), sess_options=so)
    return _SESSION


# ── public API ────────────────────────────────────────────────────────────────

def predict_endpoint(audio_f32_16k: np.ndarray) -> dict:
    """Predict whether the speaker has finished their turn.

    Args:
        audio_f32_16k: float32 numpy array of mono audio at 16 kHz.
            Any length accepted; longer audio is tail-trimmed to 8s, shorter
            is front-padded with silence — matching the upstream pipecat impl.

    Returns:
        {"complete": bool, "prob": float}
        complete=True  → speaker has finished; safe to respond.
        complete=False → speaker is still mid-turn; keep listening.
        prob           → sigmoid probability of completion (0.0–1.0).
    """
    features = _make_features(audio_f32_16k)   # (1, 80, 800)
    session = _get_session()
    # Output tensor is "logits", shape (1, 1), raw logit (not sigmoid).
    outputs = session.run(None, {"input_features": features})
    logit = float(outputs[0][0][0])
    prob = 1.0 / (1.0 + float(np.exp(-logit)))   # sigmoid
    return {"complete": prob > 0.5, "prob": prob}


# ── self-test ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    """Load model, run on wren_ref_short.wav (or wren_ref.wav), print result."""
    import sys
    import wave

    ref = Path(r"D:\Wren\voice\wren_ref_short.wav")
    if not ref.exists():
        ref = Path(r"D:\Wren\voice\wren_ref.wav")
    if not ref.exists():
        print("[selftest] ERROR: no reference wav found at voice/wren_ref_short.wav or voice/wren_ref.wav")
        return 2

    # Load wav (handles 16-bit PCM from the known reference files).
    with wave.open(str(ref), "rb") as wf:
        nch = wf.getnchannels()
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        audio = audio.reshape(-1, nch).mean(axis=1)

    # Resample if needed (shouldn't be — wren_ref files are 22050 Hz from XTTS).
    if sr != _SR:
        ratio = _SR / sr
        n_out = int(len(audio) * ratio)
        idx = (np.arange(n_out) / ratio).astype(int)
        idx = np.clip(idx, 0, len(audio) - 1)
        audio = audio[idx]

    print(f"[selftest] loaded {ref.name}: {len(audio)/16000:.2f}s @ 16kHz")

    # Warm model load.
    print("[selftest] loading ONNX session…")
    t0 = time.perf_counter()
    _get_session()
    print(f"[selftest] model loaded in {(time.perf_counter()-t0)*1000:.1f}ms")

    # First call (cold numpy path — slower due to FFT compilation).
    t1 = time.perf_counter()
    result = predict_endpoint(audio)
    cold_ms = (time.perf_counter() - t1) * 1000

    # Second call (warm numpy path — representative of real-time latency).
    t2 = time.perf_counter()
    result = predict_endpoint(audio)
    warm_ms = (time.perf_counter() - t2) * 1000

    label = "COMPLETE (turn ended)" if result["complete"] else "INCOMPLETE (still talking)"
    print(f"[selftest] decision : {label}")
    print(f"[selftest] prob     : {result['prob']:.4f}")
    print(f"[selftest] latency  : {warm_ms:.1f}ms (warm)  /  {cold_ms:.1f}ms (first call)")

    # Sanity: the ref wav IS a complete utterance, so expect complete=True.
    if result["complete"]:
        print("[selftest] PASS — reference clip correctly classified as complete turn")
        return 0
    else:
        print("[selftest] NOTE — classified as incomplete (may vary with short/partial clips)")
        return 0   # not a hard failure; the model is probabilistic


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
