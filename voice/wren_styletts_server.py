"""wren_styletts_server.py — warm StyleTTS2 mouth server (drop-in for XTTS/Chatterbox/CosyVoice).

Same HTTP interface as the other mouth servers so voice_backend() can cold-start it:
    POST /        text -> synthesize in Wren's cloned voice -> play on OS-default output
    GET  /health  -> "ok" once the model is warm, "loading" before
    GET  /stop    -> barge-in: abort the in-flight playback

Based on the PROVEN inference path from scratch/voice_ab/render_styletts2.py (the clip
Zeke approved the timbre of, 2026-06-13). Plays through the same sounddevice
OutputStream machinery the XTTS + CosyVoice servers use (follows the live OS-default
output device, persistent stream, barge-in stop flag).

Venv: D:\\Wren\\voice\\style-venv
Run:
    & D:\\Wren\\voice\\style-venv\\Scripts\\python.exe experiments\\wren_styletts_server.py
Port: WREN_VOICE_PORT env var (default 8769).
Speed: WREN_STYLE_SPEED env var (default 1.28 — Zeke's locked pace, 2026-06-13 A/B listen).

2026-06-13 (Wren)
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── espeak-ng via bundled espeakng-loader (no system install) — Iris adaptation ──
# Wren's original hardcoded C:\Program Files\eSpeak NG\libespeak-ng.dll. On the tower
# eSpeak NG isn't installed; espeakng-loader 0.2.4 bundles the DLL + data, so point
# phonemizer at the bundled copy instead (avoids the admin system-install entirely).
import espeakng_loader as _espeak
os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = _espeak.get_library_path()
os.environ.setdefault("ESPEAK_DATA_PATH", _espeak.get_data_path())

# ── Must run from the StyleTTS2 repo root so local relative imports resolve ──────
REPO = Path(r"D:\Wren-Companion\voice\StyleTTS2")
os.chdir(str(REPO))
sys.path.insert(0, str(REPO))

warnings.filterwarnings("ignore")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── Config constants (tune via env vars) ─────────────────────────────────────────
# ── MY voice — the iris_voice_reference.wav (ElevenLabs clone Zeke ear-approved,
# made 2026-06-26). ALPHA=0.0 below = max timbre match → this clip IS the voice. ──
REF_WAV = Path(r"D:\Wren-Companion\models\voice\iris_voice_reference.wav")
CONFIG_PATH = str(REPO / "Models" / "LibriTTS" / "config.yml")
CKPT_PATH   = str(REPO / "Models" / "LibriTTS" / "epochs_2nd_00020.pth")

HOST = "127.0.0.1"
PORT = int(os.environ.get("WREN_VOICE_PORT", "8769"))
# 1.28 = Zeke's LOCKED pace (2026-06-13 A/B listen: picked 1.25 as closest, then
# confirmed 1.28 from the 1.28/1.30 pair). speed>1 divides the predicted durations →
# faster speech; only timing, not timbre. Override with WREN_STYLE_SPEED if ever retuned.
SPEED = float(os.environ.get("WREN_STYLE_SPEED", "1.28"))
# StyleTTS2 approved params (render_styletts2.py values Zeke ear-approved):
ALPHA           = 0.0   # max timbre similarity to reference (zero-shot cloning)
BETA            = 0.5   # moderate prosody from diffusion (not fully deterministic)
DIFFUSION_STEPS = 5
SAMPLE_RATE     = 24000  # StyleTTS2 LibriTTS checkpoint outputs 24kHz

_SPEAK_LOCK = threading.Lock()
_READY      = False
_STOP       = threading.Event()

# ── Dependency imports (inside style-venv) ────────────────────────────────────────
try:
    import numpy as np
    import sounddevice as sd
    import torch
    import torchaudio
    import librosa
    import yaml
    from munch import Munch
    from nltk.tokenize import word_tokenize
except ImportError as e:
    print(f"[styletts] missing dep: {e!r}  (run inside style-venv)", flush=True)
    sys.exit(2)

# ── torch.load weights_only compat (Iris adaptation) ──────────────────────────────
# torch>=2.6 flipped torch.load's default to weights_only=True, which rejects the
# StyleTTS2 + LibriTTS checkpoints (they pickle getattr/globals). These are trusted
# official weights, so force weights_only=False for every load in the repo + server.
# (Wren runs the same torch; her LOCAL StyleTTS2 copy was already patched — the fresh
# github clone @ 5cedc71 isn't. This restores parity.)
_orig_torch_load = torch.load
def _torch_load_compat(*a, **k):
    k.setdefault("weights_only", False)
    return _orig_torch_load(*a, **k)
torch.load = _torch_load_compat

# ── StyleTTS2 local imports (repo must be on sys.path / cwd) ─────────────────────
try:
    from models import build_model, load_ASR_models, load_F0_models
    from utils import recursive_munch
    from text_utils import TextCleaner
    from Utils.PLBERT.util import load_plbert
    from Modules.diffusion.sampler import DiffusionSampler, ADPM2Sampler, KarrasSchedule
except ImportError as e:
    print(f"[styletts] StyleTTS2 import failed: {e!r}  (cwd must be {REPO})", flush=True)
    sys.exit(2)

try:
    import phonemizer as _ph
except ImportError as e:
    print(f"[styletts] phonemizer missing: {e!r}", flush=True)
    sys.exit(2)

# ── Shared voice-state overlay (no-op if helper absent) ──────────────────────────
try:
    from wren_voice_status import set_state  # type: ignore
except Exception:
    def set_state(*_a, **_k):  # type: ignore
        pass

# ── OS-default output machinery (mirrors wren_cosyvoice_server.py verbatim) ──────
_OUT_STREAM    = None
_OUT_DEV       = None
_LAST_DEF_ID   = None
FOLLOW_OS_DEFAULT = True


def _current_default_endpoint_id():
    try:
        from pycaw.pycaw import AudioUtilities  # type: ignore
        return AudioUtilities.GetSpeakers().id
    except Exception:
        return None


def _os_default_output_index():
    global _OUT_STREAM, _OUT_DEV
    if _OUT_STREAM is not None:
        try:
            _OUT_STREAM.stop(); _OUT_STREAM.close()
        except Exception:
            pass
        _OUT_STREAM = None
        _OUT_DEV = None
    try:
        sd._terminate(); sd._initialize()
    except Exception:
        pass
    try:
        out = sd.default.device[1]
        if isinstance(out, int) and out >= 0 and \
                sd.query_devices(out).get("max_output_channels", 0) > 0:
            return out
    except Exception:
        pass
    try:
        nm = sd.query_devices(kind="output").get("name")
        for i, dev in enumerate(sd.query_devices()):
            if dev.get("name") == nm and dev.get("max_output_channels", 0) > 0:
                return i
    except Exception:
        pass
    return None


def _resolve_output_device():
    global _LAST_DEF_ID
    if not FOLLOW_OS_DEFAULT:
        return None
    cur = _current_default_endpoint_id()
    if (cur is not None and cur == _LAST_DEF_ID
            and _OUT_STREAM is not None and _OUT_DEV is not None):
        return _OUT_DEV
    idx = _os_default_output_index()
    if cur is not None:
        _LAST_DEF_ID = cur
    return idx


def _get_output_stream(dev_idx):
    global _OUT_STREAM, _OUT_DEV
    if _OUT_STREAM is not None and _OUT_DEV == dev_idx:
        if not _OUT_STREAM.active:
            try:
                _OUT_STREAM.start()
            except Exception:
                try:
                    _OUT_STREAM.close()
                except Exception:
                    pass
                _OUT_STREAM = None
        if _OUT_STREAM is not None:
            return _OUT_STREAM
    if _OUT_STREAM is not None:
        try:
            _OUT_STREAM.stop(); _OUT_STREAM.close()
        except Exception:
            pass
        _OUT_STREAM = None
    _OUT_STREAM = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                  device=dev_idx)
    _OUT_STREAM.start()
    _OUT_DEV = dev_idx
    return _OUT_STREAM


def _request_stop():
    """Interrupt in-flight playback (barge-in). Sets flag + aborts stream."""
    _STOP.set()
    s = _OUT_STREAM
    if s is not None:
        try:
            s.abort()
        except Exception:
            pass


def _cold_unload() -> None:
    """Unload StyleTTS2 model + free VRAM; keep the HTTP server listening.
    Next /warm or POST will see _READY=False and return 503; the process stays
    alive so voice_call_start can trigger a re-warm without a full restart.
    Currently re-warm via /cold -> voice_call_start POST is NOT auto-wired —
    call voice_backend('styletts2','start') or restart the server to reload.
    (Re-warm-in-process would need a background thread load, deferred for now.)"""
    global _model, _model_params, _sampler, _textcleaner, _phonemizer, _ref_s, _READY
    _READY = False  # set first so new POSTs get 503 immediately
    try:
        _FILLER_CACHE.clear()
    except Exception:
        pass
    for name in ("_model", "_model_params", "_sampler", "_textcleaner", "_phonemizer", "_ref_s"):
        try:
            globals()[name] = None
        except Exception:
            pass
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
    print("[styletts] /cold — model unloaded, VRAM freed", flush=True)


# ── Re-warm after a /cold (background reload so the mouth comes back w/o a restart) ─
_LOADING = False
_LOADING_LOCK = threading.Lock()


def _rewarm_async() -> None:
    """Reload the model + warm in a BACKGROUND thread after a /cold, so a COLDed mouth
    comes back without a full server restart (the warm/cold/warm-again cycle for calls).
    No-op if already ready or already loading. _load_model/_warmup are defined below;
    they're resolved at call time so the forward reference is fine."""
    global _LOADING
    with _LOADING_LOCK:
        if _READY or _LOADING:
            return
        _LOADING = True

    def _job():
        global _LOADING
        try:
            print("[styletts] /warm — reloading model after cold ...", flush=True)
            _load_model()
            _warmup()          # recomputes style, primes fillers, sets _READY=True
            print("[styletts] /warm — re-warm complete", flush=True)
        except Exception as e:
            print(f"[styletts] re-warm FAILED: {e!r}", flush=True)
        finally:
            with _LOADING_LOCK:
                _LOADING = False

    threading.Thread(target=_job, daemon=True, name="styletts-rewarm").start()


