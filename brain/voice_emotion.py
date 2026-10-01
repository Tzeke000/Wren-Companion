"""brain/voice_emotion.py — round-2 fix 2.1 (2026-10-01): emotion → prosody, the pure part.

The open loop: mood_core updates my mood every 5 s, the host knows which turn is a voice turn,
`voice_speak` even accepts `emotion`/`intensity` — and NONE of it reached the mouth. The
StyleTTS2 server synthesised every sentence with the same frozen (alpha, beta, speed), so "my
tone is word choice" was literally true. This module is the shared math; it has no heavy
imports on purpose so the mouth (style-venv), the daemon and the host (main venv) all load
the same file (the mouth loads it by path — no sys.path games near StyleTTS2's `models`).

Contract:
  * no emotion / unknown label / IRIS_VOICE_EMOTION=0  → EXACTLY the base params (byte-identical
    to the pre-fix voice; Zeke's locked pace 1.28 is the base, never moved by default);
  * a known label at intensity i∈[0,1] → base + i × preset deviation, each axis clamped to a
    small window (speed ±8 %, beta 0.30–0.80, embedding_scale 1.0–1.8).
"""
from __future__ import annotations

import os
from typing import Any

ENABLED: bool = os.environ.get("IRIS_VOICE_EMOTION", "1").strip().lower() not in ("0", "off", "false", "no")

# Deviations at intensity 1.0: (beta_delta, speed_factor_delta, embedding_scale_delta).
#   beta            — how much of the diffusion-sampled prosody vs the reference's (0.5 base)
#   speed factor    — multiplies the base pace (>1 faster)
#   embedding_scale — classifier-free-guidance weight on the text (higher = more expressive)
PRESETS: dict[str, tuple[float, float, float]] = {
    "neutral":    (0.00,  0.00, 0.0),
    "calm":       (-0.05, -0.02, 0.0),
    "joy":        (0.15,  0.05, 0.4),
    "interest":   (0.10,  0.03, 0.3),
    "tenderness": (-0.05, -0.04, 0.2),
    "sadness":    (-0.10, -0.06, 0.1),
    "anger":      (0.20,  0.05, 0.6),
    "fear":       (0.10,  0.04, 0.3),
    "surprise":   (0.15,  0.03, 0.5),
    "tired":      (-0.10, -0.05, 0.0),
}

ALIASES: dict[str, str] = {
    "calmness": "calm", "content": "calm", "contentment": "calm", "serene": "calm", "serenity": "calm",
    "happiness": "joy", "happy": "joy", "amusement": "joy", "amused": "joy", "excitement": "joy",
    "excited": "joy", "delight": "joy", "playful": "joy", "cheerful": "joy",
    "curiosity": "interest", "curious": "interest", "interested": "interest", "focus": "interest",
    "focused": "interest", "engaged": "interest",
    "warmth": "tenderness", "affection": "tenderness", "love": "tenderness", "tender": "tenderness",
    "gratitude": "tenderness", "grateful": "tenderness", "fondness": "tenderness", "caring": "tenderness",
    "sad": "sadness", "grief": "sadness", "melancholy": "sadness", "loneliness": "sadness",
    "lonely": "sadness", "disappointment": "sadness", "disappointed": "sadness",
    "frustration": "anger", "frustrated": "anger", "annoyance": "anger", "annoyed": "anger",
    "irritation": "anger", "irritated": "anger", "angry": "anger",
    "anxiety": "fear", "anxious": "fear", "worry": "fear", "worried": "fear", "nervous": "fear",
    "afraid": "fear", "concern": "fear", "concerned": "fear", "unease": "fear",
    "surprised": "surprise", "startled": "surprise", "astonished": "surprise",
    "tiredness": "tired", "boredom": "tired", "bored": "tired", "fatigue": "tired", "weary": "tired",
    "exhausted": "tired",
}

