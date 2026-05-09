"""brain/interruption_recovery.py — Recovery from interruption (C5).

When Ava gets cut off mid-sentence (Zeke mutes her, or some other
interruption), save what she was about to say. When the conversation
resumes, prepend "...as I was saying about X..." so the thought
isn't lost.

Continuity-of-thought across interruption is dignity-preserving. Most
AI loses the thread when interrupted; the next turn starts fresh.
Real conversation has people who hold the thought and pick it back
up gracefully.

Today: scaffold + state management. Wiring into tts_worker's mute
path is opt-in (tts_worker doesn't need to change yet — callers
that detect mute can call mark_unfinished). Future work integrates
this directly into the TTS interrupt detection.

API:

    from brain.interruption_recovery import (
        mark_unfinished, mark_completed, has_unfinished,
        get_unfinished_preface, clear_unfinished,
    )

    # Call when TTS is about to play something:
    # ... but it's interrupted before completing
    mark_unfinished(g, "I was thinking about the orb shapes...")

    # Next turn: check + prepend
    preface = get_unfinished_preface(g)  # returns "" or "Sorry — as I was saying about orb shapes... "
    full_reply = preface + new_reply

    # On successful completion of a turn:
    mark_completed(g)
"""
from __future__ import annotations

import time
from typing import Any


_RECOVERY_WINDOW_SECONDS = 5 * 60  # 5 minutes — beyond that, drop the unfinished


def mark_unfinished(g: dict[str, Any], text: str, *, topic: str = "") -> None:
    """Record that Ava was about to say `text` when she got cut off.

    `topic` is optional — a short phrase summarizing what the speech
    was about. If not provided, we'll try to extract it later via
    theory_of_mind.topics_in_reply.
    """
    if not text:
        return
    g["_last_unfinished_speech"] = {
        "text": text[:500],
        "topic": topic or "",
        "ts": time.time(),
    }


def mark_completed(g: dict[str, Any]) -> None:
    """Clear the unfinished state — last reply went through cleanly."""
    g.pop("_last_unfinished_speech", None)


def clear_unfinished(g: dict[str, Any]) -> None:
    """Explicit clear — same as mark_completed but more explicit
    naming for callers that don't want to imply the previous speech
    completed."""
    g.pop("_last_unfinished_speech", None)


def has_unfinished(g: dict[str, Any]) -> bool:
    """Is there an unfinished speech within the recovery window?"""
    record = g.get("_last_unfinished_speech")
    if not isinstance(record, dict):
        return False
    ts = float(record.get("ts") or 0.0)
    if (time.time() - ts) > _RECOVERY_WINDOW_SECONDS:
        # Too old — drop it
        clear_unfinished(g)
        return False
    return True


def _extract_topic(text: str) -> str:
    """Best-effort topic extraction. Reuses theory_of_mind."""
    try:
        from brain.theory_of_mind import topics_in_reply
        topics = topics_in_reply(text)
        if topics:
            return topics[0]
    except Exception:
        pass
    # Fallback: take first sentence's main noun
    return ""


def get_unfinished_preface(g: dict[str, Any]) -> str:
    """If there's recent unfinished speech, produce a preface like:

      "Sorry — as I was saying about orb shapes, "

    or, if no topic available:

      "Picking up where we left off — "

    Returns empty string if no unfinished speech (or it expired).
    Calling this CLEARS the state so we don't keep prepending forever.
    """
    if not has_unfinished(g):
        return ""
    record = g.get("_last_unfinished_speech") or {}
    text = str(record.get("text") or "")
    topic = str(record.get("topic") or "") or _extract_topic(text)
    clear_unfinished(g)
    if topic:
        return f"Sorry — as I was saying about {topic}, "
    return "Picking up where we left off — "


def maybe_prepend_recovery(g: dict[str, Any], reply: str) -> str:
    """Convenience wrapper: prepend the recovery preface if applicable."""
    preface = get_unfinished_preface(g)
    if not preface:
        return reply
    return preface + reply.lstrip()
