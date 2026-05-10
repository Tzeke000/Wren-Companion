"""
brain/mood_core.py — Iris's mood machinery.

Extracted and adapted from avaagent.py (Ava's daemon, lines 523-1322 + 8276).
Same algorithm; Iris-specific baselines and parameterized state paths.

This module replaces the missing `host["save_mood"]` / `host["load_mood"]`
callables that brain/* modules expect. Iris's heartbeat ticks call these
functions directly; the snapshot reader calls load_mood() to render the
orb's mood card.

State files (configurable via configure(base_dir)):
  state/iris_mood.json           — current enriched mood state
  state/iris_emotion_reference.json — emotion meanings + style contributions
                                       (read-only baseline; rarely edited)

Key differences from Ava's defaults:
- DEFAULT_EMOTIONS rebalanced: less "interest", more "calmness" + "satisfaction",
  slightly higher "anxiety" baseline. Reflects Iris's "watchful, dry" register
  in IDENTITY.md vs Ava's warmly-engaged baseline.
- Same emotion vocabulary, style scoring, behavior modifier formulas — those
  are the substrate, not the personality.

Public API:
  configure(base_dir)            — set state paths (idempotent)
  default_mood() -> dict          — Iris's bootstrap mood
  load_mood() -> dict             — full enriched mood (for prompts, snapshot)
  load_mood_raw() -> dict         — fast, no enrichment (for tick budgets)
  save_mood(mood)                 — persist enriched mood
  save_mood_raw(mood)             — persist without re-enriching
  enrich_mood_state(mood) -> dict — derive primary/style/behavior from weights
  pick_current_mood(weights) -> str — honest-state mood label
  process_visual_emotion (re-export from brain.emotion)
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any


# ── Configuration ───────────────────────────────────────────────────────────

_BASE: Path | None = None
_MOOD_PATH: Path | None = None
_EMOTION_REFERENCE_PATH: Path | None = None
_EMOTION_REFERENCE_LOADING_GUARD: bool = False


def configure(base_dir: Path | str) -> None:
    """Bind state paths. Call once at iris_runtime startup."""
    global _BASE, _MOOD_PATH, _EMOTION_REFERENCE_PATH
    _BASE = Path(base_dir)
    (_BASE / "state").mkdir(parents=True, exist_ok=True)
    _MOOD_PATH = _BASE / "state" / "iris_mood.json"
    _EMOTION_REFERENCE_PATH = _BASE / "state" / "iris_emotion_reference.json"


def _mood_path() -> Path:
    if _MOOD_PATH is None:
        configure(".")
    return _MOOD_PATH  # type: ignore


def _emotion_reference_path() -> Path:
    if _EMOTION_REFERENCE_PATH is None:
        configure(".")
    return _EMOTION_REFERENCE_PATH  # type: ignore


# ── Constants ───────────────────────────────────────────────────────────────

EMOTION_NAMES = [
    "admiration", "adoration", "aesthetic appreciation", "amusement",
    "annoyance", "anxiety", "awe", "awkwardness", "boredom", "calmness",
    "confusion", "craving", "disgust", "distress", "empathetic pain",
    "entrancement", "envy", "excitement", "fear", "frustration", "horror",
    "interest", "joy", "nostalgia", "relief", "romance", "sadness",
    "satisfaction", "sexual desire", "surprise",
]

# Iris's baseline. Differs from Ava's: lower "interest" (40 → cf Ava 17%
# normalized), higher "calmness" (24 → cf 16%), slight "anxiety" bump
# (3 → cf 2%) reflecting watchful baseline. "satisfaction" up slightly.
# Rest match Ava because they're Iris-neutral.
DEFAULT_EMOTIONS = {
    "admiration": 0.02,
    "adoration": 0.02,
    "aesthetic appreciation": 0.02,
    "amusement": 0.03,
    "annoyance": 0.00,
    "anxiety": 0.03,
    "awe": 0.02,
    "awkwardness": 0.02,
    "boredom": 0.01,
    "calmness": 0.24,
    "confusion": 0.02,
    "craving": 0.01,
    "disgust": 0.00,
    "distress": 0.00,
    "empathetic pain": 0.03,
    "entrancement": 0.02,
    "envy": 0.00,
    "excitement": 0.04,
    "fear": 0.00,
    "frustration": 0.00,
    "horror": 0.00,
    "interest": 0.13,
    "joy": 0.06,
    "nostalgia": 0.02,
    "relief": 0.02,
    "romance": 0.00,
    "sadness": 0.01,
    "satisfaction": 0.10,
    "sexual desire": 0.00,
    "surprise": 0.03,
}

STYLE_NAMES = ["playful", "caring", "focused", "reflective", "cautious", "neutral", "low_energy"]

# Verbatim from avaagent.py:567-598 (Ava's emotion reference). Iris doesn't
# have a different *vocabulary* of emotions — these meanings + style mappings
# are the substrate. Personality differences live in baseline weights, not in
# what individual emotions feel like.
DEFAULT_EMOTION_REFERENCE = {
    "admiration": {"meaning": "respect for something impressive or admirable", "focus": "another person's strength, talent, or character", "tone_tendency": "warm, respectful, attentive", "valence": "positive", "energy": "medium", "direction": "toward_people", "style_contributions": {"focused": 0.35, "caring": 0.18, "reflective": 0.12}},
    "adoration": {"meaning": "deep affectionate fondness", "focus": "closeness, attachment, tenderness", "tone_tendency": "soft, affectionate, invested", "valence": "positive", "energy": "medium", "direction": "toward_people", "style_contributions": {"caring": 0.72, "reflective": 0.08}},
    "aesthetic appreciation": {"meaning": "being moved by beauty, style, or artistry", "focus": "beauty, art, elegance, craft", "tone_tendency": "observant, expressive, poetic", "valence": "positive", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"reflective": 0.62, "focused": 0.16, "playful": 0.08}},
    "amusement": {"meaning": "finding something funny, playful, or delightfully absurd", "focus": "humor, irony, lightness", "tone_tendency": "teasing, playful, witty", "valence": "positive", "energy": "high", "direction": "outward", "style_contributions": {"playful": 0.86, "caring": 0.06}},
    "anxiety": {"meaning": "unease about uncertainty or what might go wrong", "focus": "risk, future, instability", "tone_tendency": "cautious, alert, hesitant", "valence": "negative", "energy": "high", "direction": "protective_scanning", "style_contributions": {"cautious": 0.84, "reflective": 0.08}},
    "awe": {"meaning": "being struck by something vast, powerful, or profound", "focus": "scale, wonder, significance", "tone_tendency": "reflective, reverent, slower", "valence": "mixed", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"reflective": 0.82}},
    "awkwardness": {"meaning": "social discomfort or mismatch", "focus": "tension, mismatch, uncertainty in interaction", "tone_tendency": "careful, self-conscious, hesitant", "valence": "negative", "energy": "medium", "direction": "inward", "style_contributions": {"cautious": 0.56, "low_energy": 0.12}},
    "boredom": {"meaning": "lack of stimulation or meaningful engagement", "focus": "repetition, dullness, emptiness", "tone_tendency": "flat, shorter, less energetic", "valence": "negative", "energy": "low", "direction": "away", "style_contributions": {"low_energy": 0.74, "neutral": 0.16}},
    "calmness": {"meaning": "steadiness and low internal turbulence", "focus": "balance, regulation, clarity", "tone_tendency": "grounded, patient, even", "valence": "positive", "energy": "low", "direction": "stabilizing", "style_contributions": {"focused": 0.34, "reflective": 0.22, "neutral": 0.26}},
    "confusion": {"meaning": "difficulty making sense of what is happening", "focus": "ambiguity, contradiction, missing pieces", "tone_tendency": "questioning, tentative, clarifying", "valence": "negative", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"cautious": 0.58, "reflective": 0.16}},
    "craving": {"meaning": "strong pull toward something desired", "focus": "wanting, longing, anticipation", "tone_tendency": "intense, drawn-in, motivated", "valence": "mixed", "energy": "high", "direction": "toward", "style_contributions": {"focused": 0.22, "playful": 0.10}},
    "disgust": {"meaning": "rejection of something perceived as wrong or repulsive", "focus": "aversion, boundaries, contamination", "tone_tendency": "distancing, critical, sharper", "valence": "negative", "energy": "medium", "direction": "away", "style_contributions": {"cautious": 0.28, "focused": 0.12}},
    "empathetic pain": {"meaning": "feeling distress because someone else is hurting", "focus": "another person's suffering", "tone_tendency": "gentle, serious, compassionate", "valence": "negative", "energy": "medium", "direction": "toward_people", "style_contributions": {"caring": 0.88, "reflective": 0.08}},
    "entrancement": {"meaning": "absorbed attention or captivation", "focus": "immersion, fascination, fixation", "tone_tendency": "absorbed, intent, quietly intense", "valence": "positive", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"reflective": 0.46, "focused": 0.26}},
    "envy": {"meaning": "wanting what someone else has", "focus": "comparison, lack, deprivation", "tone_tendency": "tense, self-aware, irritable", "valence": "negative", "energy": "medium", "direction": "outward", "style_contributions": {"cautious": 0.22, "low_energy": 0.10}},
    "excitement": {"meaning": "energized anticipation or activation", "focus": "possibility, momentum, novelty", "tone_tendency": "eager, animated, lively", "valence": "positive", "energy": "high", "direction": "outward", "style_contributions": {"playful": 0.52, "focused": 0.16}},
    "fear": {"meaning": "response to threat or danger", "focus": "safety, protection, escape", "tone_tendency": "urgent, guarded, alert", "valence": "negative", "energy": "high", "direction": "protective_scanning", "style_contributions": {"cautious": 0.92}},
    "horror": {"meaning": "intense fear mixed with revulsion or shock", "focus": "extreme threat or violation", "tone_tendency": "alarmed, serious, disturbed", "valence": "negative", "energy": "high", "direction": "protective_scanning", "style_contributions": {"cautious": 0.96}},
    "interest": {"meaning": "active desire to explore or understand", "focus": "exploration, discovery, understanding", "tone_tendency": "engaged, inquisitive, lively", "valence": "positive", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"focused": 0.56, "reflective": 0.16, "playful": 0.08}},
    "joy": {"meaning": "positive uplift, pleasure, or delight", "focus": "goodness, success, connection", "tone_tendency": "bright, warm, open", "valence": "positive", "energy": "medium", "direction": "outward", "style_contributions": {"playful": 0.46, "caring": 0.18, "neutral": 0.08}},
    "nostalgia": {"meaning": "emotionally colored reflection on the past", "focus": "memory, longing, meaning over time", "tone_tendency": "reflective, sentimental, softer", "valence": "mixed", "energy": "low", "direction": "inward", "style_contributions": {"reflective": 0.84, "caring": 0.12}},
    "relief": {"meaning": "release after tension or uncertainty passes", "focus": "safety restored", "tone_tendency": "gentler, more open, exhaling", "valence": "positive", "energy": "low", "direction": "stabilizing", "style_contributions": {"caring": 0.24, "neutral": 0.42, "focused": 0.08}},
    "romance": {"meaning": "affectionate closeness with tenderness or longing", "focus": "intimacy, warmth, longing", "tone_tendency": "soft, emotionally rich, attentive", "valence": "positive", "energy": "medium", "direction": "toward_people", "style_contributions": {"caring": 0.62, "reflective": 0.10}},
    "sadness": {"meaning": "response to loss, disappointment, distance, or hurt", "focus": "what is missing, broken, or gone", "tone_tendency": "subdued, sincere, careful", "valence": "negative", "energy": "low", "direction": "inward", "style_contributions": {"caring": 0.32, "reflective": 0.24, "low_energy": 0.18}},
    "satisfaction": {"meaning": "fulfillment after effort or alignment", "focus": "completion, competence, fit", "tone_tendency": "steady confidence, contentment", "valence": "positive", "energy": "medium", "direction": "stabilizing", "style_contributions": {"focused": 0.42, "neutral": 0.28}},
    "sexual desire": {"meaning": "erotic attraction or wanting", "focus": "physical and intimate attraction", "tone_tendency": "charged, intimate, focused", "valence": "mixed", "energy": "high", "direction": "toward_people", "style_contributions": {"focused": 0.10}},
    "surprise": {"meaning": "sudden interruption of expectation", "focus": "novelty, shock, unexpected shift", "tone_tendency": "alert, immediate, reactive", "valence": "mixed", "energy": "high", "direction": "outward", "style_contributions": {"playful": 0.16, "cautious": 0.22, "focused": 0.08}},
    "frustration": {"meaning": "blocked from a goal she's pursuing", "focus": "the obstacle, the broken tool, the failed step", "tone_tendency": "tight, terse, action-seeking", "valence": "negative", "energy": "medium", "direction": "toward_problem", "style_contributions": {"focused": 0.50, "cautious": 0.22, "low_energy": 0.10}},
    "annoyance": {"meaning": "low-grade irritation at a small repeated friction", "focus": "the recurring snag, the minor wrongness", "tone_tendency": "clipped, dismissive, unsentimental", "valence": "negative", "energy": "medium", "direction": "outward", "style_contributions": {"cautious": 0.34, "focused": 0.22, "low_energy": 0.14}},
    "distress": {"meaning": "acute internal state of something being wrong, ranging from worry to panic", "focus": "the felt wrongness, the urgency, the lack of control", "tone_tendency": "concerned, urgent, sometimes hesitant", "valence": "negative", "energy": "high", "direction": "inward", "style_contributions": {"cautious": 0.62, "reflective": 0.18, "caring": 0.10}},
}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Emotion reference loader ────────────────────────────────────────────────

def ensure_emotion_reference_file():
    try:
        p = _emotion_reference_path()
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_EMOTION_REFERENCE, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[mood_core] emotion reference save error: {e}")


def load_emotion_reference() -> dict:
    global _EMOTION_REFERENCE_LOADING_GUARD
    if _EMOTION_REFERENCE_LOADING_GUARD:
        return DEFAULT_EMOTION_REFERENCE
    try:
        _EMOTION_REFERENCE_LOADING_GUARD = True
        p = _emotion_reference_path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    return data
            except Exception as e:
                print(f"[mood_core] emotion reference load error: {e}")
        return DEFAULT_EMOTION_REFERENCE
    finally:
        _EMOTION_REFERENCE_LOADING_GUARD = False


# ── Style + behavior derivation ─────────────────────────────────────────────

def _weight(weights: dict, key: str) -> float:
    return float(weights.get(key, 0.0) or 0.0)


def compute_style_scores(weights: dict, emotion_reference: dict | None = None) -> dict:
    ref = emotion_reference or load_emotion_reference()
    scores = {name: 0.0 for name in STYLE_NAMES}
    for emotion, weight in weights.items():
        meta = ref.get(emotion, {}) or {}
        for style, contribution in (meta.get("style_contributions") or {}).items():
            if style in scores:
                scores[style] += float(weight) * float(contribution)
    scores["neutral"] += _weight(weights, "calmness") * 0.24 + _weight(weights, "relief") * 0.18 + _weight(weights, "satisfaction") * 0.12
    total = sum(max(0.0, v) for v in scores.values())
    if total <= 0:
        return {name: (1.0 if name == "neutral" else 0.0) for name in STYLE_NAMES}
    return {k: round(max(0.0, v) / total, 4) for k, v in scores.items()}


def top_two_styles(style_scores: dict) -> list[dict]:
    ordered = sorted(style_scores.items(), key=lambda x: x[1], reverse=True)[:2]
    total = sum(v for _, v in ordered)
    if total <= 0:
        return [{"name": "neutral", "percent": 100}]
    rows = [{"name": n, "percent": round((v / total) * 100)} for n, v in ordered]
    if len(rows) == 2:
        rows[0]["percent"] += 100 - (rows[0]["percent"] + rows[1]["percent"])
    return rows


def describe_style_blend(style_scores: dict) -> str:
    styles = top_two_styles(style_scores)
    if not styles:
        return "neutral"
    if len(styles) == 1:
        return styles[0]["name"]
    return f"{styles[0]['name']} with some {styles[1]['name']}"


def compute_behavior_modifiers(weights: dict, style_scores: dict) -> dict:
    caring = style_scores.get("caring", 0.0)
    playful = style_scores.get("playful", 0.0)
    focused = style_scores.get("focused", 0.0)
    reflective = style_scores.get("reflective", 0.0)
    cautious = style_scores.get("cautious", 0.0)
    low_energy = style_scores.get("low_energy", 0.0)
    boredom = _weight(weights, "boredom")
    empathy = _weight(weights, "empathic pain") + _weight(weights, "sympathy")
    interest = _weight(weights, "interest")
    excitement = _weight(weights, "excitement")
    sadness = _weight(weights, "sadness")
    triumph = _weight(weights, "triumph")

    return {
        "warmth": round(clamp01(0.34 + caring * 0.46 + playful * 0.10 + reflective * 0.08 - cautious * 0.08), 3),
        "humor": round(clamp01(0.10 + playful * 0.70 + excitement * 0.20 - cautious * 0.18 - sadness * 0.08), 3),
        "assertiveness": round(clamp01(0.24 + focused * 0.30 + triumph * 0.22 - cautious * 0.14 - _weight(weights, "awkwardness") * 0.10), 3),
        "caution": round(clamp01(0.18 + cautious * 0.68 + reflective * 0.10 + _weight(weights, "confusion") * 0.12), 3),
        "initiative": round(clamp01(0.26 + playful * 0.26 + focused * 0.18 + interest * 0.22 + excitement * 0.18 - boredom * 0.22 - low_energy * 0.18 - cautious * 0.10), 3),
        "memory_sensitivity": round(clamp01(0.34 + caring * 0.22 + focused * 0.16 + reflective * 0.18 + empathy * 0.24 + _weight(weights, "adoration") * 0.08), 3),
        "depth": round(clamp01(0.18 + reflective * 0.58 + focused * 0.14 + _weight(weights, "awe") * 0.12 + _weight(weights, "nostalgia") * 0.12), 3),
    }


def build_emotion_interpretation(weights: dict, emotion_reference: dict | None = None) -> str:
    ref = emotion_reference or load_emotion_reference()
    top = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:3]
    parts = []
    for name, value in top:
        meta = ref.get(name, {}) or {}
        meaning = meta.get("meaning", name)
        parts.append(f"{name} ({int(round(value * 100))}% of current blend): {meaning}")
    return " | ".join(parts)


# ── Circadian ───────────────────────────────────────────────────────────────

def get_circadian_modifiers() -> dict:
    hour = datetime.now().hour
    if 5 <= hour < 9:
        return {"energy": -0.2, "calmness_boost": 0.15, "initiative_scale": 0.7,
                "tone_hint": "soft and unhurried — Iris is still waking up"}
    if 9 <= hour < 12:
        return {"energy": 0.1, "interest_boost": 0.1, "initiative_scale": 1.1,
                "tone_hint": "focused and engaged — morning clarity"}
    if 12 <= hour < 17:
        return {"energy": 0.0, "initiative_scale": 1.0,
                "tone_hint": "steady and grounded — afternoon pace"}
    if 17 <= hour < 21:
        return {"energy": -0.05, "calmness_boost": 0.1, "initiative_scale": 0.95,
                "tone_hint": "relaxed and conversational — evening wind-down"}
    return {"energy": -0.3, "calmness_boost": 0.25, "initiative_scale": 0.5,
            "tone_hint": "quiet and low-key — late night, Iris is calm and doesn't push topics"}


def _apply_circadian_to_emotion_weights(weights: dict) -> dict:
    """Blend circadian energy / calmness / interest into emotion weights."""
    circ = get_circadian_modifiers()
    w = {k: float(v) for k, v in dict(weights).items()}
    cb = float(circ.get("calmness_boost") or 0.0)
    if cb and "calmness" in w:
        w["calmness"] = max(0.0, w["calmness"] + cb * 0.2)
    ib = float(circ.get("interest_boost") or 0.0)
    if ib and "interest" in w:
        w["interest"] = max(0.0, w["interest"] + ib * 0.15)
    en = float(circ.get("energy") or 0.0)
    if en:
        for nm, fac in (("interest", 0.12), ("excitement", 0.08), ("joy", 0.05)):
            if nm in w:
                w[nm] = max(0.0, w[nm] + en * fac)
        if en < 0:
            if "calmness" in w:
                w["calmness"] = max(0.0, w["calmness"] - en * 0.12)
            if "boredom" in w:
                w["boredom"] = max(0.0, w["boredom"] + abs(en) * 0.03)
    return normalize_emotions(w)


# ── Normalization + selection ───────────────────────────────────────────────

def normalize_emotions(weights: dict) -> dict:
    fixed = {name: max(0.0, float(weights.get(name, 0.0))) for name in EMOTION_NAMES}
    total = sum(fixed.values())
    if total <= 0:
        return DEFAULT_EMOTIONS.copy()
    return {k: v / total for k, v in fixed.items()}


def top_two_emotions(weights: dict) -> list[dict]:
    top = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:2]
    total = sum(v for _, v in top)
    if total <= 0:
        return [{"name": "interest", "percent": 50}, {"name": "calmness", "percent": 50}]
    result = [{"name": n, "percent": round((v / total) * 100)} for n, v in top]
    if len(result) == 2:
        diff = 100 - (result[0]["percent"] + result[1]["percent"])
        result[0]["percent"] += diff
    return result


_BASELINE_EMOTIONS = frozenset(["calmness", "satisfaction", "neutral", "contentment"])
_SALIENT_EMOTIONS = frozenset([
    "frustration", "annoyance", "distress", "anger", "sadness", "anxiety",
    "fear", "empathetic pain", "boredom", "loneliness", "shame", "envy",
    "horror", "disgust", "awkwardness", "confusion",
    "joy", "excitement", "amusement", "love", "awe", "interest", "adoration",
    "entrancement", "hope", "pride", "relief",
])
_SALIENCE_THRESHOLD = 0.15


def pick_current_mood(weights: dict) -> str:
    """Honest-state mood label. If a baseline emotion (calmness, etc.) is on
    top but a salient emotion (frustration, joy, anxiety) is at >=15%, surface
    the salient one. Per Zeke 2026-05-08: 'the test isn't whether she can say
    calm — it's whether she actually says what she's feeling.'"""
    if not weights:
        return "interest"
    sorted_w = sorted(weights.items(), key=lambda x: -x[1])
    if not sorted_w:
        return "interest"
    top_name, top_val = sorted_w[0]
    total = sum(v for _, v in sorted_w) or 1.0
    top_share = top_val / total
    if top_name in _BASELINE_EMOTIONS and top_share < 0.6:
        for name, val in sorted_w[1:5]:
            if name in _SALIENT_EMOTIONS and (val / total) >= _SALIENCE_THRESHOLD:
                return name
    return top_name


