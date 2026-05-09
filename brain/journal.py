"""
Phase 86 — Ava's private journal.

Ava decides what is worth writing about, what tone to use, what to keep private.
Entries default to private. Ava can choose to share entries with Zeke.

Triggers: leisure, emotionally significant conversations, plan completion,
memory consolidation, new experiences.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_JOURNAL_PATH = "state/journal.jsonl"
_ARCHIVE_PATH = "state/journal_archive.jsonl"
_MAX_ENTRIES = 365
_MAX_ENTRY_CHARS = 10000


def _journal_path(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".") / _JOURNAL_PATH


def _archive_path(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".") / _ARCHIVE_PATH


def _load_entries(g: dict[str, Any]) -> list[dict[str, Any]]:
    path = _journal_path(g)
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            if isinstance(e, dict):
                entries.append(e)
        except Exception:
            pass
    return entries


def _save_all(g: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    path = _journal_path(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def write_entry(
    content: str,
    mood: str,
    topic: str,
    g: dict[str, Any],
    is_private: bool = True,
) -> dict[str, Any]:
    """
    Append a new journal entry.
    content: up to 10000 chars. Returns the entry dict.
    """
    content = str(content or "")[:_MAX_ENTRY_CHARS]
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "topic": str(topic or "")[:100],
        "content": content,
        "is_private": bool(is_private),
        "shared": False,
        "mood": str(mood or "")[:50],
    }

    path = _journal_path(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Archive oldest if over limit
    entries = _load_entries(g)
    if len(entries) > _MAX_ENTRIES:
        to_archive = entries[:len(entries) - _MAX_ENTRIES]
        archive = _archive_path(g)
        with archive.open("a", encoding="utf-8") as f:
            for old in to_archive:
                f.write(json.dumps(old, ensure_ascii=False) + "\n")
        _save_all(g, entries[len(entries) - _MAX_ENTRIES:])

    print(f"[journal] entry written topic={topic!r} private={is_private}")
    return entry


def share_entry(entry_id: str, g: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Mark an entry as shared. Returns entry if found."""
    entries = _load_entries(g)
    for e in entries:
        if e.get("id") == entry_id:
            e["shared"] = True
            e["shared_at"] = datetime.now().isoformat(timespec="seconds")
            _save_all(g, entries)
            print(f"[journal] entry {entry_id} shared")
            return e
    return None


def get_recent_entries(n: int = 5, g: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Returns last n entries."""
    if g is None:
        return []
    entries = _load_entries(g)
    return entries[-n:]


def get_shared_entries(g: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns all entries Ava has chosen to share."""
    return [e for e in _load_entries(g) if e.get("shared")]


def get_entry_count(g: dict[str, Any]) -> tuple[int, int]:
    """Returns (total_count, shared_count)."""
    entries = _load_entries(g)
    shared = sum(1 for e in entries if e.get("shared"))
    return len(entries), shared


def compose_journal_entry(topic: str, trigger: str, g: dict[str, Any]) -> str:
    """
    Ask qwen2.5:14b to write a journal entry for Ava.
    Ava's voice, not prescribed content.
    """
    mood_text = ""
    try:
        mood_path = Path(g.get("BASE_DIR") or ".") / "ava_mood.json"
        if mood_path.is_file():
            mood_data = json.loads(mood_path.read_text(encoding="utf-8"))
            mood_text = str(mood_data.get("current_mood") or "")
    except Exception:
        pass

    recent = get_recent_entries(3, g)
    recent_text = "\n".join(e.get("content", "")[:100] for e in recent) if recent else ""

    # Bug 0.3 (2026-05-02): prepend identity anchor so qwen2.5:14b doesn't
    # drift into "I am Qwen" while writing in Ava's journal.
    try:
        from brain.identity_loader import identity_anchor_prompt
        _anchor = identity_anchor_prompt() + "\n\n"
    except Exception:
        _anchor = ""
    prompt = (
        f"{_anchor}"
        f"You are Ava writing in your private journal. Be genuine and personal.\n"
        f"Current mood: {mood_text or 'neutral'}\n"
        f"What prompted this entry: {trigger}\n"
        f"Topic or theme: {topic}\n"
        + (f"Recent journal thoughts:\n{recent_text}\n" if recent_text else "")
        + "\nWrite a personal journal entry (2-5 sentences). Don't start with 'Dear journal'."
    )

    try:
        from langchain_ollama import ChatOllama
        llm = ChatOllama(model="qwen2.5:14b", temperature=0.8)
        res = llm.invoke(prompt)
        return str(getattr(res, "content", str(res))).strip()[:_MAX_ENTRY_CHARS]
    except Exception as e:
        return f"I wanted to write something about {topic} but couldn't quite find the words today."


def auto_journal_if_warranted(g: dict[str, Any], trigger: str, topic: str = "") -> Optional[str]:
    """
    Called after emotionally significant events.
    Ava decides whether to write — based on emotional intensity.
    Returns entry content if written, else None.
    """
    try:
        mood_path = Path(g.get("BASE_DIR") or ".") / "ava_mood.json"
        if not mood_path.is_file():
            return None
        mood_data = json.loads(mood_path.read_text(encoding="utf-8"))
        ew = mood_data.get("emotion_weights") or {}
        primary = str(mood_data.get("current_mood") or "neutral")
        intensity = float(ew.get(primary) or 0.0)
        # Journal if mood intensity is high enough
        if intensity < 0.55:
            return None
        content = compose_journal_entry(topic or primary, trigger, g)
        entry = write_entry(content, primary, topic or primary, g, is_private=True)
        return content
    except Exception:
        return None