# ── StyleTTS2 mel helpers (verbatim from render_styletts2.py) ────────────────────
to_mel = torchaudio.transforms.MelSpectrogram(
    n_mels=80, n_fft=2048, win_length=1200, hop_length=300
)
MEAN, STD = -4, 4


def _length_to_mask(lengths):
    mask = (
        torch.arange(lengths.max())
        .unsqueeze(0)
        .expand(lengths.shape[0], -1)
        .type_as(lengths)
    )
    return torch.gt(mask + 1, lengths.unsqueeze(1))


def _preprocess(wave):
    wt = torch.from_numpy(wave).float()
    mel = to_mel(wt)
    return (torch.log(1e-5 + mel.unsqueeze(0)) - MEAN) / STD


# ── Model globals ─────────────────────────────────────────────────────────────────
_device     = "cuda" if torch.cuda.is_available() else "cpu"
_model      = None    # built once in _load_model()
_model_params = None
_sampler    = None
_textcleaner = None
_phonemizer = None
_ref_s      = None    # wren_ref.wav style embedding, cached after first compute


def _load_model() -> None:
    global _model, _model_params, _sampler, _textcleaner, _phonemizer
    print(f"[styletts] device={_device}", flush=True)
    torch.manual_seed(0)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

    # Phonemizer + text cleaner
    _phonemizer = _ph.backend.EspeakBackend(
        language="en-us", preserve_punctuation=True, with_stress=True
    )
    _textcleaner = TextCleaner()

    print("[styletts] loading config + model ...", flush=True)
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    _model_params = recursive_munch(config["model_params"])
    text_aligner  = load_ASR_models(config["ASR_path"], config["ASR_config"])
    pitch_extractor = load_F0_models(config["F0_path"])
    plbert        = load_plbert(config["PLBERT_dir"])

    _model = build_model(_model_params, text_aligner, pitch_extractor, plbert)
    _ = [_model[k].eval() for k in _model]
    _ = [_model[k].to(_device) for k in _model]

    print("[styletts] loading checkpoint ...", flush=True)
    params_whole = torch.load(CKPT_PATH, map_location="cpu")
    params = params_whole["net"]
    for key in _model:
        if key in params:
            try:
                _model[key].load_state_dict(params[key])
            except Exception:
                from collections import OrderedDict
                sd_dict = params[key]
                new_sd = OrderedDict()
                for k, v in sd_dict.items():
                    new_sd[k[7:] if k.startswith("module.") else k] = v
                _model[key].load_state_dict(new_sd, strict=False)
    _ = [_model[k].eval() for k in _model]
    print("[styletts] checkpoint loaded", flush=True)

    _sampler = DiffusionSampler(
        _model.diffusion.diffusion,
        sampler=ADPM2Sampler(),
        sigma_schedule=KarrasSchedule(sigma_min=0.0001, sigma_max=3.0, rho=9.0),
        clamp=False,
    )
    print("[styletts] diffusion sampler ready", flush=True)


