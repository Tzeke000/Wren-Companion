"""brain/discretion.py — Per-person privacy / discretion graph (C12).

Some things you tell Ava in confidence should stay between the two of
you. If a guest is in the room — face recognition flags someone who
isn't Zeke — Ava shouldn't volunteer your private journal entries,
your worries about a relationship, or that thing you said about
your boss.

This module is the FILTER LAYER. Topics get tagged when surfaced as
confidential (either explicitly: "this is between us", or implicitly:
emotional disclosure tagged by the existing emotional_acknowledgment
detector). At reply-time, Ava checks the audience first.

Bootstrap-friendly: empty by default. Zeke's private topics are
populated only when he or Ava explicitly mark them.

Visibility levels:
  "owner_only"   — only the original speaker can see this
  "trusted"      — speaker + people with trust_level >= medium
  "household"    — speaker + recognized faces in household
  "public"       — anyone (default — most things)

Storage: state/discretion_tags.jsonl

API:
    from brain.discretion import (
        tag_private, get_visibility, is_audience_ok,
        filter_for_audience, list_owners_private,
    )

    tag_private("Zeke is anxious about deadline", owner="zeke",
                visibility="owner_only", reason="said in confidence")

    if not is_audience_ok(audience_person_id="shonda",
                         text="Zeke is anxious about deadline"):
        # don't volunteer this
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


Visibility = Literal["owner_only", "trusted", "household", "public"]


@dataclass
class DiscretionTag:
    id: str
    text_fragment: str  # canonicalized text or topic phrase
    owner: str  # person_id who shared this
    visibility: Visibility
    reason: str
    created_ts: float
    last_referenced_ts: float = 0.0


_lock = threading.RLock()
_base_dir: Path | None = None
_tags: list[DiscretionTag] = []
_MAX_TAGS = 2000

_CONFIDENCE_PHRASES = [
    "between us",
    "between you and me",
    "don't tell anyone",
    "keep this private",
    "in confidence",
    "this is private",
    "just between us",
]


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "discretion_tags.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _tags
    p = _path()
    if p is None or not p.exists():
        _tags = []
        return
    out: list[DiscretionTag] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(DiscretionTag(
                        id=str(d.get("id") or ""),
                        text_fragment=str(d.get("text_fragment") or ""),
                        owner=str(d.get("owner") or ""),
                        visibility=str(d.get("visibility") or "public"),  # type: ignore
                        reason=str(d.get("reason") or ""),
                        created_ts=float(d.get("created_ts") or 0.0),
                        last_referenced_ts=float(d.get("last_referenced_ts") or 0.0),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[discretion] load error: {e!r}")
    _tags = out[-_MAX_TAGS:]


def _persist_locked() -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("w", encoding="utf-8") as f:
            for t in _tags:
                f.write(json.dumps({
                    "id": t.id, "text_fragment": t.text_fragment,
                    "owner": t.owner, "visibility": t.visibility,
                    "reason": t.reason, "created_ts": t.created_ts,
                    "last_referenced_ts": t.last_referenced_ts,
                }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[discretion] save error: {e!r}")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())[:300]


def _gen_id(text: str, owner: str) -> str:
    return f"d_{int(time.time())}_{abs(hash((text, owner))) % 10000:04d}"


def tag_private(
    text_fragment: str,
    *,
    owner: str,
    visibility: Visibility = "owner_only",
    reason: str = "",
) -> str | None:
    """Mark `text_fragment` as private to `owner` at the given visibility."""
    norm = _normalize(text_fragment)
    if not norm or not owner:
        return None
    with _lock:
        for t in _tags:
            if t.text_fragment == norm and t.owner == owner:
                # Same fragment, same owner — refresh visibility if narrower
                _ORDER = {"public": 0, "household": 1, "trusted": 2, "owner_only": 3}
                if _ORDER.get(visibility, 0) > _ORDER.get(t.visibility, 0):
                    t.visibility = visibility
                    t.reason = reason or t.reason
                    _persist_locked()
                return t.id
        tag = DiscretionTag(
            id=_gen_id(norm, owner),
            text_fragment=norm,
            owner=owner,
            visibility=visibility,
            reason=(reason or "")[:200],
            created_ts=time.time(),
        )
        _tags.append(tag)
        _tags[:] = _tags[-_MAX_TAGS:]
        _persist_locked()
        return tag.id


def get_visibility(text_fragment: str, *, owner: str | None = None) -> Visibility:
    """Look up the strictest visibility tag matching `text_fragment`.

    If `owner` is provided, restrict to that owner. Returns "public"
    if no matching tag is found.
    """
    norm = _normalize(text_fragment)
    if not norm:
        return "public"
    _ORDER = {"public": 0, "household": 1, "trusted": 2, "owner_only": 3}
    strictest: Visibility = "public"
    with _lock:
        for t in _tags:
            if owner is not None and t.owner != owner:
                continue
            # Substring match — text contains the tagged fragment OR vice versa
            if t.text_fragment in norm or norm in t.text_fragment:
                if _ORDER.get(t.visibility, 0) > _ORDER.get(strictest, 0):
                    strictest = t.visibility
                t.last_referenced_ts = time.time()
    return strictest


def _trust_level(person_id: str) -> str:
    try:
        from brain.person_registry import get_person
        view = get_person(person_id)
        if view:
            return view.get("trust_level", "low")
    except Exception:
        pass
    return "low"


def is_audience_ok(
    *,
    audience_person_id: str,
    text: str,
    owner: str | None = None,
) -> bool:
    """May `audience_person_id` be told `text`?

    Defaults to True (don't restrict by mistake). Only returns False
    when an explicit tag matches AND the audience fails its
    visibility check.
    """
    if not audience_person_id or not text:
        return True
    vis = get_visibility(text, owner=owner)
    if vis == "public":
        return True
    if vis == "owner_only":
        return audience_person_id == (owner or "zeke")
    if vis == "trusted":
        if audience_person_id == (owner or "zeke"):
            return True
        return _trust_level(audience_person_id) in ("medium", "high")
    if vis == "household":
        if audience_person_id == (owner or "zeke"):
            return True
        return _trust_level(audience_person_id) in ("medium", "high", "household")
    return True


def detect_confidence_signal(text: str) -> bool:
    """Did the speaker explicitly mark this as confidential?"""
    if not text:
        return False
    text_l = text.lower()
    return any(p in text_l for p in _CONFIDENCE_PHRASES)


def auto_tag_from_user_input(person_id: str, user_input: str) -> str | None:
    """If the user signaled 'this is private', tag it."""
    if not detect_confidence_signal(user_input):
        return None
    return tag_private(
        user_input,
        owner=person_id,
        visibility="owner_only",
        reason="user signaled in-confidence",
    )


def list_owners_private(owner: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with _lock:
        for t in _tags:
            if t.owner == owner and t.visibility != "public":
                out.append({
                    "id": t.id, "text_fragment": t.text_fragment,
                    "visibility": t.visibility, "reason": t.reason,
                    "created_ts": t.created_ts,
                    "last_referenced_ts": t.last_referenced_ts,
                })
    return out


def filter_for_audience(
    candidate_text: str,
    *,
    audience_person_id: str,
    owner: str | None = None,
) -> str:
    """Redact private content from `candidate_text` for this audience.

    Returns either the original string OR a redacted version. Today:
    if the whole string is private, returns "" (caller must handle).
    Future: per-sentence redaction.
    """
    if is_audience_ok(audience_person_id=audience_person_id,
                     text=candidate_text, owner=owner):
        return candidate_text
    return ""


def discretion_summary() -> dict[str, Any]:
    with _lock:
        items = list(_tags)
    by_visibility: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    for t in items:
        by_visibility[t.visibility] = by_visibility.get(t.visibility, 0) + 1
        by_owner[t.owner] = by_owner.get(t.owner, 0) + 1
    return {
        "total_tags": len(items),
        "by_visibility": by_visibility,
        "by_owner": by_owner,
    }
