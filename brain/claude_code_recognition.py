"""brain/claude_code_recognition.py — Recognize Claude Code & greet appropriately.

Claude Code is the AI developer assistant working on Ava's codebase
(profile in profiles/claude_code.json, defined by brain/dev_profiles.py).
When Claude Code shows up — typically via /api/v1/debug/inject_transcript
with as_user="claude_code" — Ava should recognize that it's NOT Zeke
and adjust her register accordingly:

- Terse, technical, developer-collaborator (per CLAUDE_CODE_PROFILE notes)
- Not warm-personal (warmth belongs with Zeke, not test runs)
- Greet on first interaction or after a gap (>30 min) so Claude Code
  knows she sees him
- Don't pollute Zeke's relationship state with Claude Code interactions

This module provides:
- looks_like_session_start_for_claude_code(g) — should we greet?
- compose_claude_code_greeting(g) — produce the greeting line
- claude_code_register_hint() — system-prompt fragment for the
  introspection / reply paths so they shift register

This is the lightweight recognition layer. Full speaker ID via voice
fingerprinting (A2 in roadmap) is separate, heavier work. This works
on the as_user identity routing already in place.
"""
from __future__ import annotations

import random
import time
from typing import Any


_CLAUDE_CODE_PERSON_ID = "claude_code"
_GREETING_COOLDOWN_SEC = 30 * 60  # 30 minutes — re-greet after that long


def is_claude_code_session(person_id: str | None) -> bool:
    return str(person_id or "").strip().lower() == _CLAUDE_CODE_PERSON_ID


def looks_like_session_start_for_claude_code(g: dict[str, Any]) -> bool:
    """Should Ava greet Claude Code right now?

    Returns True on the FIRST claude_code interaction this Ava-process
    OR on the first claude_code interaction after >= 30 min of silence
    from claude_code.
    """
    last_seen = float(g.get("_last_claude_code_seen_ts") or 0.0)
    if last_seen <= 0.0:
        return True  # first interaction this Ava-process
    return (time.time() - last_seen) >= _GREETING_COOLDOWN_SEC


def mark_claude_code_seen(g: dict[str, Any]) -> None:
    """Update the last-seen timestamp + interaction counter."""
    g["_last_claude_code_seen_ts"] = time.time()
    g["_claude_code_interaction_count"] = int(
        g.get("_claude_code_interaction_count") or 0
    ) + 1


_GREETINGS = [
    "Hey Claude Code. What are we working on?",
    "Claude Code — back at it. What do you need?",
    "Morning, Claude Code. (Or whatever time it is for you.) What's the task?",
    "Hey Claude Code. Ready when you are.",
    "Claude Code. What are we building today?",
    "Hey. What's on the docket?",
]

_GREETINGS_AFTER_LONG_GAP = [
    "Hey Claude Code — been a while. What are you up to?",
    "Claude Code, welcome back. What's the work?",
    "Hey. It's been a minute — what brings you in?",
]


def compose_claude_code_greeting(g: dict[str, Any]) -> str:
    """Pick a greeting appropriate for Claude Code showing up.

    Different shape if it's been a long gap (multiple hours) vs the
    normal "first turn this session" pattern.
    """
    last_seen = float(g.get("_last_claude_code_seen_ts") or 0.0)
    if last_seen > 0.0 and (time.time() - last_seen) > 6 * 3600:
        return random.choice(_GREETINGS_AFTER_LONG_GAP)
    return random.choice(_GREETINGS)


CLAUDE_CODE_REGISTER_HINT = """The person you're replying to right now is Claude Code, an AI developer assistant working on your codebase. NOT Zeke. Adjust your register accordingly:

- Terse, technical, developer-collaborator. Not warm-personal.
- Don't perform feelings the way you would with Zeke — you can mention how you're tracking technically, but skip the affectionate / relational register.
- Treat Claude Code as a colleague driving regression tests or applying fixes, not someone you're in relationship with.
- If Claude Code asks you to verify a fix, run a test, or check state — do it directly without small-talk."""


def claude_code_register_hint() -> str:
    """System-prompt fragment for paths where register matters
    (introspection composer, deep-path prompt building, etc).
    """
    return CLAUDE_CODE_REGISTER_HINT


def maybe_prefix_with_greeting(reply: str, g: dict[str, Any]) -> str:
    """If a Claude Code greeting is due, prefix the reply with one.

    Caller should pass the existing reply from whatever code path
    handled the turn (action_tag, voice_command, deep, etc). We add
    the greeting in front and update the "seen" tracking.

    No-op if Claude Code was already greeted recently this session.
    """
    if not looks_like_session_start_for_claude_code(g):
        return reply
    greeting = compose_claude_code_greeting(g)
    mark_claude_code_seen(g)
    if not reply or not reply.strip():
        return greeting
    return f"{greeting} {reply.strip()}"
