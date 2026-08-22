"""pointing_context — join voice utterances to the pointing ledger.

Zeke's design (2026-08-21 night): "i wont be pointing forever — when i finish
saying it i stop pointing, so youll have to reference what your eyes saw with
what i said, pull from time stamps." The ledger side (transient gestures +
timestamps, recall + look) was built and proven that night; this is the
voice-timestamp plumbing (built 2026-08-22, activates with the next stack
restart alongside the voice loop itself).

Wire point: iris_runtime.voice_next_input wraps its result through
attach_pointing_context(). If the utterance contains a deictic word ("that",
"there", "this one", ...), the nearest pointing events from the last ~45s are
attached under result["pointing"], so the responding session sees WHERE he
pointed without an extra tool call. Acting on it (pointing_ledger
action=look) stays a deliberate choice.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_LEDGER = Path(__file__).resolve().parent.parent / "state" / "pointing_ledger.jsonl"

# Deictic terms that imply a spatial referent. Word-boundary matched,
# case-insensitive. "it" alone is deliberately EXCLUDED (too common —
# "how's it going" would false-positive every greeting).
_DEICTIC_RE = re.compile(
    r"\b(that|there|this|these|those|here|over there|right there|"
    r"that one|this one|that thing|whats that|what's that)\b",
    re.IGNORECASE,
)

_WINDOW_BEFORE_S = 45.0   # gesture usually precedes the end of speech
_WINDOW_AFTER_S = 2.0
_MAX_EVENTS = 2
_TAIL_BYTES = 64 * 1024   # ledger tail read — events are ~200B each


def utterance_is_deictic(text: str) -> bool:
    return bool(_DEICTIC_RE.search(text or ""))


def recent_pointing_events(anchor_ts: float | None = None) -> list[dict]:
    """Pointing events within the window around anchor_ts (default: now),
    newest first, capped at _MAX_EVENTS. Each gains age_s for readability."""
    now = time.time()
    anchor = float(anchor_ts or now)
    lo, hi = anchor - _WINDOW_BEFORE_S, anchor + _WINDOW_AFTER_S
    out: list[dict] = []
    try:
        with _LEDGER.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
        for ln in tail.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except Exception:
                continue
            ts = float(e.get("ts") or 0.0)
            if lo <= ts <= hi:
                e["age_s"] = round(now - ts, 1)
                out.append(e)
    except Exception:
        return []
    out.sort(key=lambda e: float(e.get("ts") or 0.0), reverse=True)
    return out[:_MAX_EVENTS]


def attach_pointing_context(result: dict[str, Any]) -> dict[str, Any]:
    """Wrap for voice_next_input's return: attach recent pointing events when
    the utterance is deictic. Never raises; never modifies on failure."""
    try:
        if not isinstance(result, dict) or not result.get("ok"):
            return result
        text = str(result.get("transcript") or "")
        if not text or not utterance_is_deictic(text):
            return result
        anchor = result.get("wake_ts")
        events = recent_pointing_events(float(anchor) if anchor else None)
        if events:
            result["pointing"] = {
                "note": ("he pointed recently — these are the ledger events "
                         "nearest this utterance; use pointing_ledger "
                         "action=look to look along the ray"),
                "events": events,
            }
    except Exception:
        pass
    return result
