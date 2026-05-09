"""brain/mirix_taxonomy.py — MIRIX six-type memory tagging.

Per the 2026-05-07 night research synthesis, the MIRIX architecture
(Yang et al., arXiv:2507.07957, July 2025) proposes six functional
categories for agent memory. Tagging Ava's memory entries with these
types lets future retrieval/routing pick the right kind of memory
for the question at hand instead of treating everything as one
undifferentiated bucket.

Today this is **additive only**: every existing memory write gets a
type tag based on its existing category. Behavior is unchanged. The
tags become useful when retrieval starts filtering by type — that's
a follow-up.

The six types:

  CORE — persistent identity-anchored facts: who Zeke is, who Ava is,
         names, relationships, stable preferences, profile entries.

  EPISODIC — specific events with timestamps: "we had a conversation
             about X on date Y", camera observations, conversation
             excerpts that matter as events.

  SEMANTIC — concepts, definitions, factual knowledge not tied to a
             specific event: "polar bears are arctic mammals."

  PROCEDURAL — how-to-do-things, learned skills, step sequences:
               compound voice commands, action skills, learned
               recipes for accomplishing something.

  RESOURCE — files, documents, images, media that the user has
             shared or that are referenced. Pointers + metadata,
             not the content itself.

  KNOWLEDGE_VAULT — verbatim-required sensitive facts that must NEVER
                    be summarized: phone numbers, addresses, account
                    numbers, prescription names, exact quotes from
                    Zeke that he wants preserved word-for-word.

API:

    from brain.mirix_taxonomy import (
        MemoryType, classify_by_category, classify_by_content,
        classify_memory,
    )

    mtype = classify_memory(
        category="camera_event",
        content="Zeke walked into the room at 7:15pm",
        tags=["camera", "snapshot"],
    )
    # → MemoryType.EPISODIC
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Iterable


class MemoryType(str, Enum):
    """Six MIRIX memory types. str-subclass so they serialize cleanly."""
    CORE = "core"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    RESOURCE = "resource"
    KNOWLEDGE_VAULT = "knowledge_vault"


# Mapping of Ava's existing category strings → MIRIX types.
# Categories not in this map fall through to content-based classification,
# then to SEMANTIC as the default.
_CATEGORY_TO_TYPE: dict[str, MemoryType] = {
    # Identity / profile / relational state
    "profile": MemoryType.CORE,
    "preference": MemoryType.CORE,
    "impression": MemoryType.CORE,
    "camera_identity": MemoryType.CORE,
    "trust": MemoryType.CORE,

    # Events
    "camera_event": MemoryType.EPISODIC,
    "promoted_reflection": MemoryType.EPISODIC,
    "full_user_message": MemoryType.EPISODIC,
    "conversation_event": MemoryType.EPISODIC,

    # Knowledge / facts
    "general": MemoryType.SEMANTIC,
    "fact": MemoryType.SEMANTIC,
    "concept": MemoryType.SEMANTIC,

    # Procedural
    "skill": MemoryType.PROCEDURAL,
    "compound_action": MemoryType.PROCEDURAL,
    "voice_command": MemoryType.PROCEDURAL,

    # Resources
    "file": MemoryType.RESOURCE,
    "image": MemoryType.RESOURCE,
    "document": MemoryType.RESOURCE,
    "screenshot": MemoryType.RESOURCE,

    # Verbatim-required
    "verbatim": MemoryType.KNOWLEDGE_VAULT,
    "credential": MemoryType.KNOWLEDGE_VAULT,
    "exact_quote": MemoryType.KNOWLEDGE_VAULT,
    "contact": MemoryType.KNOWLEDGE_VAULT,
}


# Content-pattern signals when category doesn't decisively map.
# Each pattern → MemoryType. First match wins.
_CONTENT_PATTERNS: list[tuple[re.Pattern[str], MemoryType]] = [
    # Phone numbers, addresses, account-shaped strings → vault
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), MemoryType.KNOWLEDGE_VAULT),  # phone
    (re.compile(r"\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:St|Ave|Blvd|Rd|Ln|Dr|Way|Ct)\b"), MemoryType.KNOWLEDGE_VAULT),  # street address
    (re.compile(r"\b[A-Z0-9]{6,}\b"), MemoryType.KNOWLEDGE_VAULT),  # account-like ALL-CAPS strings (loose)

    # File / image references → resource
    (re.compile(r"\b\S+\.(?:pdf|png|jpg|jpeg|gif|mp4|mov|wav|mp3|txt|md|docx?|xlsx?)\b", re.IGNORECASE), MemoryType.RESOURCE),
    (re.compile(r"\bC:\\|D:\\|/home/|/Users/", re.IGNORECASE), MemoryType.RESOURCE),

    # Procedural language
    (re.compile(r"\b(?:step \d|first[,]?\s|then[,]?\s|finally[,]?\s|how to)\b", re.IGNORECASE), MemoryType.PROCEDURAL),

    # Episode-shaped (timestamps, "today/yesterday", events)
    (re.compile(r"\b(?:today|yesterday|tonight|this morning|just now|earlier)\b", re.IGNORECASE), MemoryType.EPISODIC),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), MemoryType.EPISODIC),
    (re.compile(r"\b(?:happened|occurred|took place)\b", re.IGNORECASE), MemoryType.EPISODIC),

    # Identity-shaped (preference, "I am", "you are")
    (re.compile(r"\b(?:I (?:am|like|prefer|hate|love)|you (?:are|like|prefer))\b", re.IGNORECASE), MemoryType.CORE),
]


def classify_by_category(category: str | None) -> MemoryType | None:
    """Return MemoryType from category string, or None if unknown category."""
    if not category:
        return None
    return _CATEGORY_TO_TYPE.get(str(category).strip().lower())


def classify_by_content(content: str | None) -> MemoryType | None:
    """Return MemoryType from content patterns, or None if no pattern matches."""
    if not content:
        return None
    for pat, mtype in _CONTENT_PATTERNS:
        if pat.search(str(content)):
            return mtype
    return None


def classify_by_tags(tags: Iterable[str] | None) -> MemoryType | None:
    """Return MemoryType from tags. Specific tag-name conventions:

    - 'verbatim' / 'sensitive' / 'phone' / 'address' → KNOWLEDGE_VAULT
    - 'procedure' / 'skill' / 'recipe' → PROCEDURAL
    - 'file' / 'image' / 'document' → RESOURCE
    - 'preference' / 'identity' / 'profile' → CORE
    - 'episode' / 'conversation' / 'event' → EPISODIC
    - 'concept' / 'fact' / 'knowledge' → SEMANTIC
    """
    if not tags:
        return None
    tag_set = {str(t).strip().lower() for t in tags if t}
    if tag_set & {"verbatim", "sensitive", "phone", "address", "credential", "ssn", "exact_quote"}:
        return MemoryType.KNOWLEDGE_VAULT
    if tag_set & {"procedure", "skill", "recipe", "how_to", "compound_action"}:
        return MemoryType.PROCEDURAL
    if tag_set & {"file", "image", "document", "screenshot", "media"}:
        return MemoryType.RESOURCE
    if tag_set & {"preference", "identity", "profile", "trust", "impression"}:
        return MemoryType.CORE
    if tag_set & {"episode", "conversation", "event", "snapshot", "occurrence"}:
        return MemoryType.EPISODIC
    if tag_set & {"concept", "fact", "knowledge", "definition", "general"}:
        return MemoryType.SEMANTIC
    return None


def classify_memory(
    *,
    category: str | None = None,
    content: str | None = None,
    tags: Iterable[str] | None = None,
    default: MemoryType = MemoryType.SEMANTIC,
) -> MemoryType:
    """Classify a memory entry into one of the six MIRIX types.

    Resolution order:
      1. Category mapping (most authoritative)
      2. Tags-based hints
      3. Content patterns
      4. Default (SEMANTIC — generic factual)

    Caller passes whichever fields they have. Returns a MemoryType
    enum value safe to store as a string.
    """
    by_cat = classify_by_category(category)
    if by_cat is not None:
        return by_cat
    by_tags = classify_by_tags(tags)
    if by_tags is not None:
        return by_tags
    by_content = classify_by_content(content)
    if by_content is not None:
        return by_content
    return default


def all_types() -> list[str]:
    """List all six type strings, useful for filter UIs / queries."""
    return [t.value for t in MemoryType]


def description(mtype: MemoryType | str) -> str:
    """Human-readable description of a memory type."""
    if isinstance(mtype, str):
        try:
            mtype = MemoryType(mtype)
        except ValueError:
            return f"unknown memory type: {mtype}"
    return {
        MemoryType.CORE: "Persistent identity-anchored facts (profile, preferences, relationships).",
        MemoryType.EPISODIC: "Specific events with timestamps.",
        MemoryType.SEMANTIC: "Concepts and factual knowledge not tied to a specific event.",
        MemoryType.PROCEDURAL: "How-to-do-things, learned skills, step sequences.",
        MemoryType.RESOURCE: "Files, documents, images — pointers + metadata.",
        MemoryType.KNOWLEDGE_VAULT: "Verbatim-required sensitive facts (never summarize).",
    }[mtype]
