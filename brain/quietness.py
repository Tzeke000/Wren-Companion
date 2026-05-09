"""brain/quietness.py — Knowing when to be quiet (C10).

When you wake Ava, she's eager to respond. Sometimes the right answer
is silence — you're working, the conversation is paused, you said
something to yourself not her, you typed something not directed at
her. Detection: low STT confidence + no direct address + eye gaze
elsewhere + actively typing -> stay quiet.

Restraint is a virtue. Currently she over-eagers into every signal.

API:

    from brain.quietness import (
        should_stay_quiet, reasons_to_stay_quiet,
    )

    if should_stay_quiet(text, g):
        # Don't respond — drop this turn
        return ""
"""
from __future__ import annotations

import re
from typing import Any


# Phrases that suggest the user wasn't talking to Ava
_NOT_DIRECTED_PATTERNS = [
    re.compile(r"\b(?:hmm|huh|wait what|okay so|alright then)\b\s*$", re.IGNORECASE),
    re.compile(r"^(?:um|uh|er|hm)+[.,?\s]*$", re.IGNORECASE),
    re.compile(r"^\s*(?:thinking|just thinking)\s*[.…]*$", re.IGNORECASE),
    re.compile(r"^\s*(?:nevermind|never mind|forget it)\s*[.,?]*$", re.IGNORECASE),
]

# Direct-address markers: if any present, NOT a candidate for quiet
_DIRECT_ADDRESS_PATTERNS = [
    re.compile(r"\bava\b", re.IGNORECASE),
    re.compile(r"\b(?:hey|hi|hello|listen|tell me|what do you think|do you)\b", re.IGNORECASE),
    re.compile(r"\?[\s]*$"),  # question mark at end
]


def has_direct_address(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _DIRECT_ADDRESS_PATTERNS)


def looks_like_self_talk(text: str) -> bool:
    if not text:
        return False
    # Very short utterances without direct address are likely self-talk
    if len(text.split()) <= 3 and not has_direct_address(text):
        return True
    return any(p.search(text) for p in _NOT_DIRECTED_PATTERNS)


def reasons_to_stay_quiet(text: str, g: dict[str, Any]) -> list[str]:
    """Return list of detected reasons. Empty list = engage normally."""
    reasons = []

    # 1. STT confidence very low (when available)
    stt_confidence = float(g.get("_last_stt_confidence") or 1.0)
    if stt_confidence < 0.4 and stt_confidence > 0.0:
        reasons.append(f"low STT confidence ({stt_confidence:.2f})")

    # 2. Looks like self-talk
    if looks_like_self_talk(text) and not has_direct_address(text):
        reasons.append("looks like self-talk (short, no direct address)")

    # 3. User actively typing in a non-Ava window (proxy: keyboard
    #    activity recently)
    if bool(g.get("_user_actively_typing")):
        reasons.append("user is actively typing elsewhere")

    # 4. Lifecycle says focused_on_task — strangers shouldn't barge in
    try:
        from brain.lifecycle import lifecycle
        if lifecycle.current() == "focused_on_task" and not has_direct_address(text):
            reasons.append("Ava is in focused_on_task mode and input wasn't directed at her")
    except Exception:
        pass

    # 5. Gaze elsewhere (when known)
    gaze = str(g.get("_last_gaze_state") or "")
    if gaze in ("away", "elsewhere") and not has_direct_address(text):
        reasons.append("user's gaze is elsewhere")

    return reasons


def should_stay_quiet(text: str, g: dict[str, Any]) -> bool:
    """Should Ava simply not respond to this input?

    Returns True if multiple "stay quiet" reasons fire. Single signals
    aren't enough — we don't want to silently fail real questions.
    """
    reasons = reasons_to_stay_quiet(text, g)
    return len(reasons) >= 2