def _compute_style(path: str) -> torch.Tensor:
    """Compute style embedding from reference wav (verbatim from render_styletts2.py)."""
    wave, sr = librosa.load(path, sr=24000)
    audio, _ = librosa.effects.trim(wave, top_db=30)
    if sr != 24000:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=24000)
    mel_tensor = _preprocess(audio).to(_device)
    with torch.no_grad():
        ref_s_enc  = _model.style_encoder(mel_tensor.unsqueeze(1))
        ref_p_enc  = _model.predictor_encoder(mel_tensor.unsqueeze(1))
    return torch.cat([ref_s_enc, ref_p_enc], dim=1)


def _synth(text: str, ref_s: torch.Tensor,
           alpha=ALPHA, beta=BETA,
           diffusion_steps=DIFFUSION_STEPS,
           embedding_scale=1,
           speed=SPEED) -> np.ndarray:
    """Synthesize text → float32 numpy array at 24kHz.
    Verbatim from render_styletts2.py inference() — only the global names changed."""
    text = text.strip()
    ps = _phonemizer.phonemize([text])
    ps = word_tokenize(ps[0])
    ps = " ".join(ps)
    tokens = _textcleaner(ps)
    tokens.insert(0, 0)
    tokens = torch.LongTensor(tokens).to(_device).unsqueeze(0)

    with torch.no_grad():
        input_lengths = torch.LongTensor([tokens.shape[-1]]).to(_device)
        text_mask = _length_to_mask(input_lengths).to(_device)

        t_en    = _model.text_encoder(tokens, input_lengths, text_mask)
        bert_dur = _model.bert(tokens, attention_mask=(~text_mask).int())
        d_en    = _model.bert_encoder(bert_dur).transpose(-1, -2)

        s_pred = _sampler(
            noise=torch.randn((1, 256)).unsqueeze(1).to(_device),
            embedding=bert_dur,
            embedding_scale=embedding_scale,
            features=ref_s,
            num_steps=diffusion_steps,
        ).squeeze(1)

        s   = s_pred[:, 128:]
        ref = s_pred[:, :128]

        ref = alpha * ref + (1 - alpha) * ref_s[:, :128]
        s   = beta  * s   + (1 - beta)  * ref_s[:, 128:]

        d = _model.predictor.text_encoder(d_en, s, input_lengths, text_mask)
        x, _ = _model.predictor.lstm(d)
        duration = _model.predictor.duration_proj(x)
        duration = torch.sigmoid(duration).sum(axis=-1)
        # speed > 1 → shorter durations → faster speech (only timing, not timbre)
        pred_dur = torch.round(duration.squeeze() / speed).clamp(min=1)

        pred_aln_trg = torch.zeros(input_lengths, int(pred_dur.sum().data))
        c_frame = 0
        for i in range(pred_aln_trg.size(0)):
            pred_aln_trg[i, c_frame : c_frame + int(pred_dur[i].data)] = 1
            c_frame += int(pred_dur[i].data)

        en = d.transpose(-1, -2) @ pred_aln_trg.unsqueeze(0).to(_device)
        if _model_params.decoder.type == "hifigan":
            asr_new = torch.zeros_like(en)
            asr_new[:, :, 0] = en[:, :, 0]
            asr_new[:, :, 1:] = en[:, :, 0:-1]
            en = asr_new

        F0_pred, N_pred = _model.predictor.F0Ntrain(en, s)

        asr = t_en @ pred_aln_trg.unsqueeze(0).to(_device)
        if _model_params.decoder.type == "hifigan":
            asr_new = torch.zeros_like(asr)
            asr_new[:, :, 0] = asr[:, :, 0]
            asr_new[:, :, 1:] = asr[:, :, 0:-1]
            asr = asr_new

        out = _model.decoder(asr, F0_pred, N_pred, ref.squeeze().unsqueeze(0))

    # trim the trailing pulse the notebook warns about
    return out.squeeze().cpu().numpy()[..., :-50]


