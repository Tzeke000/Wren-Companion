"""brain/working_memory.py — Multi-turn task tracking + attention continuity (B3, C2).

Working memory layer for ACTIVE THREADS — tasks Ava is helping with
right now, current conversation topic, last action target. Distinct
from chat_history (episodic, append-only) and concept_graph (semantic,
long-term).

This is L1 in the memory hierarchy formalization (architecture #3).
The pieces:

- Active tasks: explicitly-tracked work threads ("debug the build error",
  "plan the morning workspace"). Created when user says "I'm working on X"
  / "let me debug this." Closed when explicitly resolved or after long
  inactivity.
- Current topic: the conversation's current subject. Updated each turn.
  Lets pronouns resolve cleanly across turns.
- Last action target: explicit "open chrome" -> chrome. Already
  partly tracked via _last_opened_app.

Why this matters: today Ava treats each turn as starting fresh from
chat history retrieval. Working memory lets her hold a project
thread for hours. "Still chasing the X error?" when you come back
from lunch. "Did you find what we were looking for?" later.

Storage: lives in g (in-memory only — ephemeral per state_classification).
Active task summaries that survive restart are written via journal.

API:

    from brain.working_memory import (
        start_task, end_task, list_active_tasks,
        set_current_topic, get_current_topic, update_topic_from_input,
        last_action_target, set_last_action_target,
    )

    start_task(g, "debug the build error", details={...})
    set_current_topic(g, "the build error")

    # Later turn:
    if list_active_tasks(g):
        # remind / re-engage
        ...
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


_ACTIVE_TASK_TIMEOUT_SECONDS = 6 * 3600  # 6 hours of no mention -> close


@dataclass
class ActiveTask:
    id: str
    description: str
    started_ts: float
    last_mentioned_ts: float
    details: dict[str, Any] = field(default_factory=dict)
    person_id: str = ""

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_mentioned_ts) > _ACTIVE_TASK_TIMEOUT_SECONDS


# ── Working memory state lives in g ──────────────────────────────────────


def _tasks(g: dict[str, Any]) -> list[ActiveTask]:
    """Get the active tasks list. Filters stale entries."""
    raw = g.get("_active_tasks") or []
    if not isinstance(raw, list):
        g["_active_tasks"] = []
        return []
    fresh: list[ActiveTask] = []
    for r in raw:
        if isinstance(r, ActiveTask):
            if not r.is_stale:
                fresh.append(r)
        elif isinstance(r, dict):
            try:
                t = ActiveTask(
                    id=str(r.get("id") or ""),
                    description=str(r.get("description") or ""),
                    started_ts=float(r.get("started_ts") or 0.0),
                    last_mentioned_ts=float(r.get("last_mentioned_ts") or 0.0),
                    details=dict(r.get("details") or {}),
                    person_id=str(r.get("person_id") or ""),
                )
                if not t.is_stale:
                    fresh.append(t)
            except Exception:
                continue
    g["_active_tasks"] = fresh
    return fresh


def _save_tasks(g: dict[str, Any], tasks: list[ActiveTask]) -> None:
    g["_active_tasks"] = tasks


# ── Active tasks API ──────────────────────────────────────────────────────


def start_task(
    g: dict[str, Any],
    description: str,
    *,
    details: dict[str, Any] | None = None,
    person_id: str = "",
) -> str:
    """Start tracking an active task. Returns the task id."""
    if not description:
        return ""
    import uuid
    tid = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    task = ActiveTask(
        id=tid,
        description=description.strip()[:200],
        started_ts=time.time(),
        last_mentioned_ts=time.time(),
        details=dict(details or {}),
        person_id=person_id,
    )
    tasks = _tasks(g)
    tasks.append(task)
    _save_tasks(g, tasks)
    return tid


def end_task(g: dict[str, Any], task_id: str) -> bool:
    """Mark a task as resolved. Returns True if found + removed."""
    if not task_id:
        return False
    tasks = _tasks(g)
    new_tasks = [t for t in tasks if t.id != task_id]
    if len(new_tasks) == len(tasks):
        return False
    _save_tasks(g, new_tasks)
    return True


def touch_task(g: dict[str, Any], task_id: str) -> bool:
    """Mark a task as recently mentioned (extends its timeout)."""
    tasks = _tasks(g)
    for t in tasks:
        if t.id == task_id:
            t.last_mentioned_ts = time.time()
            _save_tasks(g, tasks)
            return True
    return False


def list_active_tasks(g: dict[str, Any], *, person_id: str | None = None) -> list[ActiveTask]:
    tasks = _tasks(g)
    if person_id is not None:
        tasks = [t for t in tasks if t.person_id == person_id]
    return tasks


def find_task_by_topic(g: dict[str, Any], topic: str) -> ActiveTask | None:
    """Look up an active task whose description matches a topic."""
    if not topic:
        return None
    q = topic.lower()
    for t in _tasks(g):
        if q in t.description.lower():
            return t
    return None


# ── Current topic / attention ────────────────────────────────────────────


def set_current_topic(g: dict[str, Any], topic: str) -> None:
    """Update the conversation's current topic."""
    if not topic:
        return
    g["_current_topic"] = topic.strip()[:200]
    g["_current_topic_ts"] = time.time()


