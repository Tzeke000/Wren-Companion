"""
Phase 96 — Conversation quality improvement.

response_quality_check: checks for too-short, too-long, repetitive, generic responses.
Flags for one regeneration attempt if issues found.
Tracks quality scores and response diversity patterns.
Bootstrap: Ava develops her own quality standards via self_critique.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_QUALITY_LOG = "state/response_quality.jsonl"
_OPENER_HISTORY: list[str] = []  # in-process ring buffer
_MAX_OPENER_HISTORY = 10

_CASUAL_TOPICS = {
    "hi", "hello", "hey", "thanks", "ok", "cool", "nice",
    "bye", "goodnight", "good morning", "good night",
}


def _opening_word(reply: str) -> str:
    """Return first word of reply, lowercased."""
    words = reply.strip().split()
    return words[0].lower().rstrip(".,!?") if words else ""


def _log_quality(g: dict[str, Any], entry: dict[str, Any]) -> None:
    path = Path(g.get("BASE_DIR") or ".") / _QUALITY_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def response_quality_check(
    reply: str, user_input: str, context: dict[str, Any], g: dict[str, Any]
) -> tuple[str, list[str]]:
    """
    Check reply quality. Returns (possibly-improved reply, list of issues).
    Max 1 regeneration attempt.
    """
    global _OPENER_HISTORY
    issues: list[str] = []
    words = reply.strip().split()
    n_words = len(words)
    inp_low = user_input.lower().strip()

    # Check 1: too short for substantive question
    is_casual = any(inp_low.startswith(t) for t in _CASUAL_TOPICS) or len(inp_low.split()) < 4
    if not is_casual and n_words < 15 and "?" not in user_input:
        issues.append("too_short")

    # Check 2: too long for casual chat (only in fast path)
    if is_casual and n_words > 200:
        issues.append("too_long")

    # Check 3: repetitive opener
    opener = _opening_word(reply)
    if opener:
        recent_openers = _OPENER_HISTORY[-_MAX_OPENER_HISTORY:]
        if recent_openers.count(opener) >= 3:
            issues.append("repetitive_opener")

    # Check 4: starts with "I" too often
    if opener == "i":
        recent_i = sum(1 for op in _OPENER_HISTORY[-5:] if op == "i")
        if recent_i >= 3:
            issues.append("too_many_i_openers")

    # Update opener history
    if opener:
        _OPENER_HISTORY.append(opener)
        _OPENER_HISTORY = _OPENER_HISTORY[-_MAX_OPENER_HISTORY:]

    # Log quality score
    score = 1.0 - len(issues) * 0.25
    _log_quality(g, {
        "ts": time.time(),
        "word_count": n_words,
        "issues": issues,
        "score": round(max(0.0, score), 3),
        "opener": opener,
    })

    if not issues:
        return reply, []

    # One regeneration attempt with specific fix instruction
    try:
        improved = _attempt_fix(reply, user_input, issues, g)
        if improved and improved != reply:
            return improved, issues
    except Exception:
        pass

    return reply, issues


def _attempt_fix(
    reply: str, user_input: str, issues: list[str], g: dict[str, Any]
) -> str:
    """One regeneration attempt with fix instructions."""
    fix_instructions: list[str] = []
    if "too_short" in issues:
        fix_instructions.append("expand your answer slightly — add 1-2 more sentences of substance")
    if "too_long" in issues:
        fix_instructions.append("trim to under 100 words — be more concise")
    if "repetitive_opener" in issues or "too_many_i_openers" in issues:
        fix_instructions.append("vary your opening — don't start with the same word as recent replies")

    if not fix_instructions:
        return reply

    instruction = " and ".join(fix_instructions)
    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import HumanMessage
        llm = ChatOllama(model="ava-personal:latest", temperature=0.6)
        prompt = (
            f"Here is your reply to '{user_input[:100]}':\n\n{reply}\n\n"
            f"Please rewrite it to {instruction}. Keep your personality. Return only the rewritten reply."
        )
        res = llm.invoke([HumanMessage(content=prompt)])
        improved = str(getattr(res, "content", str(res))).strip()
        if improved and len(improved) > 5:
            return improved
    except Exception:
        pass
    return reply


def track_response_diversity(g: dict[str, Any]) -> dict[str, Any]:
    """Check if Ava keeps steering toward same topics or openers."""
    path = Path(g.get("BASE_DIR") or ".") / _QUALITY_LOG
    if not path.is_file():
        return {}
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-30:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    if not entries:
        return {}

    opener_counts: dict[str, int] = {}
    for e in entries:
        op = str(e.get("opener") or "")
        if op:
            opener_counts[op] = opener_counts.get(op, 0) + 1

    dominant_opener = max(opener_counts.items(), key=lambda x: x[1], default=("", 0))
    avg_score = sum(float(e.get("score") or 1.0) for e in entries) / max(1, len(entries))

    return {
        "dominant_opener": dominant_opener[0],
        "dominant_opener_count": dominant_opener[1],
        "avg_quality_score": round(avg_score, 3),
        "total_logged": len(entries),
    }