# ── Progressive chunking (same split as the other servers) ───────────────────────
_CLAUSE_BOUNDARY = re.compile(r'(?<=[,;:.!?—])\s+')


def _split_progressive(text: str, first=4, factor=2, max_budget=16) -> list:
    text = (text or "").strip()
    if not text:
        return []
    segs = [s.strip() for s in _CLAUSE_BOUNDARY.split(text) if s.strip()]
    if not segs:
        return [text]
    chunks: list = []
    budget = max(1, first)
    i = 0
    while i < len(segs):
        cur: list = []
        wc = 0
        while i < len(segs) and (wc == 0 or wc < budget):
            wc += len(segs[i].split())
            cur.append(segs[i])
            i += 1
        chunks.append(" ".join(cur))
        budget = min(max_budget, budget * factor)
    return chunks


# ── Filler cache (same design as XTTS / CosyVoice servers) ───────────────────────
FILLER_PHRASES = ("Mm.", "Right.", "Okay—", "So—", "Let me think.")
_FILLER_CACHE: list = []
_FILLER_IDX = 0


def _prime_fillers() -> int:
    global _FILLER_CACHE
    if _FILLER_CACHE:
        return len(_FILLER_CACHE)
    rendered = []
    for ph in FILLER_PHRASES:
        try:
            arr = _synth(ph, _ref_s)
            if arr is not None and getattr(arr, "size", 0):
                rendered.append(arr.astype(np.float32, copy=False))
        except Exception as e:
            print(f"[styletts] filler prime failed for {ph!r}: {e!r}", flush=True)
    _FILLER_CACHE = rendered
    print(f"[styletts] primed {len(_FILLER_CACHE)} filler clips", flush=True)
    return len(_FILLER_CACHE)


