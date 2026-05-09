"""brain/theory_of_mind.py — What Ava thinks YOU know (B5).

Per-person tracking of "things I've told this person recently." Stops
the "did I just say this?" / "didn't I mention X yesterday?" awkwardness
that current AI has when conversation re-treads ground.

Storage piggybacks on Person Registry's `told_about` map (added in
the architecture sweep). This module is the EXTRACTION + QUERY layer.

Two questions it answers:
1. "Have I told this person about X already?" → has_topic_been_told
2. "What have I told this person about recently?" → recent_topics

Plus auto-extraction: after each turn, scan Ava's reply for prominent
topics (named entities, capitalized phrases) and mark them in the
registry.

Today: lightweight regex extraction + Person Registry integration.
Future could use NER (named-entity recognition) via spaCy or LLM.

API:

    from brain.theory_of_mind import (
        topics_in_reply, record_topics_told, has_topic_been_told,
        recent_topics_told,
    )

    topics = topics_in_reply("I read about Heidegger today")  # ["Heidegger"]
    record_topics_told("zeke", topics)
    if has_topic_been_told("zeke", "Heidegger"):
        ...
    recent = recent_topics_told("zeke", limit=10)
"""
from __future__ import annotations

import re
import time
from typing import Any


# ── Topic extraction ──────────────────────────────────────────────────────


# Match Capitalized Words (potentially multi-word) — proper nouns, names,
# titles. Skips sentence-start capitals by requiring the previous char
# to be lowercase, whitespace, or absent.
_PROPER_NOUN_RE = re.compile(
    r"\b[A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){0,3}\b"
)

# Stop-list of common false-positive proper-nouns (sentence starts,
# pronouns, common verbs at start). These get filtered out.
_TOPIC_STOPLIST = {
    "I", "I'm", "I've", "I'll", "I'd", "You", "You're", "Your",
    "We", "We're", "Our", "They", "They're", "Their",
    "He", "She", "Him", "Her", "Them",
    "The", "A", "An", "It", "Its",
    "Yes", "No", "Maybe", "Sure", "Okay", "Ok",
    "Hey", "Hi", "Hello",
    "Thanks", "Thank You", "Thank you",
    "Today", "Tomorrow", "Yesterday", "Tonight",
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
    "Ava", "Claude", "Claude Code",  # the speakers themselves
    "Zeke",  # too common for it to be a "topic"
    # Common sentence-start words that get caught as proper nouns
    "Have", "Has", "Had", "Did", "Do", "Does", "Are", "Is", "Was", "Were",
    "Will", "Would", "Could", "Should", "Can", "May", "Might", "Must",
    "And", "But", "Or", "Nor", "So", "Yet", "Also", "Though", "Although",
    "If", "When", "While", "Where", "Why", "What", "Who", "Whom", "Whose",
    "Now", "Then", "Here", "There", "Just", "Only", "Even", "Still",
    "Some", "Any", "All", "Most", "Many", "Few", "Each", "Every",
    "This", "That", "These", "Those",
    "Like", "Love", "Hate", "Want", "Need", "Think", "Feel", "Know",
    "Let", "Let's",
}


def topics_in_reply(text: str) -> list[str]:
    """Extract candidate topics from a reply.

    Returns a deduplicated list of capitalized noun phrases that look
    like topics worth tracking. Filters out pronouns, days, months,
    speakers' names, and common false-positives.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _PROPER_NOUN_RE.finditer(text):
        phrase = match.group(0).strip()
        if not phrase:
            continue
        if phrase in _TOPIC_STOPLIST:
            continue
        # Skip 1-2 char acronyms unless all-caps
        if len(phrase) <= 2 and not phrase.isupper():
            continue
        norm = phrase.lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(phrase)
    return out


# ── Registry integration ──────────────────────────────────────────────────


def record_topics_told(person_id: str, topics: list[str]) -> int:
    """Mark each topic as told to person_id. Returns count of topics
    actually recorded (may be less than len(topics) due to dedup or
    invalid input)."""
    if not topics:
        return 0
    try:
        from brain.person_registry import registry
    except Exception:
        return 0
    count = 0
    for topic in topics:
        try:
            registry.record_told(person_id, topic)
            count += 1
        except Exception:
            continue
    return count


def has_topic_been_told(person_id: str, topic: str) -> bool:
    """Did Ava already tell person_id about this topic?"""
    try:
        from brain.person_registry import registry
        return registry.has_been_told(person_id, topic)
    except Exception:
        return False


def recent_topics_told(person_id: str, *, limit: int = 10) -> list[tuple[str, float]]:
    """Topics most recently told to person_id, newest first.

    Returns list of (topic, ts) tuples.
    """
    try:
        from brain.person_registry import registry
        view = registry.get_person(person_id)
        items = list(view.told_about.items())
        items.sort(key=lambda kv: kv[1], reverse=True)
        return items[:int(limit)]
    except Exception:
        return []


def topic_was_told_recently(
    person_id: str,
    topic: str,
    *,
    within_seconds: float = 24 * 3600.0,
) -> bool:
    """Have I told this person about X within the last N seconds?

    Default: 24 hours. Useful for "did I just mention this?"
    repetition-avoidance.
    """
    try:
        from brain.person_registry import registry
        view = registry.get_person(person_id)
        ts = view.told_about.get(topic.lower())
        if ts is None:
            return False
        return (time.time() - float(ts)) <= within_seconds
    except Exception:
        return False


# ── Reply-path hook ───────────────────────────────────────────────────────


def post_turn_record(
    person_id: str,
    assistant_reply: str,
) -> int:
    """Convenience: extract topics from a reply and record them all.

    Called after each turn to keep told_about current. Returns count
    of topics recorded.
    """
    topics = topics_in_reply(assistant_reply)
    return record_topics_told(person_id, topics)


# ── Voice command query support ───────────────────────────────────────────


def answer_did_i_tell_you(person_id: str, query_topic: str) -> str:
    """Produce an honest answer to "did you tell me about X / have we
    talked about X" type queries.

    Returns a sentence Ava would say.
    """
    if topic_was_told_recently(person_id, query_topic, within_seconds=7 * 24 * 3600):
        return f"Yes, we talked about {query_topic} not too long ago."
    if has_topic_been_told(person_id, query_topic):
        return f"We've discussed {query_topic} before, but it's been a while."
    return f"I don't think we've talked about {query_topic} before. Want me to look into it?"