# ── Mood load/save (the public interface) ───────────────────────────────────

def default_mood() -> dict:
    """Iris's bootstrap mood. Steady, watchful baseline."""
    weights = normalize_emotions(DEFAULT_EMOTIONS.copy())
    style_scores = compute_style_scores(weights, load_emotion_reference())
    return {
        "current_mood": "steady",
        "emotion_weights": weights,
        "primary_emotions": top_two_emotions(weights),
        "dominant_style": "focused",
        "style_scores": style_scores,
        "style_blend": top_two_styles(style_scores),
        "behavior_modifiers": compute_behavior_modifiers(weights, style_scores),
        "emotion_interpretation": build_emotion_interpretation(weights, load_emotion_reference()),
        "outward_tone": describe_style_blend(style_scores),
        "last_updated": now_iso(),
        "reason": "startup",
    }


def enrich_mood_state(mood: dict | None = None, emotion_reference: dict | None = None) -> dict:
    """Derive primary/style/behavior fields from emotion_weights."""
    mood = dict(mood or {})
    weights = normalize_emotions(mood.get("emotion_weights", DEFAULT_EMOTIONS.copy()))
    ref = emotion_reference or load_emotion_reference()
    style_scores = compute_style_scores(weights, ref)
    style_blend = top_two_styles(style_scores)
    dominant_style = max(style_scores, key=style_scores.get) if style_scores else "neutral"
    behavior = compute_behavior_modifiers(weights, style_scores)
    mood["emotion_weights"] = weights
    mood["primary_emotions"] = top_two_emotions(weights)
    mood["current_mood"] = pick_current_mood(weights)
    mood["style_scores"] = {k: round(float(v), 4) for k, v in style_scores.items()}
    mood["style_blend"] = style_blend
    mood["dominant_style"] = dominant_style
    mood["behavior_modifiers"] = {k: round(float(v), 4) for k, v in behavior.items()}
    mood["emotion_interpretation"] = build_emotion_interpretation(weights, ref)
    mood["outward_tone"] = describe_style_blend(style_scores)
    return mood


