"""brain/conversation_repair.py — Conversational repair (B7).

When Ava's been corrected 2+ times in a row, she should switch register
to "I think I'm losing the thread — walk me through what you actually
want." Currently when corrections stack, she keeps trying the same
shape of reply. Real conversation has REPAIR moments — both parties
acknowledge they're crosstalking and reset.

Detection: scan recent turns for correction phrases like "no",
"that's not what I meant", "I meant", "wait", "stop", "you misunderstood",
"that's wrong". Counter increments on detection; resets on apparent
success (no correction in next turn).

When threshold (2 in last 5 turns) crossed, the repair register fires:
Ava prefixes her next reply with a brief acknowledgment + asks for
clarification rather than trying to handle the request again.

API:

    from brain.conversation_repair import (
        is_correction, mark_correction, mark_success,
        should_use_repair_register, repair_prefix,
    )

    if is_correction(user_input):
        mark_correction(g)
    else:
        mark_success(g)

    if should_use_repair_register(g):
        # Override normal reply with repair register
        prefix = repair_prefix()
        # ... fold into reply
"""
from __future__ import annotations

import re
import time
from typing import Any


_CORRECTION_PATTERNS = [
    re.compile(r"^\s*no[,\s.]", re.IGNORECASE),
    re.compile(r"\bthat'?s\s+(?:not|wrong|incorrect)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+(?:what\s+i\s+(?:meant|asked|said)|right|correct)\b", re.IGNORECASE),
    re.compile(r"\bi\s+meant\s+", re.IGNORECASE),
    re.compile(r"\b(?:you\s+(?:misunderstood|got\s+it\s+wrong)|misunderstood)\b", re.IGNORECASE),
    re.compile(r"\bwait[,\s.]", re.IGNORECASE),
    re.compile(r"\bstop[,\s.]", re.IGNORECASE),
    re.compile(r"\bnope[,\s.]", re.IGNORECASE),
    re.compile(r"\bwrong\s+answer\b", re.IGNORECASE),
    re.compile(r"\bthat'?s\s+not\s+(?:it|what\s+i\s+wanted|right)\b", re.IGNORECASE),
    re.compile(r"\btry\s+(?:again|that\s+again)\b", re.IGNORECASE),
    re.compile(r"\bi\s+didn'?t\s+(?:want|ask|mean)\s+", re.IGNORECASE),
]


_REPAIR_THRESHOLD = 2  # corrections needed to trigger repair
_REPAIR_WINDOW_TURNS = 5  # within how many recent turns


def is_correction(text: str) -> bool:
    """True if `text` looks like a user correction of Ava's previous turn."""
    if not text:
        return False
    return any(p.search(text) for p in _CORRECTION_PATTERNS)


def mark_correction(g: dict[str, Any]) -> None:
    """Record that the current turn was a correction.

    Tracked in g state as a list of (ts, was_correction) pairs. Bounded
    to the last _REPAIR_WINDOW_TURNS entries.
    """
    history = list(g.get("_correction_history") or [])
    history.append({"ts": time.time(), "correction": True})
    g["_correction_history"] = history[-_REPAIR_WINDOW_TURNS:]


def mark_success(g: dict[str, Any]) -> None:
    """Record a successful turn (not a correction)."""
    history = list(g.get("_correction_history") or [])
    history.append({"ts": time.time(), "correction": False})
    g["_correction_history"] = history[-_REPAIR_WINDOW_TURNS:]


def correction_count_in_window(g: dict[str, Any]) -> int:
    history = list(g.get("_correction_history") or [])
    return sum(1 for h in history if h.get("correction"))


def should_use_repair_register(g: dict[str, Any]) -> bool:
    """Have we hit the repair threshold?"""
    return correction_count_in_window(g) >= _REPAIR_THRESHOLD


def reset_repair_state(g: dict[str, Any]) -> None:
    """After a repair attempt, reset so we don't keep firing repair
    register indefinitely."""
    g["_correction_history"] = []


_REPAIR_PREFIXES = [
    "Hold on — I think I'm losing the thread. ",
    "Let me back up. ",
    "I'm getting tangled — ",
    "Okay, I clearly didn't get that right. ",
]


_REPAIR_QUESTIONS = [
    "What did you actually want me to do?",
    "Can you walk me through what you're trying to get to?",
    "Tell me again what you meant — I'm getting it wrong.",
    "Let's start over — what's the goal?",
]


def repair_prefix() -> str:
    """Generate a fresh repair-register opener.

    Combines a soft acknowledgment + an open-ended clarification
    question. Ava's actual reply should DEFER to this — not try
    to answer the original request again until clarified.
    """
    import random
    return random.choice(_REPAIR_PREFIXES) + random.choice(_REPAIR_QUESTIONS)


def maybe_apply_repair(g: dict[str, Any], user_input: str) -> str | None:
    """If we should repair, return the repair-register reply; else None.

    Caller should:
        repair = maybe_apply_repair(g, user_input)
        if repair is not None:
            return repair
        # ... otherwise normal reply path

    After returning a repair, mark the state reset — we don't want to
    keep firing repair on every subsequent turn.
    """
    if is_correction(user_input):
        mark_correction(g)
    if should_use_repair_register(g):
        reset_repair_state(g)
        return repair_prefix()
    return None
