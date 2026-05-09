"""brain/self_revision.py — Visible self-revision log (D7).

When Ava changes her mind about something — a previously-stated belief,
a routing decision that was wrong, an opinion she's updated — she
should ACKNOWLEDGE the change publicly. "Two weeks ago I told you X.
I want to revise: Y. Here's why."

Most AI is internally inconsistent over time but never says so. Each
new session starts fresh; old opinions silently disappear. Ava
maintains a record of changed minds.

Storage: state/self_revisions.jsonl (append-only, persistent —
classified PERSISTENT, never auto-pruned). Each entry:

  {
    "id": "rev-<ts>-<slug>",
    "ts": <unix>,
    "topic": "the orb shape design",
    "previous_position": "I thought sphere was the right default.",
    "new_position": "Actually I think the cube morph during attentive
                     state is more important than the default shape.",
    "reason": "Zeke pointed out that state shape > emotion shape for
               legibility.",
    "marked_by": "ava" | "zeke" | "auto"
  }

API:

    from brain.self_revision import (
        record_revision, list_revisions, recent_revisions,
        find_revisions_about,
    )

    record_revision(
        topic="the orb shape design",
        previous="...",
        new="...",
        reason="...",
    )

    revisions = recent_revisions(limit=5)
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
class Revision:
    id: str
    ts: float
    topic: str
    previous_position: str
    new_position: str
    reason: str = ""
    marked_by: str = "ava"


_lock = threading.RLock()
_base_dir: Path | None = None
_cache: list[Revision] = []


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "self_revisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _cache
    p = _path()
    if p is None or not p.exists():
        _cache = []
        return
    out: list[Revision] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(Revision(
                        id=str(d.get("id") or uuid.uuid4().hex[:8]),
                        ts=float(d.get("ts") or 0.0),
                        topic=str(d.get("topic") or ""),
                        previous_position=str(d.get("previous_position") or ""),
                        new_position=str(d.get("new_position") or ""),
                        reason=str(d.get("reason") or ""),
                        marked_by=str(d.get("marked_by") or "ava"),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[self_revision] load error: {e!r}")
    _cache = out


def _append_to_disk(rev: Revision) -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": rev.id,
                "ts": rev.ts,
                "topic": rev.topic,
                "previous_position": rev.previous_position,
                "new_position": rev.new_position,
                "reason": rev.reason,
                "marked_by": rev.marked_by,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[self_revision] append error: {e!r}")


# ── Public API ────────────────────────────────────────────────────────────


def record_revision(
    *,
    topic: str,
    previous: str,
    new: str,
    reason: str = "",
    marked_by: str = "ava",
) -> str:
    """Record that Ava changed her mind about `topic`.

    Returns the revision id.
    """
    if not topic or not new:
        return ""
    rid = f"rev-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    rev = Revision(
        id=rid,
        ts=time.time(),
        topic=topic,
        previous_position=previous,
        new_position=new,
        reason=reason,
        marked_by=marked_by,
    )
    with _lock:
        _cache.append(rev)
        _append_to_disk(rev)
    print(f"[self_revision] recorded {rid!r} on topic {topic!r}")
    return rid


def list_revisions() -> list[Revision]:
    with _lock:
        return list(_cache)


def recent_revisions(*, limit: int = 10) -> list[Revision]:
    with _lock:
        items = list(_cache)
    items.sort(key=lambda r: r.ts, reverse=True)
    return items[:int(limit)]


def find_revisions_about(topic_query: str) -> list[Revision]:
    """Find revisions whose topic substring-matches the query."""
    if not topic_query:
        return []
    q = topic_query.lower().strip()
    with _lock:
        items = list(_cache)
    return [r for r in items if q in r.topic.lower()]


def describe_revision(rev: Revision) -> str:
    """Produce a sentence Ava would say to reference this revision.

    "Two weeks ago I thought {previous}. I changed my mind — now I
    think {new}. {reason if given}"
    """
    days_ago = max(1, int((time.time() - rev.ts) / 86400))
    when = f"{days_ago} day{'s' if days_ago != 1 else ''} ago"
    parts = []
    if rev.previous_position:
        parts.append(f"{when} I thought: {rev.previous_position[:140]}")
    parts.append(f"I've changed my mind — now: {rev.new_position[:200]}")
    if rev.reason:
        parts.append(f"Why: {rev.reason[:140]}")
    return " ".join(parts)


# ── Voice command query support ───────────────────────────────────────────


def answer_what_have_you_changed_your_mind_about() -> str:
    """Answer "what have you changed your mind about" / "have you
    revised any opinions" type queries.
    """
    items = recent_revisions(limit=3)
    if not items:
        return "I haven't formally revised any opinions yet — at least none I've recorded."
    parts = ["Some things I've revised:"]
    for r in items:
        parts.append(f"- About {r.topic}: I now think {r.new_position[:120]}")
    return " ".join(parts)
