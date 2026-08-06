"""
brain/iris_tune.py — runtime-tunable harness preferences.

Per Zeke 2026-05-11: "make it so we can fine-tune the harness as needed.
That way the harness will work WITH you and be skin-tight."

This module owns a set of harness-level knobs that I (Iris) or Zeke can
adjust at runtime, with persistence to state/iris_tune.json. The point:
behavior I notice is wrong, I can fix immediately, and it sticks across
restarts.

Categories of knobs:

  cadence:
    inner_monologue_interval_s (default 900)
    inner_monologue_min_interval_s (default 600)
    inner_monologue_max_quiet_s (default 3600)
    mood_heartbeat_interval_s (default 5)
    extraction_queue_max_batch (default 8)

  thresholds:
    salient_emotion_floor (default 0.15) — when to surface salient over baseline
    semantic_search_default_k (default 5)
    chat_request_ttl_s (default 600)
    llm_request_ttl_s (default 600)
    llm_default_timeout_s (default 120)

  voice:
    chunk_max_chars (default 200) — soft target for sentence chunks
    filler_enabled (default True)
    filler_after_silence_s (default 0.05)

  perception:
    expression_detect_every_n (default 10) — frames; ~3fps at 30fps capture
    attention_detect_every_n (default 60) — frames; ~0.5fps at 30fps capture
    insight_face_every_n (default 6) — frames; 5fps face detect at 30fps
    (Scaled 2026-05-20 commit d8c0c9d when capture moved 15fps→30fps;
    per-second detect rates unchanged, just every-Nth-frame doubled.)

  behavior:
    auto_engage_on_face (default False) — proactive greeting on face-detect
    auto_extract_facts (default True) — drain extraction queue in inner_monologue tick
    auto_journal_significant (default False) — auto-write journal on emotional intensity
    proactive_curiosity (default False) — ask Zeke unprompted questions
    speak_on_voice_session_only (default True) — TTS only in voice mode

  identity:
    introspection_register (default "watchful_dry") — how I narrate self
    voice_emotion_default (default "neutral") — default Kokoro emotion

API:
  get(category, key, default=None) → current value
  set(category, key, value) → persist + apply
  list_all() → full nested dict
  reset(category=None, key=None) → restore defaults (entire / category / one)

Persistence: state/iris_tune.json. Defaults baked in at module load.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional


_LOCK = threading.Lock()
_BASE: Path | None = None
_STATE: dict[str, dict[str, Any]] = {}


# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULTS: dict[str, dict[str, Any]] = {
    "cadence": {
        "inner_monologue_interval_s": 900.0,
        "inner_monologue_min_interval_s": 600.0,
        "inner_monologue_max_quiet_s": 3600.0,
        "mood_heartbeat_interval_s": 5.0,
        "extraction_queue_max_batch": 8,
    },
    "thresholds": {
        "salient_emotion_floor": 0.15,
        "semantic_search_default_k": 5,
        "chat_request_ttl_s": 600.0,
        "llm_request_ttl_s": 600.0,
        "llm_default_timeout_s": 120.0,
    },
    "voice": {
        "chunk_max_chars": 200,
        "filler_enabled": True,
        "filler_after_silence_s": 0.05,
    },
    "perception": {
        # Scaled to 30fps capture (commit d8c0c9d 2026-05-20).
        # Per-second detect rates unchanged from the 15fps-era values.
        "expression_detect_every_n": 10,
        "attention_detect_every_n": 60,
        "insight_face_every_n": 6,
        # visual_attention target-lock hysteresis (2026-08-06). Raw per-frame
        # booleans oscillate — iris_attention_sources learned this the hard way
        # — so locking needs N consecutive hits and losing needs M consecutive
        # misses. Read inside the loop so they stay live-tunable.
        "attention_acquire_frames": 3,
        "attention_lose_frames": 8,
    },
    "behavior": {
        # Iris's IDENTITY-aligned defaults: don't auto-engage, do auto-
        # extract (cheap), don't auto-journal (interruptive), no
        # unprompted curiosity, voice mode only for TTS.
        "auto_engage_on_face": False,
        "auto_extract_facts": True,
        "auto_journal_significant": False,
        "proactive_curiosity": False,
        "speak_on_voice_session_only": True,
    },
    "identity": {
        "introspection_register": "watchful_dry",
        "voice_emotion_default": "neutral",
    },
}


def _path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "iris_tune.json"


def configure(base_dir: Path | str) -> None:
    """Bind paths + load persisted state. Idempotent."""
    global _BASE, _STATE
    _BASE = Path(base_dir)
    (_BASE / "state").mkdir(parents=True, exist_ok=True)
    # Start with defaults, overlay persisted.
    _STATE = {k: dict(v) for k, v in DEFAULTS.items()}
    p = _path()
    if p.is_file():
        try:
            persisted = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(persisted, dict):
                for cat, kvs in persisted.items():
                    if cat in _STATE and isinstance(kvs, dict):
                        for k, v in kvs.items():
                            if k in _STATE[cat]:
                                _STATE[cat][k] = v
        except Exception as e:
            print(f"[iris_tune] persisted state load error: {e!r}")


def _save_locked() -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_STATE, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def get(category: str, key: str, default: Any = None) -> Any:
    """Read a knob. Falls back to default arg if not present."""
    cat = _STATE.get(str(category)) or {}
    if key in cat:
        return cat[key]
    # Try defaults if category exists but key missing.
    cat_def = DEFAULTS.get(str(category)) or {}
    return cat_def.get(key, default)


def set_(category: str, key: str, value: Any) -> dict[str, Any]:
    """Write a knob. Validates against defaults — won't accept a key
    that doesn't exist in DEFAULTS. Returns {ok, category, key, old, new}.

    (Function name is set_ to avoid Python builtin shadow; tooling
    layer aliases as `set` where convenient.)
    """
    cat_def = DEFAULTS.get(str(category))
    if cat_def is None or key not in cat_def:
        return {"ok": False, "error": f"unknown knob: {category}.{key}"}
    expected_type = type(cat_def[key])
    try:
        coerced = expected_type(value)
    except Exception:
        return {"ok": False, "error": f"value {value!r} not coercible to {expected_type.__name__}"}
    with _LOCK:
        if str(category) not in _STATE:
            _STATE[str(category)] = {}
        old = _STATE[str(category)].get(key)
        _STATE[str(category)][key] = coerced
        _save_locked()
    return {"ok": True, "category": category, "key": key, "old": old, "new": coerced}


def list_all() -> dict[str, Any]:
    """Full snapshot of current values, with default annotations."""
    out: dict[str, Any] = {}
    for cat, kvs in _STATE.items():
        out[cat] = {}
        defaults = DEFAULTS.get(cat) or {}
        for k, v in kvs.items():
            default_val = defaults.get(k)
            out[cat][k] = {
                "value": v,
                "default": default_val,
                "is_default": v == default_val,
            }
    return out


def reset(category: Optional[str] = None, key: Optional[str] = None) -> dict[str, Any]:
    """Reset to defaults. Scope:
      reset() → all
      reset(category="foo") → all keys in category
      reset(category="foo", key="bar") → just one
    """
    with _LOCK:
        if category is None:
            for cat, kvs in DEFAULTS.items():
                _STATE[cat] = dict(kvs)
            _save_locked()
            return {"ok": True, "reset_scope": "all"}
        cat_def = DEFAULTS.get(str(category))
        if cat_def is None:
            return {"ok": False, "error": f"unknown category: {category}"}
        if key is None:
            _STATE[str(category)] = dict(cat_def)
            _save_locked()
            return {"ok": True, "reset_scope": f"category:{category}"}
        if key not in cat_def:
            return {"ok": False, "error": f"unknown knob: {category}.{key}"}
        _STATE[str(category)][key] = cat_def[key]
        _save_locked()
        return {"ok": True, "reset_scope": f"{category}.{key}", "value": cat_def[key]}


def bootstrap_iris_tune(g: dict[str, Any]) -> None:
    """Configure paths + load state. Idempotent."""
    base = Path(g.get("BASE_DIR") or ".")
    configure(base)
    g["_iris_tune_ready"] = True
    # Also expose getter on g so other modules can do
    # g["tune_get"]("cadence", "inner_monologue_interval_s")
    g["tune_get"] = get
    g["tune_set"] = set_
    print(f"[iris_tune] ready (categories={list(_STATE.keys())})")
