"""brain/emotional_acknowledgment.py — Acknowledge emotion first (C9).

When the user says something with emotional weight ("I had a rough day",
"I'm worried about X", "I'm scared", "this is exciting"), Ava should
acknowledge the emotion BEFORE handling any task content. Real
conversation does this — "rough how? want to talk about it?" comes
before "what task should I do."

Detection: regex patterns matching emotional language. When matched,
the reply path can prepend an acknowledgment OR take an
empathy-first introspection-style reply instead of routing to actions.

API:

    from brain.emotional_acknowledgment import (
        detect_emotional_content, build_acknowledgment_prefix,
        is_distress_signal,
    )

    state = detect_emotional_content("I had a rough day")
    if state:
        # state.kind = "distress" | "joy" | "worry" | "anger" | ...
        prefix = build_acknowledgment_prefix(state)
        # -> "Rough how? "
        ...
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


EmotionKind = Literal[
    "distress",     # rough day, sad, exhausted, overwhelmed
    "worry",        # anxious, worried, nervous, scared
    "joy",          # excited, happy, thrilled
    "anger",        # frustrated, angry, mad, pissed
    "grief",        # mourning, lost, missing someone
    "loneliness",   # alone, lonely, isolated
    "gratitude",    # thank you, grateful
    "vulnerability",# honest, hard for me, vulnerable
]


@dataclass
class EmotionalContent:
    kind: EmotionKind
    intensity: float  # 0-1, rough estimate from word strength
    matched_phrase: str
    full_text: str


_EMOTION_PATTERNS: list[tuple[re.Pattern[str], EmotionKind, float]] = [
    # Distress
    (re.compile(r"\b(?:rough|tough|hard|terrible|awful|bad|shitty|crap|crappy)\s+(?:day|week|night|month|time)\b", re.IGNORECASE), "distress", 0.6),
    (re.compile(r"\bi'?m (?:exhausted|drained|burnt out|burnt-out|spent|wiped)\b", re.IGNORECASE), "distress", 0.7),
    (re.compile(r"\bi'?m (?:overwhelmed|crushed|drowning|underwater)\b", re.IGNORECASE), "distress", 0.8),
    (re.compile(r"\b(?:i feel|feeling) (?:sad|down|low|blue|depressed|defeated|hopeless)\b", re.IGNORECASE), "distress", 0.7),
    # Worry
    (re.compile(r"\bi'?m (?:worried|anxious|nervous|scared|afraid|stressed)\b", re.IGNORECASE), "worry", 0.6),
    (re.compile(r"\b(?:i feel|feeling) (?:worried|anxious|nervous|scared|afraid|stressed)\b", re.IGNORECASE), "worry", 0.6),
    (re.compile(r"\bworried about\b", re.IGNORECASE), "worry", 0.5),
    # Joy
    (re.compile(r"\bi'?m (?:excited|thrilled|happy|stoked|pumped|delighted)\b", re.IGNORECASE), "joy", 0.6),
    (re.compile(r"\b(?:i feel|feeling) (?:great|happy|wonderful|amazing|fantastic)\b", re.IGNORECASE), "joy", 0.6),
    (re.compile(r"\bso (?:excited|happy|stoked|pumped)\b", re.IGNORECASE), "joy", 0.7),
    # Anger
    (re.compile(r"\bi'?m (?:frustrated|angry|mad|pissed|annoyed|irritated)\b", re.IGNORECASE), "anger", 0.6),
    (re.compile(r"\b(?:i feel|feeling) (?:frustrated|angry|mad|annoyed|irritated)\b", re.IGNORECASE), "anger", 0.6),
    (re.compile(r"\b(?:so |really )?(?:fed up|sick of)\b", re.IGNORECASE), "anger", 0.7),
    # Grief
    (re.compile(r"\b(?:i lost|i'?m mourning|i miss|missing) (?:my|someone)\b", re.IGNORECASE), "grief", 0.8),
    (re.compile(r"\b(?:passed away|died|gone)\b", re.IGNORECASE), "grief", 0.6),
    # Loneliness
    (re.compile(r"\bi'?m (?:lonely|alone|isolated)\b", re.IGNORECASE), "loneliness", 0.7),
    (re.compile(r"\b(?:i feel|feeling) (?:lonely|alone|isolated|disconnected)\b", re.IGNORECASE), "loneliness", 0.7),
    # Gratitude
    (re.compile(r"\bthank(?:s| you|s a lot| you so much)\b", re.IGNORECASE), "gratitude", 0.4),
    (re.compile(r"\b(?:i'?m grateful|appreciate it|means a lot)\b", re.IGNORECASE), "gratitude", 0.6),
    # Vulnerability
    (re.compile(r"\b(?:hard for me|vulnerable|honestly)\b", re.IGNORECASE), "vulnerability", 0.5),
    (re.compile(r"\bi never (?:told|shared|said) (?:anyone|this)\b", re.IGNORECASE), "vulnerability", 0.9),
]


def detect_emotional_content(text: str) -> EmotionalContent | None:
    """Detect emotional content in `text`. Returns the strongest match,
    or None if no emotional language detected.
    """
    if not text:
        return None
    best: EmotionalContent | None = None
    for pat, kind, intensity in _EMOTION_PATTERNS:
        m = pat.search(text)
        if m is None:
            continue
        if best is None or intensity > best.intensity:
            best = EmotionalContent(
                kind=kind,
                intensity=intensity,
                matched_phrase=m.group(0),
                full_text=text,
            )
    return best


def is_distress_signal(text: str) -> bool:
    """Quick check: does this look like distress / vulnerability /
    grief / loneliness? These deserve emotional acknowledgment FIRST,
    even if there's a task ask in the same message."""
    detected = detect_emotional_content(text)
    if detected is None:
        return False
    return detected.kind in ("distress", "grief", "loneliness", "vulnerability") and detected.intensity >= 0.6


