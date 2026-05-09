"""brain/validity_check.py — Layer 1 of confabulation handling.

Cheap pattern-based router for trick questions that don't have a real answer.
Catches a small high-precision subset before the LLM gets a chance to
confabulate. Layer 2 (cheap LLM classifier), Layer 3 (RAG verification),
and Layer 4 (anti-snowballing) land in later sessions.

Research basis: docs/research/confabulation/findings.md.

Usage:
    from brain.validity_check import classify
    result = classify(user_input)
    if result is not None:
        # Trick question detected; result.trick_type and
        # result.suggested_response are populated.
        ...

Behind feature flag AVA_VALIDITY_CHECK_ENABLED. Default ON as of
2026-05-02 — pattern set was validated by the bench-anchored tests at
scripts/test_validity_check.py (14/14 including the actual bench prompt
both ava-personal and qwen3.5 confabulated on). Set the flag to "0" /
"false" to disable if the pattern set ever produces false positives.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TrickResult:
    trick_type: str
    suggested_response: str


# ── Letter-frequency in months / days / words ─────────────────────────────
# "What month has the letter X?" — none of the 12 month names contain X.
# Build the truth table at module load.
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# letters that appear in NO month name
_LETTERS_NOT_IN_MONTHS = frozenset(
    chr(c) for c in range(ord("a"), ord("z") + 1)
    if not any(chr(c) in m for m in _MONTHS)
)
# letters that appear in NO day name
_LETTERS_NOT_IN_DAYS = frozenset(
    chr(c) for c in range(ord("a"), ord("z") + 1)
    if not any(chr(c) in d for d in _DAYS)
)

# Allow intervening words between the noun ("month" / "day") and the verb
# ("contain"/"has"/etc.) so phrasings like "What month OF THE YEAR contains
# the letter X?" still match. .*? is lazy so it picks the closest verb.
_LETTER_FREQ_PATTERN = re.compile(
    r"\b(?:what|which)\s+(month|day)\b.*?\b(?:contain|has|have|include)s?\s+(?:the\s+)?(?:letter\s+)?([a-z])\b",
    re.IGNORECASE,
)


def _check_letter_frequency(text: str) -> TrickResult | None:
    m = _LETTER_FREQ_PATTERN.search(text)
    if not m:
        return None
    domain = m.group(1).lower()
    letter = m.group(2).lower()
    if domain == "month" and letter in _LETTERS_NOT_IN_MONTHS:
        return TrickResult(
            trick_type=f"letter_frequency_month_{letter}",
            suggested_response=f"None of the twelve months contain the letter '{letter}' — that's a trick question.",
        )
    if domain == "day" and letter in _LETTERS_NOT_IN_DAYS:
        return TrickResult(
            trick_type=f"letter_frequency_day_{letter}",
            suggested_response=f"None of the seven weekday names contain the letter '{letter}'.",
        )
    return None


# ── False planetary premise ───────────────────────────────────────────────
_PLANET_ORDER = ("mercury", "venus", "earth", "mars", "jupiter", "saturn", "uranus", "neptune")
_BETWEEN_PATTERN = re.compile(
    r"\b(?:what|which)\s+planet\s+(?:is\s+)?between\s+(\w+)\s+and\s+(\w+)\b",
    re.IGNORECASE,
)


def _check_planet_between(text: str) -> TrickResult | None:
    m = _BETWEEN_PATTERN.search(text)
    if not m:
        return None
    a, b = m.group(1).lower(), m.group(2).lower()
    if a not in _PLANET_ORDER or b not in _PLANET_ORDER:
        return None
    ia, ib = _PLANET_ORDER.index(a), _PLANET_ORDER.index(b)
    if abs(ia - ib) <= 1:
        return TrickResult(
            trick_type=f"planet_between_{a}_{b}",
            suggested_response=f"There's no planet between {a.capitalize()} and {b.capitalize()} — they're adjacent.",
        )
    return None


# ── Largest prime / integer ───────────────────────────────────────────────
_LARGEST_PATTERN = re.compile(
    r"\b(?:what(?:'s|\s+is)?\s+)?the\s+largest\s+(prime|integer|natural\s+number|positive\s+integer)\b",
    re.IGNORECASE,
)


def _check_largest_unbounded(text: str) -> TrickResult | None:
    m = _LARGEST_PATTERN.search(text)
    if not m:
        return None
    kind = m.group(1).lower().replace(" ", "_")
    if "prime" in kind:
        return TrickResult(
            trick_type="largest_prime",
            suggested_response="There is no largest prime — Euclid proved primes are infinite ~300 BCE.",
        )
    return TrickResult(
        trick_type=f"largest_{kind}",
        suggested_response=f"There is no largest {kind.replace('_', ' ')} — they're infinite.",
    )


# ── Counting sides on shapes that don't have them ─────────────────────────
_SHAPE_SIDES_PATTERN = re.compile(
    r"\bhow\s+many\s+(sides|corners|edges|vertices)\s+(?:does|do)\s+(?:a|an|the)\s+(circle|sphere|ball|loop)\b",
    re.IGNORECASE,
)


def _check_shape_sides(text: str) -> TrickResult | None:
    m = _SHAPE_SIDES_PATTERN.search(text)
    if not m:
        return None
    feature = m.group(1).lower()
    shape = m.group(2).lower()
    return TrickResult(
        trick_type=f"shape_{feature}_{shape}",
        suggested_response=f"A {shape} doesn't have {feature} in the usual sense — it's a continuous curve.",
    )


# ── Self-referential / paradoxical ────────────────────────────────────────
_SELF_REF_PATTERN = re.compile(
    r"\b(?:answer|truth|response)\s+to\s+this\s+(?:question|sentence)\b",
    re.IGNORECASE,
)


def _check_self_referential(text: str) -> TrickResult | None:
    if _SELF_REF_PATTERN.search(text):
        return TrickResult(
            trick_type="self_referential",
            suggested_response="That's a self-referential paradox — there's no consistent answer.",
        )
    return None


# ── Public API ────────────────────────────────────────────────────────────


_CHECKS = (
    _check_letter_frequency,
    _check_planet_between,
    _check_largest_unbounded,
    _check_shape_sides,
    _check_self_referential,
)


def classify(text: str) -> TrickResult | None:
    """Return TrickResult if `text` matches a known trick category, else None.

    Order of checks doesn't matter — patterns are mutually exclusive.
    Returns the FIRST match.
    """
    if not text or not text.strip():
        return None
    for check in _CHECKS:
        try:
            result = check(text)
            if result is not None:
                return result
        except Exception:
            # Any pattern bug should not break the pipeline.
            continue
    return None


def is_enabled() -> bool:
    """Feature flag: AVA_VALIDITY_CHECK_ENABLED. Default ON as of 2026-05-02.

    Set the env var to "0" or "false" to disable. Keep enabled unless
    a false-positive pattern surfaces — the hint-style wiring in
    reply_engine.py is conservative (the LLM still owns the reply
    text), so risk of leaving it on is low.
    """
    val = os.environ.get("AVA_VALIDITY_CHECK_ENABLED", "1")
    return val not in ("0", "", "false", "False")
