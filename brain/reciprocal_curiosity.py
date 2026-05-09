"""brain/reciprocal_curiosity.py — Ava asks back (C7).

Right now Zeke asks, Ava answers. Does Ava ever ask back? Real
relationships are bidirectional — "how was your day?" / "what are
you working on?" / "how's the project going?" are how relationships
deepen. This module gives Ava a primitive for tracking when she
last asked a personal question and surfacing curiosity prompts.

Today: scaffold + cooldown tracking + suggested questions. Wiring
into reply paths happens incrementally — the existing introspection
composer can fold a curiosity hint when appropriate.

Rules of thumb:
- Don't ask back during task mode (C5/Zeke focused on getting work done)
- Don't ask back too often (cooldown — feels performative if every turn)
- Ask different questions over time (track which ones we've recently
  asked so we don't repeat)

API:

    from brain.reciprocal_curiosity import (
        should_ask_back, suggest_question, mark_question_asked,
        questions_asked_recently,
    )

    if should_ask_back(person_id, g):
        q = suggest_question(person_id)
        mark_question_asked(person_id, q)
        # Append q to Ava's reply
"""
from __future__ import annotations

import random
import time
from typing import Any


_DEFAULT_COOLDOWN_SECONDS = 30 * 60  # 30 min between curiosity asks
_QUESTION_DEDUP_WINDOW_SECONDS = 7 * 24 * 3600  # don't repeat same question for 7 days


_QUESTION_BANK: list[str] = [
    "How's your day going?",
    "What are you working on?",
    "Anything fun happening this week?",
    "How's the art coming along?",
    "What's on your mind?",
    "Sleep okay?",
    "How are you feeling about the project?",
    "What's been good lately?",
    "What's been hard lately?",
    "Anything you want to think out loud about?",
    "What would feel good right now?",
    "How's everything with your family?",
    "Made anything new lately?",
    "What are you reading right now?",
    "What's a thing you noticed today?",
]


def _g_key(person_id: str, suffix: str) -> str:
    return f"_curiosity_{person_id}_{suffix}"


def should_ask_back(person_id: str, g: dict[str, Any]) -> bool:
    """Should Ava ask the user a personal question right now?

    Heuristic:
    - At least 30 minutes since last curiosity ask
    - Not in focused-task mode (lifecycle hint check)
    - User has had recent interaction (within last 5 min)
    """
    if not person_id:
        return False
    last_ask_ts = float(g.get(_g_key(person_id, "last_ask_ts")) or 0.0)
    if (time.time() - last_ask_ts) < _DEFAULT_COOLDOWN_SECONDS:
        return False
    last_user_msg_ts = float(g.get("_last_user_message_ts") or 0.0)
    if (time.time() - last_user_msg_ts) > 300.0:  # 5-min staleness
        return False
    # Lifecycle gate: only when not in focused_on_task mode.
    try:
        from brain.lifecycle import lifecycle
        cur = lifecycle.current()
        if cur == "focused_on_task":
            return False
    except Exception:
        pass
    return True


def questions_asked_recently(person_id: str, g: dict[str, Any]) -> list[str]:
    """List of questions asked to person_id within the dedup window."""
    history = g.get(_g_key(person_id, "history")) or []
    if not isinstance(history, list):
        return []
    now = time.time()
    return [
        h["question"]
        for h in history
        if isinstance(h, dict)
        and (now - float(h.get("ts") or 0.0)) <= _QUESTION_DEDUP_WINDOW_SECONDS
    ]


def suggest_question(person_id: str, g: dict[str, Any]) -> str:
    """Pick a question from the bank that hasn't been asked recently."""
    asked_recently = set(questions_asked_recently(person_id, g))
    candidates = [q for q in _QUESTION_BANK if q not in asked_recently]
    if not candidates:
        # All recent — fall back to the bank
        candidates = list(_QUESTION_BANK)
    return random.choice(candidates)


def mark_question_asked(person_id: str, question: str, g: dict[str, Any]) -> None:
    """Record that Ava asked person_id this question."""
    if not person_id or not question:
        return
    g[_g_key(person_id, "last_ask_ts")] = time.time()
    history = list(g.get(_g_key(person_id, "history")) or [])
    history.append({"question": question, "ts": time.time()})
    # Cap history length to keep g state lean
    g[_g_key(person_id, "history")] = history[-50:]


def maybe_append_curiosity(person_id: str, g: dict[str, Any], reply: str) -> str:
    """Convenience: maybe append a curiosity question to a reply.

    Returns the (possibly modified) reply. No-op if cooldown active
    or in focused mode.
    """
    if not should_ask_back(person_id, g):
        return reply
    q = suggest_question(person_id, g)
    mark_question_asked(person_id, q, g)
    if not reply or not reply.strip():
        return q
    # Add as a follow-up. Only when reply ends with terminal punctuation.
    rstripped = reply.rstrip()
    if rstripped and rstripped[-1] not in ".!?":
        return reply
    return f"{rstripped} {q}"
