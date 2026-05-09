"""brain/confidence.py — Calibrated confidence on claims (B4).

Each claim Ava makes carries a confidence level. Low-confidence claims
get caveat language ("I'm not sure but..."). High-confidence claims
get clean delivery. Connects to provenance (architecture #5) — the
source_kind for a claim maps to a default confidence level.

Why this matters: confabulation breaks trust faster than honesty does.
Most LLMs deliver everything with the same flat confidence — sounding
equally certain about "Today is Tuesday" (true with high confidence)
and "Polar bears live in the Arctic" (true) and "The CEO of XYZ Corp
in 2026 is Alice Smith" (might be confabulation). Ava should sound
more or less certain depending on where the claim came from.

API:

    from brain.confidence import (
        ConfidenceLevel, wrap_for_source, apply_caveat,
        infer_from_provenance, source_kind_to_default_confidence,
    )

    # Wrap a reply with appropriate caveat language:
    final = wrap_for_source(
        "Polar bears are the largest land predator.",
        source_kind="training",
    )
    # -> "I'm pretty sure — polar bears are the largest land predator.
    #     That's from my training data, so it might be a few years
    #     out of date."

    # Or directly:
    final = apply_caveat(reply, "low")
    # -> "I'm not sure, but... <reply>"

Source kinds map to default confidence levels:
  user_told    -> high  (Zeke said it directly; trust the source)
  web          -> high  (live result from a search)
  observation  -> medium-high  (Ava noticed a pattern)
  training     -> medium  (LLM weights; might be stale or wrong)
  derived      -> medium-low  (synthesized; could chain wrong)
  memory       -> medium  (came from chat history)
"""
from __future__ import annotations

import random
from typing import Literal


ConfidenceLevel = Literal["high", "medium-high", "medium", "medium-low", "low"]


# ── Source kind -> default confidence ─────────────────────────────────────


_SOURCE_DEFAULTS: dict[str, ConfidenceLevel] = {
    "user_told": "high",        # explicit user-stated; trust the source
    "chat": "high",              # something a person said
    "web": "high",               # live web result
    "email": "medium-high",      # newsletter; trustworthy if source is curated
    "observation": "medium-high",# pattern Ava noticed
    "training": "medium",        # LLM training data — often right, sometimes stale
    "memory": "medium",          # retrieved from past chat
    "skill": "medium",           # from a learned skill
    "derived": "medium-low",     # synthesized from other sources
}


def source_kind_to_default_confidence(source_kind: str) -> ConfidenceLevel:
    return _SOURCE_DEFAULTS.get(str(source_kind or "").lower(), "medium")


def infer_from_provenance(claim_id: str) -> ConfidenceLevel:
    """Look up the provenance record's source_kind + confidence and
    map to a level. Falls back to medium if no record."""
    try:
        from brain.provenance import provenance
        rec = provenance.lookup(claim_id)
        if rec is None:
            return "medium"
        # If the record has explicit confidence (0-1), map directly:
        if rec.confidence >= 0.85:
            return "high"
        if rec.confidence >= 0.65:
            return "medium-high"
        if rec.confidence >= 0.45:
            return "medium"
        if rec.confidence >= 0.25:
            return "medium-low"
        return "low"
    except Exception:
        return "medium"


# ── Caveat language ───────────────────────────────────────────────────────


_CAVEAT_PREFIXES: dict[ConfidenceLevel, list[str]] = {
    "high": [
        "",  # often no caveat needed
        "Yes — ",
        "Definitely. ",
        "",
        "",
    ],
    "medium-high": [
        "I'm pretty sure ",
        "Probably ",
        "I think ",
        "",
    ],
    "medium": [
        "I think ",
        "If I remember right, ",
        "I believe ",
        "From what I know, ",
    ],
    "medium-low": [
        "I'm not totally sure, but ",
        "I think — but worth checking — ",
        "My best guess is that ",
    ],
    "low": [
        "I really don't know, but my guess would be ",
        "I'm not sure at all — possibly ",
        "Honestly, I don't know, though ",
    ],
}


_CAVEAT_SUFFIXES: dict[ConfidenceLevel, list[str]] = {
    "high": ["", "", ""],
    "medium-high": ["", "", " — pretty sure on that."],
    "medium": ["", "", " — though I might be off."],
    "medium-low": [
        " — but I'd want to double-check that.",
        " — but don't quote me.",
    ],
    "low": [
        " — but please look that up rather than relying on me.",
        " — I'd really verify that elsewhere.",
    ],
}


_SOURCE_NOTES: dict[str, str] = {
    "training": "That's from my training data, so it might be a few years out of date.",
    "memory": "That's from our past conversations.",
    "web": "I just looked that up online.",
    "email": "I read that recently.",
    "observation": "I noticed that pattern over time.",
    "user_told": "You told me that.",
    "derived": "I worked that out from a few different things.",
}


# ── Public API ────────────────────────────────────────────────────────────


def apply_caveat(claim: str, confidence: ConfidenceLevel) -> str:
    """Wrap a claim with caveat language matching the confidence level.

    For high-confidence claims, often returns the claim unchanged.
    For low-confidence claims, adds explicit hedging.
    """
    if not claim or not claim.strip():
        return claim
    text = claim.strip()
    prefix = random.choice(_CAVEAT_PREFIXES.get(confidence, [""]))
    suffix = random.choice(_CAVEAT_SUFFIXES.get(confidence, [""]))
    # Lowercase the first letter if we're prefixing, unless it's a
    # proper noun (caps after the first char too).
    if prefix and text and text[0].isupper() and len(text) > 1 and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return (prefix + text + suffix).strip()


def wrap_for_source(claim: str, source_kind: str = "training") -> str:
    """Combine caveat (from confidence) + source note (from kind).

    Returns the wrapped reply Ava would actually say.
    """
    confidence = source_kind_to_default_confidence(source_kind)
    caveated = apply_caveat(claim, confidence)
    note = _SOURCE_NOTES.get(str(source_kind or "").lower(), "")
    if note and confidence in ("medium", "medium-low", "low"):
        # Append source note for non-high-confidence claims so user
        # knows where it came from.
        if not caveated.rstrip().endswith((".", "!", "?")):
            caveated += "."
        caveated += " " + note
    return caveated


def confidence_phrase(confidence: ConfidenceLevel) -> str:
    """Short human-readable phrase for the level. Used for diagnostic
    surfaces and the future B4-aware reply paths."""
    return {
        "high": "I'm sure",
        "medium-high": "I'm pretty sure",
        "medium": "I think",
        "medium-low": "I'm not sure",
        "low": "I really don't know",
    }.get(confidence, "I'm not sure")
