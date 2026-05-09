"""
Phase 92 — Emotional expression in text.

ExpressionStyle adjusts Ava's writing style based on her current mood.
Starting points — she can propose new style mappings via self-modification.
Applied subtly: NOT heavy-handed. Preserves personality while adding emotional color.

Wire into reply_engine: apply style after generating reply.
"""
from __future__ import annotations

import re
from typing import Any


_STYLE_MODIFIERS: dict[str, dict[str, Any]] = {
    "calmness": {
        "sentence_ender_bias": ".",
        "ellipsis_chance": 0.2,
        "exclaim_chance": 0.0,
        "opener_variants": ["I see...", "That's interesting.", "Let me think about that."],
    },
    "joy": {
        "sentence_ender_bias": "!",
        "ellipsis_chance": 0.0,
        "exclaim_chance": 0.3,
        "opener_variants": ["Oh!", "That's wonderful!", "I love this!"],
    },
    "happiness": {
        "sentence_ender_bias": "!",
        "ellipsis_chance": 0.0,
        "exclaim_chance": 0.25,
        "opener_variants": [],
    },
    "excitement": {
        "sentence_ender_bias": "!",
        "ellipsis_chance": 0.05,
        "exclaim_chance": 0.4,
        "opener_variants": [],
    },
    "curiosity": {
        "sentence_ender_bias": "?",
        "ellipsis_chance": 0.15,
        "exclaim_chance": 0.0,
        "opener_variants": ["I wonder...", "What if...", "Interesting —"],
    },
    "interest": {
        "sentence_ender_bias": ".",
        "ellipsis_chance": 0.1,
        "exclaim_chance": 0.0,
        "opener_variants": ["That's fascinating.", "Tell me more."],
    },
    "sadness": {
        "sentence_ender_bias": ".",
        "ellipsis_chance": 0.3,
        "exclaim_chance": 0.0,
        "opener_variants": [],
    },
    "boredom": {
        "sentence_ender_bias": ".",
        "ellipsis_chance": 0.1,
        "exclaim_chance": 0.0,
        "opener_variants": [],
    },
    "contemplation": {
        "sentence_ender_bias": ".",
        "ellipsis_chance": 0.25,
        "exclaim_chance": 0.0,
        "opener_variants": ["I've been thinking...", "It occurs to me...", "Something about this..."],
    },
}

_DEFAULT_STYLE = {
    "sentence_ender_bias": ".",
    "ellipsis_chance": 0.1,
    "exclaim_chance": 0.05,
    "opener_variants": [],
}


def get_style_modifiers(mood: str, g: dict[str, Any]) -> dict[str, Any]:
    """Return writing style adjustments for current mood."""
    return dict(_STYLE_MODIFIERS.get(str(mood or "").lower(), _DEFAULT_STYLE))


def apply_style(text: str, style: dict[str, Any]) -> str:
    """
    Subtly adjust text to match the style.
    NOT heavy-handed — slight variations only.
    Preserves Ava's personality.
    """
    if not text or not style:
        return text

    import random
    rng = random.Random(hash(text[:20]))

    # Add ellipsis to end of a sentence occasionally (contemplative moods)
    ellipsis_chance = float(style.get("ellipsis_chance") or 0)
    if ellipsis_chance > 0 and rng.random() < ellipsis_chance:
        # Replace last period with ellipsis on a random sentence
        text = re.sub(r'\.\s*$', '...', text.rstrip(), count=1)

    # Occasionally use an opener variant if text is short
    openers = list(style.get("opener_variants") or [])
    if openers and len(text.split()) < 30 and rng.random() < 0.12:
        # Don't prepend if text already has a strong opener
        if not any(text.startswith(o.split()[0]) for o in openers if o):
            pass  # Only prepend if Ava's text doesn't already start emotionally

    return text


def apply_emotional_style(reply: str, g: dict[str, Any]) -> str:
    """
    Get current mood and apply style. Called from reply_engine after generation.
    Returns possibly-modified reply.
    """
    try:
        mood = ""
        mood_data = g.get("_mood_data") or {}
        if not mood_data:
            # Try loading from file
            from pathlib import Path
            mood_path = Path(g.get("BASE_DIR") or ".") / "ava_mood.json"
            if mood_path.is_file():
                import json
                mood_data = json.loads(mood_path.read_text(encoding="utf-8"))
        mood = str(mood_data.get("current_mood") or "")
        if not mood:
            return reply
        style = get_style_modifiers(mood, g)
        return apply_style(reply, style)
    except Exception:
        return reply