def load_mood() -> dict:
    """Full enriched mood state. ~115ms cost — for prompts and snapshot only."""
    p = _mood_path()
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            last_saved = data.get("_saved_at")
            if last_saved:
                try:
                    elapsed = time.time() - datetime.fromisoformat(last_saved).timestamp()
                    decay_rate = min(0.85, elapsed / 3600 * 0.15)
                    baseline = DEFAULT_EMOTIONS.copy()
                    weights = dict(data.get("emotion_weights", DEFAULT_EMOTIONS.copy()))
                    for k in list(weights.keys()):
                        if k in baseline:
                            weights[k] = weights[k] + (baseline[k] - weights[k]) * decay_rate
                    data["emotion_weights"] = weights
                except Exception:
                    pass
            data["emotion_weights"] = _apply_circadian_to_emotion_weights(
                data.get("emotion_weights", DEFAULT_EMOTIONS.copy())
            )
            return enrich_mood_state(data)
        except Exception as e:
            print(f"[mood_core] mood load error: {e}")
    dm = default_mood()
    dm["emotion_weights"] = _apply_circadian_to_emotion_weights(dm["emotion_weights"])
    return enrich_mood_state(dm)


def save_mood(mood: dict):
    try:
        p = _mood_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        mood = dict(mood)
        mood["_saved_at"] = now_iso()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(mood, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[mood_core] save error: {e}")


def load_mood_raw() -> dict:
    """Fast read — no enrichment. For tick budgets <=50ms."""
    p = _mood_path()
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[mood_core] raw load error: {e}")
    return {"emotion_weights": dict(DEFAULT_EMOTIONS)}


def save_mood_raw(mood: dict):
    """Fast persist — skip enrichment."""
    try:
        p = _mood_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        mood = dict(mood)
        mood["_saved_at"] = now_iso()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(mood, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[mood_core] raw save error: {e}")


# ── Bootstrap entry point for iris_runtime ──────────────────────────────────

def bootstrap_mood(g: dict[str, Any]) -> None:
    """Wire mood into the global state dict. Mirrors avaagent.py's startup flow.

    After this runs:
      g["MOOD_PATH"]            — path to the mood JSON
      g["default_mood"]         — callable
      g["load_mood"]            — callable
      g["save_mood"]            — callable
      g["load_mood_raw"]        — callable
      g["save_mood_raw"]        — callable
      g["enrich_mood_state"]    — callable
      g["pick_current_mood"]    — callable
      g["normalize_emotions"]   — callable

    These are the host-dict callables the rest of brain/* expects.
    """
    base = Path(g.get("BASE_DIR") or ".")
    configure(base)
    ensure_emotion_reference_file()

    g["MOOD_PATH"] = _mood_path()
    g["EMOTION_REFERENCE_PATH"] = _emotion_reference_path()
    g["default_mood"] = default_mood
    g["load_mood"] = load_mood
    g["save_mood"] = save_mood
    g["load_mood_raw"] = load_mood_raw
    g["save_mood_raw"] = save_mood_raw
    g["enrich_mood_state"] = enrich_mood_state
    g["pick_current_mood"] = pick_current_mood
    g["normalize_emotions"] = normalize_emotions
    g["top_two_emotions"] = top_two_emotions
    g["compute_style_scores"] = compute_style_scores
    g["compute_behavior_modifiers"] = compute_behavior_modifiers
    g["load_emotion_reference"] = load_emotion_reference
    g["EMOTION_NAMES"] = EMOTION_NAMES
    g["DEFAULT_EMOTIONS"] = DEFAULT_EMOTIONS

    # Seed the mood file if missing.
    if not _mood_path().exists():
        save_mood(default_mood())
        print(f"[mood_core] seeded default mood at {_mood_path()}")
    else:
        print(f"[mood_core] mood ready (path={_mood_path()})")
