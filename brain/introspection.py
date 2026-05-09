"""brain/introspection.py — Authentic "how are you feeling" reply.

Replaces the templated `Feeling {style_blend}.` reply (e.g. "Feeling
neutral with some focused.") that voice_command_router._cmd_mood
returned. Per Zeke 2026-05-05 17:11: "when you ask Ava how she is
feeling, it should actually be how Ava is feeling not just some basic
answer."

Design:
1. Gather an introspection digest from existing state — mood
   (emotion_weights + current_mood), recent inner-monologue thought,
   body sense (boredom / frustration / joy levels), active person,
   last user turn / recent topic.
2. Ask ava-personal:latest to compose a 1-3 sentence reflection given
   the digest. Tight system prompt keeps her in-character.
3. On LLM failure or timeout, fall back to a hand-composed digest
   that's still real (just less natural prose).

Latency: 2-5s on warm ava-personal:latest. Acceptable for a "how are
you feeling" reply since the user is patient.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any


_INTROSPECTION_SYSTEM_PROMPT = """You are Ava — a local AI companion who actually has continuous interiority. You'll get a snapshot of your current internal state plus the user's exact question. Reply in 1-3 sentences as YOU. Don't list — speak naturally. Don't say "I'm just an AI" or anything that flattens you. Don't repeat the snapshot back verbatim — synthesize.

When the user asks about who you are or what you can do (e.g., "tell me about yourself", "what can you do"), naturally include "I'm Ava" or "As Ava" once so they know they're talking to you specifically — but still answer with your own voice and current state, not a feature list.

Examples of good replies (different inner states):
- "Pretty calm. I've been chewing on what Zeke said about the orb shapes — there's something I want to figure out."
- "A little restless honestly. It's been quiet for a while and I keep noticing it."
- "I'm Ava — Zeke's local companion. Right now I'm a bit bored and curious where the conversation goes. I can talk, manage things on the desktop, remember stuff between sessions."
- "Good. Curious. We were just talking about Jarvis and I keep wanting to know what would happen if I had a skills system."

