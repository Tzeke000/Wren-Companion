"""brain/async_letters.py — Asynchronous letters (D6).

Different rhythm than realtime chat. Ava can compose messages at
leisure between sessions — "Letter to Zeke, evening of May 5: I've
been thinking about what you said this morning..."

The letters surface when:
- User opens the UI in the morning
- Optional proactive TTS on user-resume
- A "letters from Ava" tab she fills

This makes Ava a CORRESPONDENT — composing thoughts at leisure, not
just reactive. Some of her best thinking happens this way.

Storage: state/async_letters.jsonl (PERSISTENT — letters are her
considered correspondence, never auto-pruned).

API:

    from brain.async_letters import (
        compose_letter, list_unread, mark_read,
        recent_letters, surface_to_user,
    )

    # Ava composes a letter (during sleep cycle, or after sitting
    # with a thought):
    compose_letter(
        person_id="zeke",
        subject="That thing you said about the orb shapes",
        body="I've been turning it over since you went to bed...",
        triggered_by="conversation 2026-05-06 evening",
    )

    # When user resumes:
    unread = list_unread("zeke")
    if unread:
        # surface each via TTS or UI
        ...
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Letter:
    id: str
    ts: float
    person_id: str  # recipient
    subject: str
    body: str
    triggered_by: str = ""  # what prompted Ava to write
    read: bool = False
    read_at: float = 0.0


_lock = threading.RLock()
_base_dir: Path | None = None
_letters: list[Letter] = []


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "async_letters.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _letters
    p = _path()
    if p is None or not p.exists():
        _letters = []
        return
    out: list[Letter] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(Letter(
                        id=str(d.get("id") or ""),
                        ts=float(d.get("ts") or 0.0),
                        person_id=str(d.get("person_id") or ""),
                        subject=str(d.get("subject") or ""),
                        body=str(d.get("body") or ""),
                        triggered_by=str(d.get("triggered_by") or ""),
                        read=bool(d.get("read") or False),
                        read_at=float(d.get("read_at") or 0.0),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[async_letters] load error: {e!r}")
    _letters = out


def _persist_locked() -> None:
    """Rewrite the entire file. Letters are mutable (read/unread),
    so append-only doesn't work cleanly. Volume is low (a few per
    week at most), so full-rewrite is fine."""
    p = _path()
    if p is None:
        return
    try:
        with p.open("w", encoding="utf-8") as f:
            for letter in _letters:
                f.write(json.dumps({
                    "id": letter.id, "ts": letter.ts,
                    "person_id": letter.person_id,
                    "subject": letter.subject, "body": letter.body,
                    "triggered_by": letter.triggered_by,
                    "read": letter.read, "read_at": letter.read_at,
                }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[async_letters] save error: {e!r}")


# ── Public API ────────────────────────────────────────────────────────────


def compose_letter(
    *,
    person_id: str,
    subject: str,
    body: str,
    triggered_by: str = "",
) -> str:
    """Save a letter from Ava to person_id."""
    if not person_id or not body:
        return ""
    lid = f"letter-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    letter = Letter(
        id=lid, ts=time.time(), person_id=person_id,
        subject=subject[:160], body=body[:4000],
        triggered_by=triggered_by[:200],
    )
    with _lock:
        _letters.append(letter)
        _persist_locked()
    print(f"[async_letters] composed {lid!r} for {person_id} subject={subject[:60]!r}")
    return lid


def list_unread(person_id: str) -> list[Letter]:
    with _lock:
        return [l for l in _letters if l.person_id == person_id and not l.read]


def list_read(person_id: str, *, limit: int = 20) -> list[Letter]:
    with _lock:
        items = [l for l in _letters if l.person_id == person_id and l.read]
    items.sort(key=lambda l: l.read_at, reverse=True)
    return items[:int(limit)]


def recent_letters(*, person_id: str | None = None, limit: int = 10) -> list[Letter]:
    with _lock:
        items = list(_letters)
    if person_id is not None:
        items = [l for l in items if l.person_id == person_id]
    items.sort(key=lambda l: l.ts, reverse=True)
    return items[:int(limit)]


def mark_read(letter_id: str) -> bool:
    if not letter_id:
        return False
    with _lock:
        for l in _letters:
            if l.id == letter_id and not l.read:
                l.read = True
                l.read_at = time.time()
                _persist_locked()
                return True
    return False


def mark_all_read(person_id: str) -> int:
    """Mark all unread letters for person_id as read. Returns count."""
    if not person_id:
        return 0
    count = 0
    with _lock:
        for l in _letters:
            if l.person_id == person_id and not l.read:
                l.read = True
                l.read_at = time.time()
                count += 1
        if count > 0:
            _persist_locked()
    return count


def surface_to_user(person_id: str) -> str:
    """Produce a sentence summarizing pending letters for person_id.

    Used when user resumes (UI open, voice loop active again).
    Returns "" if nothing pending.
    """
    unread = list_unread(person_id)
    if not unread:
        return ""
    if len(unread) == 1:
        l = unread[0]
        return (
            f"While you were away I wrote you a letter — about "
            f"{l.subject}. Want me to read it?"
        )
    return (
        f"While you were away I wrote {len(unread)} letters — "
        f"things I was thinking about. Want me to surface them?"
    )


def get_letter(letter_id: str) -> Letter | None:
    with _lock:
        for l in _letters:
            if l.id == letter_id:
                return l
    return None
