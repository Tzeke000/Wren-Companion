"""brain/preference_learning.py — Per-person preference learning (B6).

Accumulates style preferences for each person from their feedback
signals. The Person Registry already has a preferences map per
person; this module is the EXTRACTION + APPLICATION layer.

Preferences tracked:

  reply_length        - "short" | "medium" | "long"
                        Inferred from "be shorter" / "I want more
                        detail" type corrections.
  formality          - "casual" | "neutral" | "formal"
                        Inferred from how they address Ava + how
                        she's asked to address them.
  addressed_as       - what they like being called
                        (e.g., "Zeke" not "sir", "user", "Ezekiel")
  prefers_emojis     - bool — most people don't; some do
  prefers_markdown   - bool — code/dev folks tend to want it,
                        casual chat tends not to
  technical_depth    - "casual" | "intermediate" | "expert"
                        Inferred from how technical their queries are.
  voice_speed        - float (multiplier) — Kokoro speed adjustment
  voice_volume       - float (gain) — relative TTS gain

Why this matters: each person gets THEIR Ava. Zeke wants concise
technical replies; Shonda might prefer warm explanations; an unknown
guest gets formal-default. Without this layer, Ava performs the
SAME register for everyone.

API:

    from brain.preference_learning import (
        infer_preferences_from_feedback,
        learn_from_correction,
        apply_preferences_hint,
    )

    # User says "be shorter" → record:
    learn_from_correction(person_id="zeke", correction="be shorter")

    # Generating a reply → fetch hint:
    hint = apply_preferences_hint(person_id="zeke")
    # -> "Reply preferences: short, casual, no markdown."
    # Fold into the system prompt or fast-path style.

Storage: Person Registry's preferences dict. This module just
provides the inference + application contract.
"""
from __future__ import annotations

import re
from typing import Any


# ── Correction pattern detection ──────────────────────────────────────────


_CORRECTION_PATTERNS: list[tuple[re.Pattern[str], str, Any]] = [
    # (regex, preference_key, value)
    (re.compile(r"\b(?:be|reply) shorter\b|\btoo (?:long|wordy)\b|\bless verbose\b", re.IGNORECASE),
     "reply_length", "short"),
    (re.compile(r"\b(?:more detail|more thorough|expand|tell me more)\b", re.IGNORECASE),
     "reply_length", "long"),
    (re.compile(r"\bcasual(?:ly)?\b|\bless formal\b|\brelax\b", re.IGNORECASE),
     "formality", "casual"),
    (re.compile(r"\b(?:no|stop|don't use) (?:emojis?|emoticons?)\b", re.IGNORECASE),
     "prefers_emojis", False),
    (re.compile(r"\bmore emojis?\b|\buse emojis?\b", re.IGNORECASE),
     "prefers_emojis", True),
    (re.compile(r"\b(?:no|stop|don't use) markdown\b", re.IGNORECASE),
     "prefers_markdown", False),
    (re.compile(r"\b(?:use|with) markdown\b", re.IGNORECASE),
     "prefers_markdown", True),
    (re.compile(r"\bcall me ([A-Z][a-z]{1,30})\b"),
     "addressed_as", None),  # captures via regex group
    (re.compile(r"\b(?:don't|stop) call(?:ing)? me sir\b", re.IGNORECASE),
     "addressed_as_not_sir", True),
    (re.compile(r"\bspeak (?:slower|more slowly)\b", re.IGNORECASE),
     "voice_speed", 0.85),
    (re.compile(r"\bspeak (?:faster|more quickly)\b", re.IGNORECASE),
     "voice_speed", 1.15),
    (re.compile(r"\b(?:louder|speak (?:up|louder))\b", re.IGNORECASE),
     "voice_volume", 1.2),
    (re.compile(r"\b(?:quieter|speak (?:more )?(?:quietly|softer))\b", re.IGNORECASE),
     "voice_volume", 0.85),
]


def detect_preference_signals(text: str) -> list[tuple[str, Any]]:
    """Scan `text` for known preference correction patterns.

    Returns list of (preference_key, value) pairs found.
    """
    if not text:
        return []
    out: list[tuple[str, Any]] = []
    for pat, key, value in _CORRECTION_PATTERNS:
        m = pat.search(text)
        if m is None:
            continue
        if value is None:
            # Captures from the regex group (e.g. "call me Zeke" -> "Zeke")
            try:
                captured = m.group(1).strip()
                if captured:
                    out.append((key, captured))
            except IndexError:
                continue
        else:
            out.append((key, value))
    return out


# ── Learning from feedback ────────────────────────────────────────────────


def learn_from_correction(person_id: str, correction: str) -> int:
    """When the user provides a correction, extract preference signals
    and persist them via Person Registry.

    Returns count of preferences updated.
    """
    if not correction:
        return 0
    signals = detect_preference_signals(correction)
    if not signals:
        return 0
    try:
        from brain.person_registry import registry
    except Exception:
        return 0
    count = 0
    for key, value in signals:
        try:
            registry.set_preference(person_id, key, value)
            count += 1
        except Exception:
            continue
    return count


# ── Preference application ───────────────────────────────────────────────


def get_preferences(person_id: str) -> dict[str, Any]:
    """Fetch the current preference map for person_id."""
    try:
        from brain.person_registry import registry
        return registry.get_preferences(person_id)
    except Exception:
        return {}


def apply_preferences_hint(person_id: str) -> str:
    """Produce a short prompt-context string describing this person's
    preferences. Fold into system prompt for the reply paths.

    Returns empty string if no preferences are recorded yet.
    """
    prefs = get_preferences(person_id)
    if not prefs:
        return ""
    parts: list[str] = []
    if prefs.get("reply_length") == "short":
        parts.append("Keep replies short.")
    elif prefs.get("reply_length") == "long":
        parts.append("They prefer thorough replies with detail.")
    if prefs.get("formality") == "casual":
        parts.append("Casual register; not formal.")
    elif prefs.get("formality") == "formal":
        parts.append("They prefer formal register.")
    if prefs.get("addressed_as"):
        parts.append(f"Address them as '{prefs['addressed_as']}'.")
    if prefs.get("addressed_as_not_sir"):
        parts.append("Don't call them 'sir'.")
    if prefs.get("prefers_emojis") is True:
        parts.append("Emojis welcome.")
    elif prefs.get("prefers_emojis") is False:
        parts.append("No emojis.")
    if prefs.get("prefers_markdown") is False:
        parts.append("No markdown formatting in voice replies.")
    if prefs.get("technical_depth") == "expert":
        parts.append("They're technically expert; can be precise + jargon-OK.")
    if not parts:
        return ""
    return "Reply preferences: " + " ".join(parts)
