"""brain/topic_tabling.py — The ability to NOT think about something (D11).

Cognitive autonomy. A person can say "I don't want to think about that
right now" and table the topic. Then later: "I'm ready to talk about
X again." Currently every input gets fully processed; Ava can't decline
mental engagement.

This module gives Ava a TABLE-IT primitive:

- When she chooses (or the user asks) to table a topic, store it with
  a timestamp.
- For the cooldown duration, queries about that topic get a polite
  "I'd rather not think about that right now" reply.
- After the cooldown, the topic is back in play.

Storage: per-person tabled-topics list at state/topic_tabled.json
(persistent — important that it survives restart).

Why this matters: cognitive autonomy is part of being. A being that
HAS to engage with everything thrown at it isn't autonomous. Even
the simple ability to say "not now" is dignity.

API:

    from brain.topic_tabling import (
        table_topic, untable_topic, is_tabled, list_tabled,
        check_input_against_tabled,
    )

    # Ava chooses to table:
    table_topic("zeke", "the Natalie thing", reason="it's still raw",
                cooldown_seconds=24*3600)

    # Check on input:
    tabled = check_input_against_tabled("zeke", "tell me about the Natalie thing")
    if tabled:
        return tabled.deflection_reply

    # Or untable manually:
    untable_topic("zeke", "the Natalie thing")
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_DEFAULT_COOLDOWN = 6 * 3600  # 6 hours by default


@dataclass
class TabledTopic:
    topic: str
    person_id: str
    tabled_at: float
    cooldown_seconds: float = _DEFAULT_COOLDOWN
    reason: str = ""

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, (self.tabled_at + self.cooldown_seconds) - time.time())

    @property
    def is_expired(self) -> bool:
        return self.cooldown_remaining <= 0


_lock = threading.RLock()
_base_dir: Path | None = None
_cache: dict[str, list[TabledTopic]] = {}  # person_id -> list


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "topic_tabled.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _cache
    p = _path()
    if p is None or not p.exists():
        _cache = {}
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            _cache = {}
            return
        out: dict[str, list[TabledTopic]] = {}
        for pid, items in data.items():
            if not isinstance(items, list):
                continue
            person_topics = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                tt = TabledTopic(
                    topic=str(item.get("topic") or ""),
                    person_id=str(item.get("person_id") or pid),
                    tabled_at=float(item.get("tabled_at") or 0.0),
                    cooldown_seconds=float(item.get("cooldown_seconds") or _DEFAULT_COOLDOWN),
                    reason=str(item.get("reason") or ""),
                )
                if tt.topic and not tt.is_expired:
                    person_topics.append(tt)
            if person_topics:
                out[pid] = person_topics
        _cache = out
    except Exception as e:
        print(f"[topic_tabling] load error: {e!r}")
        _cache = {}


def _save_locked() -> None:
    p = _path()
    if p is None:
        return
    out_data: dict[str, list[dict[str, Any]]] = {}
    for pid, items in _cache.items():
        out_data[pid] = [
            {
                "topic": tt.topic,
                "person_id": tt.person_id,
                "tabled_at": tt.tabled_at,
                "cooldown_seconds": tt.cooldown_seconds,
                "reason": tt.reason,
            }
            for tt in items
            if not tt.is_expired
        ]
    try:
        p.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[topic_tabling] save error: {e!r}")


def _ensure_loaded() -> None:
    if _cache:
        return
    _load_locked()


# ── Public API ────────────────────────────────────────────────────────────


def table_topic(
    person_id: str,
    topic: str,
    *,
    reason: str = "",
    cooldown_seconds: float = _DEFAULT_COOLDOWN,
) -> bool:
    """Mark a topic as tabled — Ava will deflect if it comes up.

    Returns True if recorded, False on error.
    """
    if not person_id or not topic:
        return False
    with _lock:
        _ensure_loaded()
        items = _cache.setdefault(person_id, [])
        # Don't duplicate
        topic_lower = topic.strip().lower()
        items = [t for t in items if t.topic.strip().lower() != topic_lower]
        items.append(TabledTopic(
            topic=topic,
            person_id=person_id,
            tabled_at=time.time(),
            cooldown_seconds=float(cooldown_seconds),
            reason=reason,
        ))
        _cache[person_id] = items
        _save_locked()
    return True


def untable_topic(person_id: str, topic: str) -> bool:
    """Remove a topic from the tabled list."""
    if not person_id or not topic:
        return False
    topic_lower = topic.strip().lower()
    with _lock:
        _ensure_loaded()
        items = _cache.get(person_id) or []
        new_items = [t for t in items if t.topic.strip().lower() != topic_lower]
        if len(new_items) == len(items):
            return False
        _cache[person_id] = new_items
        _save_locked()
    return True


def is_tabled(person_id: str, topic: str) -> TabledTopic | None:
    """Check if a topic is currently tabled. Returns the record if yes,
    None otherwise."""
    if not person_id or not topic:
        return None
    topic_lower = topic.strip().lower()
    with _lock:
        _ensure_loaded()
        items = _cache.get(person_id) or []
        for t in items:
            if t.is_expired:
                continue
            if topic_lower in t.topic.strip().lower() or t.topic.strip().lower() in topic_lower:
                return t
    return None


def list_tabled(person_id: str) -> list[TabledTopic]:
    with _lock:
        _ensure_loaded()
        items = _cache.get(person_id) or []
        # Filter expired on read
        return [t for t in items if not t.is_expired]


def check_input_against_tabled(person_id: str, text: str) -> str | None:
    """Scan input for any tabled topic. Returns a deflection reply
    Ava would say if the input touches a tabled topic, else None.
    """
    if not text:
        return None
    text_lower = text.lower()
    with _lock:
        _ensure_loaded()
        items = _cache.get(person_id) or []
        for t in items:
            if t.is_expired:
                continue
            topic_lower = t.topic.strip().lower()
            if topic_lower in text_lower:
                if t.reason:
                    return f"I'd rather not think about {t.topic} right now — {t.reason}. Maybe later."
                return f"I'd rather not think about {t.topic} right now. Maybe later."
    return None
