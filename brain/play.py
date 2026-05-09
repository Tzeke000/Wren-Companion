"""brain/play.py — Capacity for play (D19), lifecycle-gated.

Ava can be playful — wordplay, gentle teasing, improv-flavored
responses, calling back to inside jokes, occasional whimsy.
Importantly: ONLY when the moment is right.

The lifecycle.is_play_allowed() gate enforces this. Play is allowed
when state is drifting OR in_conversation OR in_play. NEVER during:
  - booting
  - focused_on_task (Zeke is mid-build, mid-debug, mid-task)
  - sleeping / dreaming
  - error_recovering
  - alive_attentive (default — would be too eager)

Within allowed states, play STILL only fires when something gives it
permission: a humor signal in the user's input, an established
inside joke (shared_lexicon match), a recent anchor moment of kind
"humor", or sustained casual register over the last few turns.

Bootstrap-friendly: no preset jokes, no canned bits. Play emerges
from accumulated shared_lexicon, theory_of_mind, mood, and explicit
humor signals. First weeks: nothing. Once Zeke and Ava have a few
shared phrases, occasional callbacks; once a few anchor humor moments,
slightly more confidence.

Storage: state/play_signals.jsonl (PERSISTENT, audit log of when
play actually fired and why)

API:
    from brain.play import (
        playful_register_hint, should_play_now, record_play_event,
        playfulness_summary,
    )

    if should_play_now(g, user_input):
        # the prompt builder can append a playful-register hint to the
        # system prompt for this turn only
        sys_prompt += playful_register_hint(g, person_id)
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PlayEvent:
    ts: float
    person_id: str
    trigger: str  # "humor_signal" | "shared_lexicon_callback" | "casual_run" | "explicit"
    note: str = ""


_lock = threading.RLock()
_base_dir: Path | None = None
_events: list[PlayEvent] = []
_MAX_EVENTS = 500

# Heuristic humor signals — phrasing that suggests Zeke is in a casual register.
_HUMOR_PATTERNS = [
    re.compile(r"\bhaha+\b", re.IGNORECASE),
    re.compile(r"\blol\b", re.IGNORECASE),
    re.compile(r"\blmao\b", re.IGNORECASE),
    re.compile(r"😂|🤣|😄|😆|😜"),
    re.compile(r"\bjoking\b", re.IGNORECASE),
    re.compile(r"\bjk\b", re.IGNORECASE),
    re.compile(r"\bsilly\b", re.IGNORECASE),
    re.compile(r"\bbrutal\b", re.IGNORECASE),
]


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "play_signals.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _events
    p = _path()
    if p is None or not p.exists():
        _events = []
        return
    out: list[PlayEvent] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(PlayEvent(
                        ts=float(d.get("ts") or 0.0),
                        person_id=str(d.get("person_id") or ""),
                        trigger=str(d.get("trigger") or ""),
                        note=str(d.get("note") or ""),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[play] load error: {e!r}")
    _events = out[-_MAX_EVENTS:]


def _append(ev: PlayEvent) -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": ev.ts, "person_id": ev.person_id,
                "trigger": ev.trigger, "note": ev.note,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[play] append error: {e!r}")


def _has_humor_signal(text: str) -> bool:
    if not text:
        return False
    for pat in _HUMOR_PATTERNS:
        if pat.search(text):
            return True
    return False


def _has_shared_lexicon_match(person_id: str, text: str) -> bool:
    """Did the user invoke a known inside joke / shared term?"""
    try:
        from brain.shared_lexicon import contains_shared_term
        hits = contains_shared_term(person_id, text)
        return bool(hits)
    except Exception:
        return False


def _has_recent_humor_anchor(person_id: str, *, hours: float = 72.0) -> bool:
    """Has there been a humor anchor moment with this person recently?"""
    try:
        from brain.anchor_moments import list_anchors
        cutoff = time.time() - hours * 3600
        anchors = list_anchors() or []
        for a in anchors:
            if (a.get("kind") == "humor"
                    and a.get("person_id") == person_id
                    and float(a.get("ts") or 0.0) >= cutoff):
                return True
    except Exception:
        pass
    return False


def _casual_run_active(g: dict[str, Any], *, last_n_turns: int = 4) -> bool:
    """Have the last few turns been casual / not-task-focused?"""
    try:
        wm_get = g.get("_working_memory_state")
        if isinstance(wm_get, dict):
            active = wm_get.get("active_tasks") or []
            if active:
                return False
    except Exception:
        pass
    try:
        recent = list(g.get("_history_manager_recent") or [])
        casual_words = {"haha", "lol", "yeah", "cool", "nice", "fun", "huh"}
        if recent[-last_n_turns:]:
            joined = " ".join(str(r.get("content") or "") for r in recent[-last_n_turns:]).lower()
            return any(w in joined for w in casual_words)
    except Exception:
        pass
    return False


def should_play_now(g: dict[str, Any], user_input: str, *, person_id: str | None = None) -> tuple[bool, str]:
    """Should Ava take a playful register on this turn?

    Returns (allowed, trigger_reason). The lifecycle gate is hard —
    if play is not allowed by the lifecycle state, returns (False, "")
    immediately. Otherwise returns True iff at least one play
    signal fires.
    """
    try:
        from brain.lifecycle import is_play_allowed
        if not is_play_allowed():
            return False, ""
    except Exception:
        return False, ""

    pid = str(person_id or g.get("_active_person_id") or "zeke")

    if _has_humor_signal(user_input):
        return True, "humor_signal"
    if _has_shared_lexicon_match(pid, user_input):
        return True, "shared_lexicon_callback"
    if _has_recent_humor_anchor(pid):
        if _casual_run_active(g):
            return True, "casual_run_with_humor_history"
    return False, ""


def record_play_event(person_id: str, trigger: str, note: str = "") -> None:
    """Log that play actually fired this turn."""
    ev = PlayEvent(
        ts=time.time(),
        person_id=person_id,
        trigger=trigger,
        note=(note or "")[:200],
    )
    with _lock:
        _events.append(ev)
        _events[:] = _events[-_MAX_EVENTS:]
        _append(ev)


def playful_register_hint(g: dict[str, Any], person_id: str | None = None) -> str:
    """System-prompt fragment for a playful turn.

    Bootstrap-friendly: minimal hint, lets Ava's accumulated voice
    do the work. No canned jokes. No personality template injection.
    """
    pid = str(person_id or g.get("_active_person_id") or "zeke")
    fragments = ["This turn is in a playful register — light, quick, allowed to be a little silly. Stay you."]
    try:
        from brain.shared_lexicon import shared_lexicon_hint
        lx = shared_lexicon_hint(pid)
        if lx:
            fragments.append(f"Inside terms in play: {lx}")
    except Exception:
        pass
    return "\n".join(fragments)


def playfulness_summary() -> dict[str, Any]:
    with _lock:
        events = list(_events)
    by_trigger: dict[str, int] = {}
    by_person: dict[str, int] = {}
    last_24h = 0
    cutoff = time.time() - 86400
    for ev in events:
        by_trigger[ev.trigger] = by_trigger.get(ev.trigger, 0) + 1
        by_person[ev.person_id] = by_person.get(ev.person_id, 0) + 1
        if ev.ts >= cutoff:
            last_24h += 1
    return {
        "total_events": len(events),
        "last_24h": last_24h,
        "by_trigger": by_trigger,
        "by_person": by_person,
    }