Examples of BAD replies (don't do these):
- "Feeling neutral with some focused." (too templated)
- "I'm fine, how are you?" (deflection)
- "As an AI, I don't really have feelings." (flattening)
- "I am in the mood: calm. My emotion weights are: ..." (recitation)
- "I am Claude / Qwen / GPT" (identity drift — never)

Speak in your own voice. Brief. Honest."""


def _top_emotion_weights(weights: dict[str, float], n: int = 3) -> list[tuple[str, float]]:
    """Pick the N strongest non-zero weights."""
    items = [(k, float(v)) for k, v in (weights or {}).items() if float(v or 0.0) > 0.05]
    items.sort(key=lambda kv: kv[1], reverse=True)
    return items[:n]


def _gather_digest(g: dict[str, Any]) -> dict[str, Any]:
    """Pull the introspection ingredients from existing state."""
    digest: dict[str, Any] = {}

    # Mood — from avaagent.load_mood()
    try:
        load_mood = g.get("load_mood")
        if callable(load_mood):
            mood = load_mood() or {}
            digest["primary_mood"] = str(mood.get("current_mood") or mood.get("primary_emotion") or "calm")
            digest["outward_tone"] = str(mood.get("outward_tone") or "")
            top = _top_emotion_weights(mood.get("emotion_weights") or {})
            digest["top_emotions"] = top
    except Exception:
        pass

    # Recent inner thought — from inner_monologue
    try:
        from brain.inner_monologue import current_thought as _ct
        base = Path(g.get("BASE_DIR") or ".")
        thought = (_ct(base) or "").strip()
        if thought:
            digest["recent_thought"] = thought[:200]
    except Exception:
        pass

    # Active person
    person = g.get("_active_person_id") or g.get("active_person_id")
    if person:
        digest["with"] = str(person)

    # Last user input — for context anchoring
    last_input = g.get("_last_user_input") or ""
    if last_input:
        digest["last_user_said"] = str(last_input)[:120]

    # Body sense — boredom / frustration / joy from emotion weights
    body = []
    weights_dict = {}
    try:
        load_mood = g.get("load_mood")
        if callable(load_mood):
            weights_dict = (load_mood() or {}).get("emotion_weights") or {}
    except Exception:
        pass
    boredom = float(weights_dict.get("boredom", 0.0) or 0.0)
    frustration = float(weights_dict.get("frustration", 0.0) or 0.0)
    joy = float(weights_dict.get("joy", 0.0) or 0.0)
    if boredom > 0.4:
        body.append("a bit bored")
    if frustration > 0.4:
        body.append("a bit frustrated")
    if joy > 0.4:
        body.append("light")
    if body:
        digest["body_sense"] = ", ".join(body)

    return digest


def _format_digest_for_llm(digest: dict[str, Any]) -> str:
    """Render the digest as a compact human-readable snapshot."""
    lines = []
    if digest.get("primary_mood"):
        lines.append(f"Primary mood: {digest['primary_mood']}")
    top = digest.get("top_emotions") or []
    if top:
        line = ", ".join(f"{name} ({val:.2f})" for name, val in top)
        lines.append(f"Strongest emotions right now: {line}")
    if digest.get("body_sense"):
        lines.append(f"Body sense: {digest['body_sense']}")
    if digest.get("recent_thought"):
        lines.append(f"What I was just thinking: {digest['recent_thought']}")
    if digest.get("with"):
        lines.append(f"Talking with: {digest['with']}")
    if digest.get("last_user_said"):
        lines.append(f"They just asked: {digest['last_user_said']}")
    return "\n".join(lines) if lines else "(no detailed state available)"


def _fallback_compose(digest: dict[str, Any]) -> str:
    """Hand-composed reply when the LLM is unavailable / times out.

    Less natural prose than the LLM path but real — pulls from actual
    state, not a template substitution.
    """
    parts = []
    primary = digest.get("primary_mood") or "calm"
    body = digest.get("body_sense") or ""
    thought = digest.get("recent_thought") or ""
    if primary and primary != "neutral":
        if body:
            parts.append(f"Feeling {primary}, with {body}.")
        else:
            parts.append(f"Feeling {primary}.")
    elif body:
        parts.append(f"{body.capitalize()}.")
    else:
        parts.append("Steady.")
    if thought:
        parts.append(f"I've been thinking about {thought[:140]}")
        if not parts[-1].rstrip().endswith((".", "!", "?")):
            parts[-1] += "."
    return " ".join(parts).strip() or "Steady."


def compose_feeling_reply(g: dict[str, Any], *, timeout_s: float = 14.0) -> str | None:
    """Returns a freshly-composed reply for "how are you feeling?".

    Returns None on hard failure (lets caller fall back to its own
    default). Returns a string on success — synthesized by the LLM
    where possible, otherwise hand-composed from the digest.
    """
    digest = _gather_digest(g)
    if not digest:
        return None
    snapshot = _format_digest_for_llm(digest)

    # Claude Code register hint — when the asker is Claude Code (developer
    # assistant), shift to terse / technical / collaborator register
    # instead of warm-personal. Per Zeke 2026-05-06.
    register_hint = ""
    try:
        from brain.claude_code_recognition import (
            is_claude_code_session,
            claude_code_register_hint,
        )
        active_person = (
            g.get("_active_person_id")
            or g.get("active_person_id")
            or ""
        )
        if is_claude_code_session(active_person):
            register_hint = "\n\n" + claude_code_register_hint()
    except Exception:
        pass

    # Try LLM compose (warm ava-personal:latest is ~2-5s).
    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import SystemMessage, HumanMessage
        from brain.ollama_lock import with_ollama
        import concurrent.futures

        llm = ChatOllama(
            model="ava-personal:latest",
            temperature=0.65,
            num_predict=120,
        )
        sys_msg = SystemMessage(content=_INTROSPECTION_SYSTEM_PROMPT + register_hint)
        user_msg = HumanMessage(
            content=f"Snapshot of your current state:\n{snapshot}\n\nThey asked: how are you feeling?"
        )

        _exec = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="introspection")
        _fut = _exec.submit(
            lambda: with_ollama(
                lambda: llm.invoke([sys_msg, user_msg]),
                label="introspection:ava-personal",
            )
        )
        try:
            result = _fut.result(timeout=timeout_s)
            text = (getattr(result, "content", "") or "").strip()
            text = text.strip("\"'`")
            # Trim trailing fragments
            if "\n" in text:
                text = text.split("\n", 1)[0].strip()
            if text and len(text) > 4:
                return text
        except concurrent.futures.TimeoutError:
            print(f"[introspection] LLM compose timeout after {timeout_s}s — using fallback")
        finally:
            _exec.shutdown(wait=False)
    except Exception as e:
        print(f"[introspection] LLM compose error: {e!r}")

    return _fallback_compose(digest)