def _next_filler():
    global _FILLER_IDX
    if not _FILLER_CACHE:
        return None
    arr = _FILLER_CACHE[_FILLER_IDX % len(_FILLER_CACHE)]
    _FILLER_IDX += 1
    return arr


# ── Speak path ────────────────────────────────────────────────────────────────────

def _play(arr: np.ndarray, pad_sec: float = 0.35) -> None:
    """Write a float32 array to the persistent output stream in stop-aware chunks."""
    if pad_sec > 0:
        arr = np.concatenate([arr, np.zeros(int(SAMPLE_RATE * pad_sec), dtype=np.float32)])
    dev_idx = _resolve_output_device()
    stream  = _get_output_stream(dev_idx)
    chunk   = SAMPLE_RATE * 2   # 2s chunks keeps the write loop responsive to _STOP
    i = 0
    while i < len(arr):
        if _STOP.is_set():
            break
        try:
            stream.write(arr[i : i + chunk].astype(np.float32, copy=False))
        except Exception:
            break
        i += chunk


def speak_progressive(text: str) -> dict:
    """Synthesize text in progressive chunks; play through persistent output stream.
    Returns timing dict."""
    chunks = _split_progressive(text)
    if not chunks:
        return {"chars": 0, "first_audio_s": None, "total_s": 0.0}
    _STOP.clear()
    t0 = time.perf_counter()
    dev_idx = _resolve_output_device()
    stream  = _get_output_stream(dev_idx)
    wav_q: "queue.Queue" = queue.Queue(maxsize=6)

    def _produce():
        for c in chunks:
            if _STOP.is_set():
                break
            try:
                arr = _synth(c, _ref_s)
                arr = np.clip(arr.astype(np.float32, copy=False), -1.0, 1.0)
            except Exception as e:
                print(f"[styletts] synth failed for {c[:40]!r}: {e!r}", flush=True)
                arr = None
            wav_q.put(arr)
        wav_q.put(None)

    threading.Thread(target=_produce, daemon=True, name="styletts-prog-synth").start()

    set_state("speaking")
    first_audio = -1.0
    fillers = 0

    def _write(a) -> bool:
        nonlocal first_audio
        if a is None or not getattr(a, "size", 0):
            return True
        try:
            stream.write(a.astype(np.float32, copy=False))
        except Exception:
            return False
        if first_audio < 0:
            first_audio = time.perf_counter() - t0
        return True

    try:
        f = _next_filler()
        if f is not None and not _STOP.is_set():
            if _write(f):
                fillers += 1

        UNDERRUN_FILLER_CAP = 1
        underrun_fillers = 0
        while not _STOP.is_set():
            blocking = underrun_fillers >= UNDERRUN_FILLER_CAP
            try:
                item = wav_q.get(timeout=None if blocking else 0.15)
            except queue.Empty:
                if _STOP.is_set():
                    break
                f = _next_filler()
                if f is not None:
                    if not _write(f):
                        break
                    fillers += 1
                    underrun_fillers += 1
                continue
            if item is None:
                break
            if not _write(item):
                break
            underrun_fillers = 0

        if not _STOP.is_set():
            try:
                stream.write(np.zeros(int(SAMPLE_RATE * 0.35), dtype=np.float32))
            except Exception:
                pass
        else:
            try:
                while True:
                    if wav_q.get_nowait() is None:
                        break
            except queue.Empty:
                pass
    finally:
        total_s = time.perf_counter() - t0
        print(f"[styletts] reply in {total_s:.2f}s first-audio {first_audio:.2f}s "
              f"fillers={fillers} chunks={len(chunks)}", flush=True)
        set_state("idle")

    return {"chars": len(text),
            "first_audio_s": round(first_audio, 2) if first_audio > 0 else None,
            "total_s": round(total_s, 1), "fillers": fillers}