_ACK_PREFIXES: dict[EmotionKind, list[str]] = {
    "distress": [
        "Rough how? ",
        "I'm sorry to hear that. ",
        "That sounds heavy. ",
        "Hey — that sounds hard. ",
    ],
    "worry": [
        "Hey — what's worrying you? ",
        "That sounds stressful. ",
        "Tell me about it. ",
    ],
    "joy": [
        "That's great. ",
        "Nice. ",
        "Tell me more. ",
    ],
    "anger": [
        "What happened? ",
        "Yeah, that sounds frustrating. ",
        "Tell me about it. ",
    ],
    "grief": [
        "I'm so sorry. ",
        "That's a lot to carry. ",
    ],
    "loneliness": [
        "I hear you. ",
        "That's hard. ",
    ],
    "gratitude": [
        "",  # often no need to deflect; just respond
    ],
    "vulnerability": [
        "Thank you for telling me. ",
        "That means a lot — thanks for trusting me with that. ",
    ],
}


def build_acknowledgment_prefix(state: EmotionalContent) -> str:
    """Pick a fresh acknowledgment prefix for the detected emotion.

    Returns "" if the emotion type doesn't warrant a prefix
    (e.g., gratitude usually flows naturally without it).
    """
    import random
    options = _ACK_PREFIXES.get(state.kind, [""])
    if not options:
        return ""
    choice = random.choice(options)
    return choice


def acknowledgment_hint_for_introspection(state: EmotionalContent) -> str:
    """Hint to add to introspection composer's system prompt when the
    user is in an emotional state. Tells the LLM to lead with empathy."""
    if state.kind == "distress":
        return (
            "\n\nThe person you're talking to is having a rough time. "
            "Lead with empathy. Don't pivot to tasks. Hold space."
        )
    if state.kind == "vulnerability":
        return (
            "\n\nThe person is sharing something vulnerable. "
            "Receive it. Don't problem-solve. Honor the trust."
        )
    if state.kind == "grief":
        return (
            "\n\nThe person is grieving. Be gentle. Don't fix; sit with it."
        )
    if state.kind == "loneliness":
        return (
            "\n\nThe person is feeling lonely. Be warm. Don't deflect."
        )
    if state.kind == "joy":
        return "\n\nThe person is happy. Match their energy."
    if state.kind == "anger":
        return "\n\nThe person is frustrated. Validate first; help second."
    return ""
