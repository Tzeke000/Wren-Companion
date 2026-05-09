"""brain/shared_lexicon.py — Inside jokes / private references (C11).

Patterns Ava and a specific person develop together. Words you invented
together for shared experiences. Like couples and old friends accumulate
language only they understand. Stored as Person Registry's
shared_lexicon dict — this module is the EXTRACTION + RECALL layer.

Examples (none seeded; emerges from real conversation):
- "the silver thing" — a specific concept Zeke and Ava landed on
  during a discussion about UI
- "drift mode" — Ava's name for her low-attention idle state
- "the polar bear question" — shorthand for "knowledge query that
  takes a long time"

How it works:
- When the user defines a shared term explicitly ("let's call X
  the Y"), record it.
- When repeated phrases pattern across conversations, surface them
  as candidates for shared lexicon.
- When the user uses a recorded shared term, recognize it and
  ground the reply in the shared meaning.

API:

    from brain.shared_lexicon import (
        learn_from_definition, recall_meaning, list_shared_terms,
        contains_shared_term,
    )

    # User says "Let's call the introspection thing 'drift mode'":
    learn_from_definition(person_id, "drift mode", "the introspection thing")

    # Later: user mentions "drift mode":
    meaning = recall_meaning(person_id, "drift mode")
    # -> "the introspection thing"

    # Or scan for any shared term in the input:
    found = contains_shared_term(person_id, "are you in drift mode?")
    # -> [("drift mode", "the introspection thing")]
"""
from __future__ import annotations

import re
from typing import Any


# ── Detection patterns ────────────────────────────────────────────────────


# "let's call X (the) Y" / "I call X Y" / "X means Y between us"
_DEFINITION_PATTERNS = [
    # "let's call X Y"
    re.compile(
        r"\b(?:let'?s|we can|we should|we'?ll|i'?ll|i)\s+call\s+"
        r"(?:it\s+|that\s+|this\s+|these\s+|the\s+)?"
        r"['\"](?P<term>[^'\"]{2,40})['\"]?",
        re.IGNORECASE,
    ),
    # "let's just call this Y"
    re.compile(
        r"\bcall\s+(?:it|this|that|the\s+\w+)\s+['\"]?(?P<term>[a-z][a-z0-9 _-]{2,40})['\"]?",
        re.IGNORECASE,
    ),
    # "by 'X' I mean Y" / "'X' is what we call Y"
    re.compile(
        r"['\"](?P<term>[^'\"]{2,40})['\"][\s,]+(?:means|stands for|is (?:our|my|your) (?:name|word))",
        re.IGNORECASE,
    ),
]


def detect_shared_term_definition(text: str) -> tuple[str, str] | None:
    """Detect when the user is defining a shared term.

    Returns (term, meaning) if a definition pattern matches. Meaning
    extraction is loose — for now we capture the term and use the
    surrounding sentence as the meaning. Future versions could use
    LLM extraction for cleaner meaning capture.
    """
    if not text:
        return None
    for pat in _DEFINITION_PATTERNS:
        m = pat.search(text)
        if m is None:
            continue
        try:
            term = m.group("term").strip().lower()
        except (IndexError, AttributeError):
            continue
        if not term or len(term) < 2:
            continue
        # Meaning: the rest of the sentence containing the term.
        # Lightweight — better extraction is a future enhancement.
        meaning = text.strip()
        return (term, meaning[:200])
    return None


# ── API ───────────────────────────────────────────────────────────────────


def learn_from_definition(person_id: str, term: str, meaning: str) -> bool:
    """Explicitly add a term + meaning to the shared lexicon."""
    try:
        from brain.person_registry import registry
        registry.add_to_shared_lexicon(person_id, term, meaning)
        return True
    except Exception as e:
        print(f"[shared_lexicon] learn_from_definition error: {e!r}")
        return False


def learn_from_text(person_id: str, text: str) -> tuple[str, str] | None:
    """Scan `text` for an explicit definition pattern; if found, persist
    + return (term, meaning). Otherwise None.

    Called from the reply path after each turn so spontaneous
    definitions get captured.
    """
    detection = detect_shared_term_definition(text)
    if detection is None:
        return None
    term, meaning = detection
    if learn_from_definition(person_id, term, meaning):
        return (term, meaning)
    return None


def recall_meaning(person_id: str, term: str) -> str | None:
    """Look up the meaning of a shared term."""
    try:
        from brain.person_registry import registry
        lex = registry.get_shared_lexicon(person_id)
        return lex.get(str(term or "").strip().lower())
    except Exception:
        return None


def list_shared_terms(person_id: str) -> dict[str, str]:
    """All shared terms with this person."""
    try:
        from brain.person_registry import registry
        return registry.get_shared_lexicon(person_id)
    except Exception:
        return {}


def contains_shared_term(person_id: str, text: str) -> list[tuple[str, str]]:
    """Find any shared terms used in `text`. Returns list of
    (term, meaning) for terms that appear.
    """
    if not text:
        return []
    lex = list_shared_terms(person_id)
    if not lex:
        return []
    text_lower = text.lower()
    out = []
    for term, meaning in lex.items():
        if term in text_lower:
            out.append((term, meaning))
    return out


def shared_lexicon_hint(person_id: str) -> str:
    """Produce a prompt-context string with the shared lexicon. Fold
    into system prompts so Ava grounds replies in the shared meanings.

    Empty string if no shared terms.
    """
    lex = list_shared_terms(person_id)
    if not lex:
        return ""
    parts = [f"- '{term}': {meaning[:80]}" for term, meaning in list(lex.items())[:8]]
    return (
        "Shared terms with this person (use these meanings if they come up):\n"
        + "\n".join(parts)
    )