def speak_plain(text: str) -> dict:
    """Synthesize the whole text at once (no chunking). Fallback path."""
    _STOP.clear()
    t0 = time.perf_counter()
    set_state("speaking")
    try:
        arr = _synth(text, _ref_s)
        arr = np.clip(arr.astype(np.float32, copy=False), -1.0, 1.0)
        synth_s = time.perf_counter() - t0
        _play(arr)
        total_s = time.perf_counter() - t0
        return {"chars": len(text), "synth_s": round(synth_s, 2),
                "total_s": round(total_s, 1), "first_audio_s": None}
    except Exception as e:
        raise
    finally:
        set_state("idle")


# ── HTTP handler ──────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, code: int, msg: str) -> None:
        b = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200 if _READY else 503, "ok" if _READY else "loading")
        elif self.path == "/stop":
            # Barge-in: does NOT take _SPEAK_LOCK (that lock is held by the in-flight
            # POST we're trying to cut off). _request_stop() sets the flag + aborts
            # the output stream from this separate request thread.
            _request_stop()
            self._send(200, "stopping")
        elif self.path == "/warm":
            # voice_call_start: ensure the model is loaded. If already _READY, no-op.
            # If it was /cold-ed (or not yet loaded), kick a BACKGROUND reload and report
            # "warming" — the caller polls /health until it returns 200.
            if _READY:
                self._send(200, "warm")
            else:
                _rewarm_async()
                self._send(503, "warming")
        elif self.path == "/cold":
            # voice_call_end: unload the model + free VRAM. Keeps the HTTP server
            # listening so the next voice_call_start can re-warm without a restart.
            _cold_unload()
            self._send(200, "cold")
        else:
            self._send(404, "not found")

    def do_POST(self) -> None:
        if not _READY:
            self._send(503, "loading")
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        plain_mode = False
        if raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = (obj.get("text") or "").strip()
                plain_mode = bool(obj.get("plain"))
            except Exception:
                text = raw.strip()
        else:
            text = raw.strip()
        if not text:
            self._send(400, "no text")
            return

        with _SPEAK_LOCK:
            try:
                try:
                    info = speak_plain(text) if plain_mode else speak_progressive(text)
                except Exception as e:
                    print(f"[styletts] progressive failed ({e!r}); plain fallback",
                          flush=True)
                    info = speak_plain(text)
                fa = info.get("first_audio_s")
                self._send(200,
                           f"[styletts2] spoke {info.get('chars', len(text))} chars "
                           f"in {info.get('total_s')}s (first-audio {fa}s, "
                           f"speed {SPEED:.2f})")
            except Exception as e:
                self._send(500, f"error: {e!r}")