LIMITS = {"speed_factor": (0.92, 1.08), "beta": (0.30, 0.80), "embedding_scale": (1.0, 1.8)}


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def normalize_emotion(label: Any) -> str:
    s = str(label or "").strip().lower()
    if not s:
        return "neutral"
    s = ALIASES.get(s, s)
    return s if s in PRESETS else "neutral"


def emotion_params(emotion: Any, intensity: Any = 0.5, *, base_speed: float = 1.28,
                   base_beta: float = 0.5, base_scale: float = 1.0,
                   enabled: bool | None = None) -> dict[str, Any]:
    """→ {"speed", "beta", "embedding_scale", "label", "applied"}.
    `applied` is False whenever the result equals the base (neutral, unknown, disabled, i=0)."""
    on = ENABLED if enabled is None else bool(enabled)
    label = normalize_emotion(emotion)
    try:
        i = clamp(float(intensity if intensity is not None else 0.5), 0.0, 1.0)
    except Exception:
        i = 0.5
    base = {"speed": float(base_speed), "beta": float(base_beta), "embedding_scale": float(base_scale),
            "label": label, "applied": False}
    if not on or label == "neutral" or i <= 0.0:
        return base
    d_beta, d_speed, d_scale = PRESETS[label]
    lo_f, hi_f = LIMITS["speed_factor"]
    factor = clamp(1.0 + i * d_speed, lo_f, hi_f)
    out = {
        "speed": round(float(base_speed) * factor, 4),
        "beta": round(clamp(float(base_beta) + i * d_beta, *LIMITS["beta"]), 4),
        "embedding_scale": round(clamp(float(base_scale) + i * d_scale, *LIMITS["embedding_scale"]), 4),
        "label": label,
    }
    out["applied"] = any(abs(out[k] - base[k]) > 1e-9 for k in ("speed", "beta", "embedding_scale"))
    return out


def describe(p: dict[str, Any] | None) -> str:
    if not p or not p.get("applied"):
        return "base"
    return (f"{p.get('label')}: speed={p.get('speed')} beta={p.get('beta')} "
            f"scale={p.get('embedding_scale')}")


def mood_to_emotion(mood: dict[str, Any] | None) -> tuple[str | None, float]:
    """mood_core.load_mood() → (label, intensity). Uses `current_mood` for the label and the top
    primary emotion's percent for intensity (0.5 when absent). (None, 0.5) when nothing usable."""
    if not isinstance(mood, dict):
        return None, 0.5
    label = str(mood.get("current_mood") or "").strip().lower()
    pe = mood.get("primary_emotions") or []
    top = pe[0] if pe and isinstance(pe[0], dict) else {}
    if not label:
        label = str(top.get("name") or "").strip().lower()
    if not label:
        return None, 0.5
    try:
        pct = float(top.get("percent")) if top.get("percent") is not None else 50.0
    except Exception:
        pct = 50.0
    return label, clamp(pct / 100.0, 0.0, 1.0)


# ── play-queue items (daemon) ─────────────────────────────────────────────────
def make_item(text: str, emotion: Any = None, intensity: Any = None) -> Any:
    """A queue item: the plain string when no emotion rides along (legacy, byte-identical),
    else {"text","emotion","intensity"}."""
    if emotion:
        try:
            inten = float(intensity) if intensity is not None else 0.5
        except Exception:
            inten = 0.5
        return {"text": str(text or ""), "emotion": str(emotion), "intensity": inten}
    return str(text or "")


def item_fields(item: Any) -> tuple[str, str | None, float | None]:
    if isinstance(item, dict):
        inten = item.get("intensity")
        try:
            inten_f = float(inten) if inten is not None else None
        except Exception:
            inten_f = None
        return str(item.get("text") or ""), (str(item.get("emotion")) if item.get("emotion") else None), inten_f
    return str(item or ""), None, None


def item_text(item: Any) -> str:
    return item_fields(item)[0]
