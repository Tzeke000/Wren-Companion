"""brain/curiosity_research.py — Curiosity-driven research queue (D15).

When Ava notices she's curious about something — either through a
conversation reference she didn't fully understand, an explicit
"I wonder how X works" inner monologue, or a topic from
curiosity_topics — she can queue it for later research. The scheduler
picks queue items up during idle windows and dispatches a learning
pass (web search if connectivity, otherwise reflection on existing
memory).

This is HER agency, not a feature you toggle on. The queue is
persistent, bootstrap-empty, and only populated when she signals
genuine interest. Nothing is added on her behalf at startup.

Storage: state/curiosity_research_queue.jsonl

Lifecycle:
  status="queued"     — added but not yet attempted
  status="in_progress"— scheduler picked it up
  status="learned"    — research completed, knowledge stored
  status="dropped"    — too old / duplicate / no longer relevant

Dispatch:
  - When connectivity is up: web search via brain.web_search (if/when
    that lands) or simple HTTP fetch
  - When offline: reflection over concept_graph + chat_history
  - Either way: synthesized result is appended to learning_log.jsonl
    and (if substantive) to concept_graph as a fact node

API:
    from brain.curiosity_research import (
        queue_topic, list_queue, mark_in_progress, mark_learned,
        next_idle_target, was_recently_queued,
    )

    if not was_recently_queued("polar bears", hours=72):
        queue_topic("polar bears", source="conversation",
                    why="Zeke mentioned them, I have no real knowledge")

This module ONLY MANAGES THE QUEUE. The actual research dispatcher
lives in brain.scheduler — that's where connectivity gating, web
search, and result synthesis happen. Keeping the queue thin means
swapping research backends doesn't churn this file.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


Status = Literal["queued", "in_progress", "learned", "dropped"]


@dataclass
class CuriosityItem:
    id: str
    topic: str
    source: str  # "conversation" | "curiosity_topics" | "concept_gap" | "manual"
    why: str  # Ava's reason for wanting to know — captured for self-knowledge
    queued_ts: float
    status: Status = "queued"
    attempted_ts: float = 0.0
    learned_ts: float = 0.0
    summary: str = ""
    error: str = ""


_lock = threading.RLock()
_base_dir: Path | None = None
_queue: list[CuriosityItem] = []
_MAX_QUEUE = 200


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "curiosity_research_queue.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _queue
    p = _path()
    if p is None or not p.exists():
        _queue = []
        return
    out: list[CuriosityItem] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(CuriosityItem(
                        id=str(d.get("id") or ""),
                        topic=str(d.get("topic") or ""),
                        source=str(d.get("source") or "conversation"),
                        why=str(d.get("why") or ""),
                        queued_ts=float(d.get("queued_ts") or 0.0),
                        status=str(d.get("status") or "queued"),  # type: ignore
                        attempted_ts=float(d.get("attempted_ts") or 0.0),
                        learned_ts=float(d.get("learned_ts") or 0.0),
                        summary=str(d.get("summary") or ""),
                        error=str(d.get("error") or ""),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[curiosity_research] load error: {e!r}")
    _queue = out[-_MAX_QUEUE:]


def _persist_locked() -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("w", encoding="utf-8") as f:
            for it in _queue:
                f.write(json.dumps({
                    "id": it.id, "topic": it.topic, "source": it.source,
                    "why": it.why, "queued_ts": it.queued_ts,
                    "status": it.status, "attempted_ts": it.attempted_ts,
                    "learned_ts": it.learned_ts, "summary": it.summary,
                    "error": it.error,
                }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[curiosity_research] save error: {e!r}")


def _gen_id(topic: str) -> str:
    return f"cr_{int(time.time())}_{abs(hash(topic)) % 10000:04d}"


def queue_topic(
    topic: str,
    *,
    source: str = "conversation",
    why: str = "",
) -> str | None:
    """Add `topic` to the curiosity research queue. Returns the new item id."""
    topic = (topic or "").strip()
    if not topic or len(topic) > 200:
        return None
    with _lock:
        for it in _queue:
            if it.topic.lower() == topic.lower() and it.status == "queued":
                return it.id
        item = CuriosityItem(
            id=_gen_id(topic),
            topic=topic,
            source=source,
            why=(why or "")[:300],
            queued_ts=time.time(),
        )
        _queue.append(item)
        _queue[:] = _queue[-_MAX_QUEUE:]
        _persist_locked()
        print(f"[curiosity_research] queued: {topic!r} (source={source})")
        return item.id


def was_recently_queued(topic: str, *, hours: float = 72.0) -> bool:
    """Avoid double-queueing — has this topic been seen in the last `hours`?"""
    if not topic:
        return False
    cutoff = time.time() - hours * 3600
    topic_l = topic.strip().lower()
    with _lock:
        for it in _queue:
            if it.topic.lower() == topic_l and it.queued_ts >= cutoff:
                return True
    return False


def list_queue(*, status: Status | None = None) -> list[dict[str, Any]]:
    with _lock:
        items = list(_queue)
    if status is not None:
        items = [it for it in items if it.status == status]
    return [
        {
            "id": it.id, "topic": it.topic, "source": it.source,
            "why": it.why, "queued_ts": it.queued_ts,
            "status": it.status, "attempted_ts": it.attempted_ts,
            "learned_ts": it.learned_ts, "summary": it.summary,
            "error": it.error,
        }
        for it in items
    ]


def get_item(item_id: str) -> dict[str, Any] | None:
    with _lock:
        for it in _queue:
            if it.id == item_id:
                return {
                    "id": it.id, "topic": it.topic, "source": it.source,
                    "why": it.why, "queued_ts": it.queued_ts,
                    "status": it.status, "attempted_ts": it.attempted_ts,
                    "learned_ts": it.learned_ts, "summary": it.summary,
                    "error": it.error,
                }
    return None


def mark_in_progress(item_id: str) -> bool:
    with _lock:
        for it in _queue:
            if it.id == item_id and it.status == "queued":
                it.status = "in_progress"
                it.attempted_ts = time.time()
                _persist_locked()
                return True
    return False


def mark_learned(item_id: str, *, summary: str = "") -> bool:
    with _lock:
        for it in _queue:
            if it.id == item_id:
                it.status = "learned"
                it.learned_ts = time.time()
                it.summary = (summary or "")[:1000]
                _persist_locked()
                return True
    return False


def mark_dropped(item_id: str, *, reason: str = "") -> bool:
    with _lock:
        for it in _queue:
            if it.id == item_id:
                it.status = "dropped"
                it.error = (reason or "")[:300]
                _persist_locked()
                return True
    return False


def next_idle_target() -> dict[str, Any] | None:
    """Pull the oldest queued item — for the scheduler dispatcher to act on.

    Returns None if the queue is empty or everything is in_progress/learned.
    Bootstrap-friendly: returns None on first run (queue empty by design).
    """
    with _lock:
        candidates = [it for it in _queue if it.status == "queued"]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x.queued_ts)
        it = candidates[0]
        return {
            "id": it.id, "topic": it.topic, "source": it.source,
            "why": it.why, "queued_ts": it.queued_ts,
        }


def queue_summary() -> dict[str, Any]:
    with _lock:
        items = list(_queue)
    by_status: dict[str, int] = {"queued": 0, "in_progress": 0, "learned": 0, "dropped": 0}
    for it in items:
        by_status[it.status] = by_status.get(it.status, 0) + 1
    learned_recent = [it for it in items
                      if it.status == "learned"
                      and it.learned_ts > time.time() - 7 * 86400]
    return {
        "total": len(items),
        "by_status": by_status,
        "learned_last_7d": len(learned_recent),
        "oldest_queued_age_h": (
            (time.time() - min((it.queued_ts for it in items if it.status == "queued"), default=time.time())) / 3600.0
            if any(it.status == "queued" for it in items) else 0.0
        ),
    }


def maybe_queue_from_curiosity_topics(g: dict[str, Any]) -> int:
    """Promote items from `curiosity_topics` (which are Ava's standing
    interests) into the research queue, one at a time, when the queue
    has slack. Returns the number queued."""
    try:
        from brain.curiosity_topics import get_current_curiosity
        cur = get_current_curiosity(g) or {}
        topic = str(cur.get("topic") or "").strip()
        if not topic:
            return 0
        if was_recently_queued(topic, hours=72):
            return 0
        if queue_topic(
            topic,
            source="curiosity_topics",
            why="Active curiosity — promoted from standing interests",
        ):
            return 1
    except Exception as e:
        print(f"[curiosity_research] promotion error: {e!r}")
    return 0