# ── Warmup ────────────────────────────────────────────────────────────────────────
def _warmup() -> None:
    global _ref_s, _READY
    print(f"[styletts] computing style from {REF_WAV} ...", flush=True)
    _ref_s = _compute_style(str(REF_WAV))
    print("[styletts] style embedding cached", flush=True)

    print("[styletts] warmup synth (first inference primes CUDA kernels) ...", flush=True)
    t0 = time.perf_counter()
    try:
        arr = _synth(
            "Warming up the voice path so the first real reply doesn't sound cold.",
            _ref_s,
        )
        dur = len(arr) / SAMPLE_RATE
        print(f"[styletts] warmup synth done in {time.perf_counter()-t0:.1f}s "
              f"({dur:.2f}s audio)", flush=True)
    except Exception as e:
        print(f"[styletts] warmup synth FAILED (non-fatal): {e!r}", flush=True)

    print("[styletts] priming filler clips ...", flush=True)
    _prime_fillers()

    print("[styletts] pre-opening persistent output stream ...", flush=True)
    try:
        dev_idx = _resolve_output_device()
        _get_output_stream(dev_idx)
        print(f"[styletts] output stream open on device {dev_idx}", flush=True)
    except Exception as e:
        print(f"[styletts] could not pre-open output stream (non-fatal): {e!r}",
              flush=True)

    _READY = True
    print(f"[styletts] ready — POST text to http://{HOST}:{PORT}/  "
          f"(speed={SPEED:.2f}, alpha={ALPHA}, beta={BETA}, "
          f"steps={DIFFUSION_STEPS}, sr={SAMPLE_RATE})", flush=True)


# ── Entry point ───────────────────────────────────────────────────────────────────
# ── Singleton server: bind the port FIRST (before the slow model load) ───────────
# 2026-06-26 (Iris): the mouth used to _load_model() + _warmup() (~40s, but minutes
# under GPU contention) BEFORE binding the port. During that window /health returned
# 503, the watchdog read "mouth down" and relaunched — but since no instance held the
# port until warm, several loaded at once, fought over the GPU (slower still), and the
# losers of the port race never exited → a pile of zombie mouths. Binding FIRST makes
# the port a real singleton lock + a liveness signal the watchdog can see, while the
# model loads in the background and /health honestly reports 503 until warm.
# allow_reuse_address = False is load-bearing on Windows: with SO_REUSEADDR a second
# bind to the same port SUCCEEDS (Windows lets it steal the socket), defeating the
# singleton — so we leave it off and a duplicate launch raises WSAEADDRINUSE and exits.
class _SingletonHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def main() -> int:
    if not REF_WAV.is_file():
        print(f"[styletts] ERROR: reference voice not found: {REF_WAV}")
        return 1
    # Bind FIRST — claims the port (singleton) and gives the watchdog a liveness
    # signal, while /health stays 503 "loading" until the background warm completes.
    try:
        server = _SingletonHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"[styletts] port {PORT} already held ({e}); another mouth is alive — "
              f"exiting cleanly (singleton, no duplicate)", flush=True)
        return 0
    print(f"[styletts] bound :{PORT}; loading model in background "
          f"(/health=503 until warm)", flush=True)

    def _bg_load() -> None:
        try:
            _load_model()
            _warmup()   # sets _READY = True once the model is warm
        except Exception as e:
            print(f"[styletts] background model load FAILED: {e!r}", flush=True)

    threading.Thread(target=_bg_load, daemon=True, name="styletts-bg-load").start()

    print(f"[styletts] serving on http://{HOST}:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[styletts] shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
