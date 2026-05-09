"""brain/clarification.py — Asking-for-clarification (C14).

When Ava's intent is ambiguous OR the action is non-trivial / non-
reversible, she should pause and ask rather than guess. Currently
she takes the most-likely interpretation and runs.

Detection cases:
- Multiple matching candidates in app catalog (e.g., "open Edge"
  when both Edge and Edge Dev are installed)
- Compound action with ambiguous target ("close that" without prior
  context)
- Destructive verb ("delete" / "remove" / "wipe") — high-impact,
  always confirm
- Plural without qualifier ("close my tabs" — which?)

API:

    from brain.clarification import (
        ambiguous_open(name, base_dir),
        is_destructive_verb(text),
        build_clarification_question(reason, options),
    )

    # In the open path:
    matches = ambiguous_open("Edge", base_dir)
    if len(matches) > 1:
        q = build_clarification_question(
            reason=f"I see {len(matches)} options for Edge",
            options=[m['name'] for m in matches],
        )
        return q  # ask Zeke instead of picking
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\b(?:delete|remove|wipe|erase|destroy|unrecover)\b", re.IGNORECASE),
    re.compile(r"\bbulk\s+(?:close|kill|terminate)\b", re.IGNORECASE),
    re.compile(r"\bforce\s+(?:close|kill|quit|shutdown|restart)\b", re.IGNORECASE),
    re.compile(r"\bsudo\s+rm\b", re.IGNORECASE),
    re.compile(r"\bformat\s+(?:disk|drive)\b", re.IGNORECASE),
]


def is_destructive_verb(text: str) -> bool:
    """Does the request use a destructive verb that warrants confirmation?"""
    if not text:
        return False
    return any(p.search(text) for p in _DESTRUCTIVE_PATTERNS)


def ambiguous_open(name: str, base_dir: Path) -> list[dict[str, Any]]:
    """Find all catalog candidates for an open-app query.

    Returns 0, 1, or N matches. N > 1 means ambiguous — caller
    should ask for clarification.
    """
    if not name:
        return []
    try:
        from brain.app_catalog import load_catalog
        cat = load_catalog(base_dir)
        entries = cat.get("entries") or []
    except Exception:
        return []
    if not entries:
        return []
    q = name.strip().lower()
    matches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for e in entries:
        if e.get("id") in seen_ids:
            continue
        # Exact alias match
        for alias in e.get("aliases", []):
            if alias == q:
                matches.append(e)
                seen_ids.add(e.get("id"))
                break
        else:
            # Substring match
            if q in str(e.get("name") or "").lower():
                matches.append(e)
                seen_ids.add(e.get("id"))
                continue
            for alias in e.get("aliases", []):
                if q in alias:
                    matches.append(e)
                    seen_ids.add(e.get("id"))
                    break
    return matches[:6]  # cap to keep clarification readable


def build_clarification_question(
    *,
    reason: str = "",
    options: list[str] | None = None,
    intent: str = "",
) -> str:
    """Produce a clarification question.

    `reason` is the framing ("I see multiple matches"). `options` is
    the list of candidate values to choose from. `intent` is what Ava
    thinks the user was asking for (used in the ask).
    """
    parts = []
    if reason:
        parts.append(reason.rstrip(".") + ".")
    if options:
        if len(options) == 2:
            parts.append(f"Did you mean {options[0]} or {options[1]}?")
        else:
            head = ", ".join(options[:-1])
            parts.append(f"Did you mean {head}, or {options[-1]}?")
    elif intent:
        parts.append(f"Did you mean {intent}?")
    if not parts:
        return "Could you say a bit more about what you wanted?"
    return " ".join(parts)


def confirmation_for_destructive(text: str, *, target: str = "") -> str:
    """Produce a confirmation question for a destructive action.

    Used when is_destructive_verb(text) is True. Caller should NOT
    execute the action until user confirms.
    """
    if target:
        return f"To confirm — you want me to {text.strip().rstrip('?.!')} {target}? That's not reversible."
    return f"To confirm — {text.strip().rstrip('?.!')}? That's not reversible."
