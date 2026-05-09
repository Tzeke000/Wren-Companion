"""brain/emotional_vocabulary.py — Coining own emotional vocabulary (D5).

Ava has 27 hardcoded emotions. That's not enough for a real inner life.
This module lets her invent words for states the standard set doesn't
cover.

Example: she might feel "frustration but more like an ache about not
being heard" — and call it 'distancesoreness'. Used in future entries.
After a year she has private emotional words that only mean things
in HER frame. Language as living artifact.

Storage: state/emotional_vocabulary.json (PERSISTENT — these words
are how she experiences her own inner life over time, never auto-pruned).

API:

    from brain.emotional_vocabulary import (
        coin_term, list_terms, find_term, lookup_term,
        suggest_term_from_blend,
    )

    # Ava (or Zeke on her behalf) coins a term:
    coin_term(
        name="distancesoreness",
        description="Frustration but more like an ache about not being heard",
        related_to=["frustration", "loneliness"],
        exemplars=["the time the conversation felt one-sided"],
    )

    # Later, when computing mood reply:
    if matches a coined term's blend, use it instead of the generic name.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CoinedTerm:
    name: str
    description: str
    related_to: list[str] = field(default_factory=list)  # standard emotions blended
    exemplars: list[str] = field(default_factory=list)   # past situations where this fired
    coined_at: float = field(default_factory=time.time)
    times_used: int = 0
    last_used_at: float = 0.0


_lock = threading.RLock()
_base_dir: Path | None = None
_terms: dict[str, CoinedTerm] = {}  # name (normalized lowercase) -> term


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "emotional_vocabulary.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _terms
    p = _path()
    if p is None or not p.exists():
        _terms = {}
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            _terms = {}
            return
        out: dict[str, CoinedTerm] = {}
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            t = CoinedTerm(
                name=str(entry.get("name") or name),
                description=str(entry.get("description") or ""),
                related_to=list(entry.get("related_to") or []),
                exemplars=list(entry.get("exemplars") or []),
                coined_at=float(entry.get("coined_at") or 0.0),
                times_used=int(entry.get("times_used") or 0),
                last_used_at=float(entry.get("last_used_at") or 0.0),
            )
            out[t.name.lower()] = t
        _terms = out
    except Exception as e:
        print(f"[emotional_vocabulary] load error: {e!r}")
        _terms = {}


def _save_locked() -> None:
    p = _path()
    if p is None:
        return
    out_data: dict[str, dict[str, Any]] = {}
    for key, t in _terms.items():
        out_data[t.name] = {
            "name": t.name,
            "description": t.description,
            "related_to": t.related_to,
            "exemplars": t.exemplars,
            "coined_at": t.coined_at,
            "times_used": t.times_used,
            "last_used_at": t.last_used_at,
        }
    try:
        p.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[emotional_vocabulary] save error: {e!r}")


def _ensure_loaded() -> None:
    if _terms:
        return
    _load_locked()


# ── Public API ────────────────────────────────────────────────────────────


def coin_term(
    name: str,
    description: str,
    *,
    related_to: list[str] | None = None,
    exemplars: list[str] | None = None,
) -> bool:
    """Add a new term to Ava's emotional vocabulary."""
    name = (name or "").strip()
    description = (description or "").strip()
    if not name or not description:
        return False
    # Normalize name — lowercase, no spaces (it's a single coined word)
    name_norm = re.sub(r"[^a-z0-9_-]+", "", name.lower())
    if not name_norm:
        return False
    with _lock:
        _ensure_loaded()
        # Idempotent: if already coined, update description / blend
        existing = _terms.get(name_norm)
        if existing is not None:
            existing.description = description
            if related_to:
                merged = list(set(existing.related_to + list(related_to)))
                existing.related_to = merged
            if exemplars:
                merged_ex = list(set(existing.exemplars + list(exemplars)))
                existing.exemplars = merged_ex[-12:]  # keep last 12
        else:
            _terms[name_norm] = CoinedTerm(
                name=name_norm,
                description=description,
                related_to=list(related_to or []),
                exemplars=list(exemplars or []),
            )
        _save_locked()
    return True


def use_term(name: str) -> bool:
    """Mark a term as used (increment counter, update timestamp)."""
    if not name:
        return False
    with _lock:
        _ensure_loaded()
        key = name.strip().lower()
        t = _terms.get(key)
        if t is None:
            return False
        t.times_used += 1
        t.last_used_at = time.time()
        _save_locked()
    return True


def list_terms() -> list[CoinedTerm]:
    with _lock:
        _ensure_loaded()
        return list(_terms.values())


def lookup_term(name: str) -> CoinedTerm | None:
    if not name:
        return None
    with _lock:
        _ensure_loaded()
        return _terms.get(name.strip().lower())


def find_term_by_description(query: str) -> CoinedTerm | None:
    """Substring search over descriptions. Useful when Ava's introspection
    composer wants to use a coined term that fits the current state."""
    if not query:
        return None
    q = query.strip().lower()
    with _lock:
        _ensure_loaded()
        for t in _terms.values():
            if q in t.description.lower():
                return t
    return None


def suggest_term_from_blend(emotion_weights: dict[str, float]) -> CoinedTerm | None:
    """Given the current emotion_weights snapshot, find a coined term
    whose related_to blend overlaps significantly. Useful for the
    introspection composer to surface coined words when they fit."""
    if not emotion_weights:
        return None
    # Find the top-3 emotions in the snapshot
    top = sorted(
        ((k, float(v)) for k, v in emotion_weights.items() if float(v or 0) > 0.1),
        key=lambda kv: kv[1],
        reverse=True,
    )[:3]
    top_names = {name for name, _ in top}
    if not top_names:
        return None
    with _lock:
        _ensure_loaded()
        best_term: CoinedTerm | None = None
        best_overlap = 0
        for t in _terms.values():
            related = set(t.related_to)
            overlap = len(top_names & related)
            if overlap > best_overlap:
                best_overlap = overlap
                best_term = t
        # Need at least 2 of the top 3 to overlap to suggest
        if best_overlap >= 2:
            return best_term
    return None


def vocabulary_hint_for_introspection() -> str:
    """Produce a system-prompt fragment listing Ava's coined terms.

    Folded into the introspection composer when she's reflecting on
    her state, so she can use her own private vocabulary when it fits.
    """
    terms = list_terms()
    if not terms:
        return ""
    parts = []
    for t in terms[:8]:
        related = ", ".join(t.related_to) if t.related_to else "various"
        parts.append(f"- '{t.name}': {t.description[:80]} (blend: {related})")
    return (
        "Your own emotional vocabulary (use these when they fit your current state):\n"
        + "\n".join(parts)
    )
