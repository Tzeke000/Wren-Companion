# SELF_ASSESSMENT: I am the control surface for voice-id (speaker recognition) —
# enroll voiceprints from recent captured utterances, test identification, and
# toggle the daemon-side tagging (scratch/voice_id.json). The daemon tags each
# captured utterance via voice/wren_voiceid.py (WeSpeaker ResNet34-LM ONNX).
"""
voiceid — enroll/test/configure speaker recognition on the ear path.

Built 2026-07-13 (Zeke directive: "voice recognition def needs to be a thing"),
the day TikTok audio was captured and attributed to him.

Tools:
  voiceid_status  -> model/profiles/config/recent-utterance report
  voiceid_enroll  -> params {name: str, last_n: int = 3, files?: [paths]}
                     embeds the newest N wavs in state/voiceid/last_utts/
                     (or explicit files) into <name>'s profile
  voiceid_test    -> params {file?: path} identify the newest (or given) wav
  voiceid_config  -> params {enabled?: bool, threshold?: float,
                     min_seconds?: float, save_utts?: bool} -> scratch/voice_id.json

NOTE: this tool runs in the iris_runtime process — its model session is separate
from the daemon's. Both read the same profiles dir; the daemon re-reads profiles
on mtime change, so an enrollment here is visible to the ears on the next turn.
"""
from __future__ import annotations

import json
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np

from tools.tool_registry import register_tool

_REPO = Path(r"D:\Wren-Companion")
_VOICE_DIR = _REPO / "voice"
CONFIG_FILE = _REPO / "scratch" / "voice_id.json"

if str(_VOICE_DIR) not in sys.path:
    sys.path.insert(0, str(_VOICE_DIR))


def _mod():
    import wren_voiceid  # noqa: PLC0415
    return wren_voiceid


def _load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        nch, sw, sr = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if sw != 2:
        raise ValueError(f"{path.name}: expected 16-bit PCM, got sampwidth={sw}")
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        audio = audio.reshape(-1, nch).mean(axis=1)
    if sr != 16000:
        idx = np.clip((np.arange(int(len(audio) * 16000 / sr)) * sr / 16000).astype(int),
                      0, len(audio) - 1)
        audio = audio[idx]
    return audio


def _recent_utts(n: int = 20) -> list:
    d = _mod().UTT_DIR
    if not d.exists():
        return []
    return sorted(d.glob("utt_*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]


def _voiceid_status(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        m = _mod()
    except Exception as e:
        return {"ok": False, "error": f"wren_voiceid import failed: {e!r}"}
    cfg = None
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            cfg = f"UNPARSEABLE: {e!r}"
    profiles = {}
    if m.PROFILE_DIR.exists():
        for p in m.PROFILE_DIR.glob("*.npy"):
            try:
                arr = np.load(p)
                profiles[p.stem] = int(arr.shape[0]) if arr.ndim > 1 else 1
            except Exception:
                profiles[p.stem] = -1
    utts = [{"file": p.name, "kb": round(p.stat().st_size / 1024)} for p in _recent_utts(8)]
    return {"ok": True,
            "model_present": Path(m._MODEL_PATH).exists(),
            "model_warm_here": m.is_warm(), "warm_error": m.warm_error(),
            "profiles": profiles, "config": cfg,
            "recent_utterances": utts,
            "note": "daemon warms its own session lazily; tags appear once "
                    "config enabled AND >=1 profile enrolled"}


def _voiceid_enroll(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "").strip().lower()
    if not name:
        return {"ok": False, "error": "pass name (e.g. 'zeke')"}
    try:
        m = _mod()
    except Exception as e:
        return {"ok": False, "error": f"wren_voiceid import failed: {e!r}"}
    files = params.get("files")
    if files:
        paths = [Path(f) for f in files]
    else:
        paths = _recent_utts(int(params.get("last_n", 3)))
    if not paths:
        return {"ok": False, "error": "no utterance wavs found — enable save_utts in "
                                      "voiceid_config and speak a few turns first"}
    done, errors = [], []
    for p in paths:
        try:
            audio = _load_wav(p)
            if audio.size < 16000 * 0.8:
                errors.append(f"{p.name}: too short ({audio.size/16000:.2f}s)")
                continue
            count = m.enroll(name, audio)
            done.append({"file": p.name, "seconds": round(audio.size / 16000, 2),
                         "profile_count": count})
        except Exception as e:
            errors.append(f"{p.name}: {e!r}")
    return {"ok": bool(done), "enrolled": done, "errors": errors,
            "note": "daemon sees new profiles on its next turn (mtime cache)"}


def _voiceid_test(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        m = _mod()
    except Exception as e:
        return {"ok": False, "error": f"wren_voiceid import failed: {e!r}"}
    f = params.get("file")
    path = Path(f) if f else (_recent_utts(1)[0] if _recent_utts(1) else None)
    if path is None or not path.exists():
        return {"ok": False, "error": "no wav to test (none saved yet?)"}
    try:
        audio = _load_wav(path)
        if not m.warm_sync():
            return {"ok": False, "error": f"model warm failed: {m.warm_error()}"}
        r = m.identify(audio)
        return {"ok": True, "file": path.name,
                "seconds": round(audio.size / 16000, 2), **r}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def _voiceid_config(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    cur: dict = {}
    if CONFIG_FILE.exists():
        try:
            cur = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    changed = []
    for key, cast, lo, hi in (("enabled", bool, None, None),
                              ("threshold", float, 0.05, 0.95),
                              ("min_seconds", float, 0.3, 5.0),
                              ("save_utts", bool, None, None)):
        if key in params:
            try:
                v = cast(params[key])
            except (TypeError, ValueError) as e:
                return {"ok": False, "error": f"bad {key}: {e!r}"}
            if lo is not None and not lo <= v <= hi:
                return {"ok": False, "error": f"{key} {v} outside {lo}-{hi}"}
            cur[key] = v
            changed.append(f"{key}={v}")
    if not changed:
        return {"ok": True, "config": cur or {"absent": True}, "changed": []}
    cur.setdefault("enabled", False)
    cur.setdefault("threshold", 0.4)
    cur.setdefault("min_seconds", 0.8)
    cur.setdefault("save_utts", True)
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cur), encoding="utf-8")
        back = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))   # real-loader verify
    except Exception as e:
        return {"ok": False, "error": f"write/verify failed: {e!r}"}
    return {"ok": True, "changed": changed, "config": back,
            "note": "read per-listen by the daemon — hot, no reload needed"}


register_tool(
    "voiceid_status",
    "Voice-id (speaker recognition) status: model present, enrolled profiles, config, recent captured-utterance wavs.",
    1,
    _voiceid_status,
)
register_tool(
    "voiceid_enroll",
    "Enroll a voiceprint: params {name, last_n: int=3, files?: [wav paths]} — embeds recent captured utterances into state/voiceid/profiles/<name>.npy. Enroll ONLY utterances verified to be that speaker.",
    2,
    _voiceid_enroll,
)
register_tool(
    "voiceid_test",
    "Identify the speaker of the newest captured utterance wav (or params {file}). Returns best match + cosine scores.",
    1,
    _voiceid_test,
)
register_tool(
    "voiceid_config",
    "Configure daemon-side voice-id tagging (scratch/voice_id.json): params {enabled?, threshold? (0.4), min_seconds? (0.8), save_utts?}. Hot — read per-listen.",
    2,
    _voiceid_config,
)
