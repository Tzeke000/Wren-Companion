from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .model_routing import discover_available_model_tags

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_TTL_SECONDS = 5 * 60


def _ollama_base() -> str:
    import os

    return (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")


def _pick_llava_model() -> str | None:
    try:
        tags, _src = discover_available_model_tags(force=False)
        tagset = {str(t).strip() for t in (tags or []) if str(t).strip()}
        if "llava:13b" in tagset:
            return "llava:13b"
        if "llava" in tagset:
            return "llava"
    except Exception:
        return None
    return None


def _call_llava(image_path: Path, prompt: str, model: str) -> str:
    raw = image_path.read_bytes()
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(raw).decode("ascii")],
        "stream": False,
    }
    req = urllib.request.Request(
        f"{_ollama_base()}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode("utf-8", errors="replace"))
    return str((out or {}).get("response") or "").strip()


def _cache_key(image_path: Path, prompt: str) -> str:
    try:
        st = image_path.stat()
        return f"{str(image_path)}::{int(st.st_mtime)}::{prompt[:120]}"
    except Exception:
        return f"{str(image_path)}::{prompt[:120]}"


def _cached_or_none(key: str) -> str | None:
    with _LOCK:
        row = _CACHE.get(key)
        if not row:
            return None
        if (time.time() - float(row.get("ts") or 0.0)) > _CACHE_TTL_SECONDS:
            _CACHE.pop(key, None)
            return None
        return str(row.get("text") or "").strip() or None


def _store_cache(key: str, text: str) -> None:
    with _LOCK:
        _CACHE[key] = {"ts": time.time(), "text": text}
        if len(_CACHE) > 64:
            oldest = sorted(_CACHE.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0.0))[:16]
            for k, _v in oldest:
                _CACHE.pop(k, None)


def describe_scene(frame_path: str, context: str = "", fallback_scene: str = "") -> str:
    path = Path(frame_path)
    if not path.is_file():
        return str(fallback_scene or "").strip()
    model = _pick_llava_model()
    if not model:
        return str(fallback_scene or "").strip()
    prompt = (
        "Describe what you see naturally and briefly. Focus on: who is present, what they are doing, "
        "their apparent mood/energy, anything notable. 2-3 sentences as if describing to a friend."
    )
    if context.strip():
        prompt += f"\nContext: {context[:220]}"
    key = _cache_key(path, prompt)
    cached = _cached_or_none(key)
    if cached is not None:
        return cached
    try:
        txt = _call_llava(path, prompt, model)
        if txt:
            _store_cache(key, txt)
            return txt
    except Exception:
        pass
    return str(fallback_scene or "").strip()


def describe_person(frame_path: str, person_name: str = "") -> str:
    path = Path(frame_path)
    if not path.is_file():
        return ""
    model = _pick_llava_model()
    if not model:
        return ""
    prompt = (
        "Describe the person in this image. "
        "Note their apparent mood, energy level, and anything that stands out. 1-2 sentences."
    )
    if person_name.strip():
        prompt += f"\nPerson name hint: {person_name[:80]}"
    key = _cache_key(path, prompt)
    cached = _cached_or_none(key)
    if cached is not None:
        return cached
    try:
        txt = _call_llava(path, prompt, model)
        if txt:
            _store_cache(key, txt)
            return txt
    except Exception:
        return ""
    return ""


def run_scene_refresh_async(g: dict[str, Any], frame_path: str, context: str = "", fallback_scene: str = "") -> None:
    def _worker() -> None:
        try:
            txt = describe_scene(frame_path, context=context, fallback_scene=fallback_scene)
            if txt:
                g["_llava_scene_description"] = txt[:600]
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True, name="ava-llava-scene")
    t.start()