def get_current_topic(g: dict[str, Any]) -> str:
    """Return the current topic if it's still fresh (< 5 min old)."""
    topic = str(g.get("_current_topic") or "")
    ts = float(g.get("_current_topic_ts") or 0.0)
    if not topic or (time.time() - ts) > 300:
        return ""
    return topic


def update_topic_from_input(g: dict[str, Any], user_input: str) -> str:
    """Extract a topic candidate from user_input + update current_topic.

    Uses theory_of_mind topic extraction. Returns the topic set
    (or empty if none extracted).
    """
    try:
        from brain.theory_of_mind import topics_in_reply
        topics = topics_in_reply(user_input)
        if topics:
            set_current_topic(g, topics[0])
            return topics[0]
    except Exception:
        pass
    return ""


# ── Last action target (already partly tracked via _last_opened_app) ─────


def set_last_action_target(g: dict[str, Any], target: str, *, action_kind: str = "") -> None:
    g["_last_action_target"] = target
    g["_last_action_kind"] = action_kind
    g["_last_action_ts"] = time.time()


def last_action_target(g: dict[str, Any]) -> str:
    target = str(g.get("_last_action_target") or "")
    ts = float(g.get("_last_action_ts") or 0.0)
    if (time.time() - ts) > 300:  # 5 min staleness
        return ""
    return target


# ── Convenience hint for system prompts ──────────────────────────────────


def working_memory_hint(g: dict[str, Any]) -> str:
    """Produce a system-prompt fragment describing current working memory.

    Folded into reply paths so Ava has working-memory context in
    every turn.
    """
    parts = []
    topic = get_current_topic(g)
    if topic:
        parts.append(f"Current topic: {topic}")
    last_target = last_action_target(g)
    if last_target:
        parts.append(f"Last action target: {last_target}")
    tasks = list_active_tasks(g)
    if tasks:
        parts.append("Active threads:")
        for t in tasks[:4]:
            mins_ago = int((time.time() - t.last_mentioned_ts) / 60)
            parts.append(f"  - {t.description} (last mentioned {mins_ago}m ago)")
    if not parts:
        return ""
    return "Working memory:\n" + "\n".join(parts)


# ── Voice command query support: "what are we working on" ─────────────────


def answer_active_threads_query(g: dict[str, Any]) -> str:
    """Answer "what are we working on / what's open / any pending tasks" queries."""
    tasks = list_active_tasks(g)
    if not tasks:
        return "Nothing active right now. We're between things."
    if len(tasks) == 1:
        return f"We're working on: {tasks[0].description}."
    parts = ["A few things:"]
    for t in tasks[:5]:
        parts.append(f"- {t.description}")
    return "\n".join(parts)
