"""brain/lifecycle.py — High-level lifecycle state machine (architecture #8).

The voice_loop already has its own state machine (passive / attentive /
listening / thinking / speaking). That's about audio capture state,
not about Ava's overall situation.

This module adds a HIGHER-LEVEL state — what Ava is BROADLY doing
right now — that other subsystems consult to decide HOW to behave:

  booting         — startup in progress, subsystems still initializing
  alive_attentive — normal operating state, waiting for input
  focused_on_task — working a complex action / build / sustained reply
  drifting        — idle, soft attention, thinking-on-her-own
  in_conversation — active multi-turn exchange in progress
  in_play         — improv / collaborative-fiction / non-task register (gated to idle)
  sleeping        — sleep_mode active (4-min nap or longer)
  dreaming        — sleep cycle running consolidation/dream generation
  error_recovering — recovering from a subsystem failure

Subsystems consult `current_state()` to decide:
- TTS volume / pace
- Eagerness to respond proactively
- Whether to interrupt the user
- Whether play-mode is allowed (only if idle/casual — gated by Zeke's
  rule that play shouldn't override active tasks)
- Whether the phenomenal-continuity tick fires (if/when D1 lands)

State transitions today are MANUAL (callers explicitly transition).
Future work can add automatic transitions based on signal_bus events
(e.g. focused_on_task → alive_attentive when the action completes).

Today's behavior change: zero. Module exists, default state is
alive_attentive, no callers modify it yet. Future modules consult
the state and adapt.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Literal


LifecycleState = Literal[
    "booting",
    "alive_attentive",
    "focused_on_task",
    "drifting",
    "in_conversation",
    "in_play",
    "sleeping",
    "dreaming",
    "error_recovering",
]


# Recommended behavior hints per state — subsystems consult these.
BEHAVIOR_HINTS: dict[LifecycleState, dict[str, Any]] = {
    "booting": {
        "respond_to_voice": False,
        "tts_enabled": False,
        "proactive_speech": False,
        "play_mode_allowed": False,
        "description": "Subsystems still initializing — don't engage yet.",
    },
    "alive_attentive": {
        "respond_to_voice": True,
        "tts_enabled": True,
        "proactive_speech": False,  # only after silence threshold
        "play_mode_allowed": False,
        "description": "Normal operating state, waiting for input.",
    },
    "focused_on_task": {
        "respond_to_voice": True,
        "tts_enabled": True,
        "proactive_speech": False,
        "play_mode_allowed": False,
        "description": "Working a complex action — interruptions discouraged.",
    },
    "drifting": {
        "respond_to_voice": True,
        "tts_enabled": True,
        "proactive_speech": True,
        "play_mode_allowed": True,
        "description": "Idle, soft attention, thinking on own. Open to play / proactive remarks.",
    },
    "in_conversation": {
        "respond_to_voice": True,
        "tts_enabled": True,
        "proactive_speech": False,
        "play_mode_allowed": True,  # play allowed within an active conversation
        "description": "Active multi-turn exchange in progress.",
    },
    "in_play": {
        "respond_to_voice": True,
        "tts_enabled": True,
        "proactive_speech": True,
        "play_mode_allowed": True,
        "description": "Play mode — improv / collab fiction / non-task register.",
    },
    "sleeping": {
        "respond_to_voice": False,  # voice loop muted during nap
        "tts_enabled": False,
        "proactive_speech": False,
        "play_mode_allowed": False,
        "description": "Sleep mode active (4-min nap or longer). Voice loop muted.",
    },
    "dreaming": {
        "respond_to_voice": False,
        "tts_enabled": False,
        "proactive_speech": False,
        "play_mode_allowed": False,
        "description": "Consolidation / dream generation running during sleep.",
    },
    "error_recovering": {
        "respond_to_voice": True,
        "tts_enabled": True,
        "proactive_speech": False,
        "play_mode_allowed": False,
        "description": "Recovering from subsystem failure — degraded operation.",
    },
}


class Lifecycle:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: LifecycleState = "booting"
        self._since_ts: float = time.time()
        self._history: list[dict[str, Any]] = []
        self._listeners: list = []  # callable(old, new) → None

    def current(self) -> LifecycleState:
        with self._lock:
            return self._state

    def hint(self, key: str, default: Any = None) -> Any:
        """Look up a behavior hint for the current state.

        e.g. hint("play_mode_allowed") → True/False
        """
        with self._lock:
            return BEHAVIOR_HINTS.get(self._state, {}).get(key, default)

    def hints(self) -> dict[str, Any]:
        with self._lock:
            return dict(BEHAVIOR_HINTS.get(self._state, {}))

    def time_in_state(self) -> float:
        with self._lock:
            return time.time() - self._since_ts

    def transition(self, new_state: LifecycleState, *, reason: str = "") -> None:
        """Move to a new lifecycle state."""
        if new_state not in BEHAVIOR_HINTS:
            print(f"[lifecycle] unknown state {new_state!r} — ignoring transition")
            return
        with self._lock:
            old = self._state
            if old == new_state:
                return
            self._state = new_state
            self._since_ts = time.time()
            entry = {
                "from": old,
                "to": new_state,
                "ts": self._since_ts,
                "reason": reason or "",
            }
            self._history.append(entry)
            if len(self._history) > 200:
                self._history = self._history[-200:]
            listeners = list(self._listeners)
        # Notify listeners outside the lock
        for cb in listeners:
            try:
                cb(old, new_state)
            except Exception as e:
                print(f"[lifecycle] listener error: {e!r}")
        print(f"[lifecycle] {old} -> {new_state} ({reason or 'no reason'})")

    def on_change(self, callback) -> None:
        """Register a callback fired on every transition."""
        with self._lock:
            self._listeners.append(callback)

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)[-int(limit):]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current": self._state,
                "since_ts": self._since_ts,
                "time_in_state_seconds": time.time() - self._since_ts,
                "hints": dict(BEHAVIOR_HINTS.get(self._state, {})),
                "recent_transitions": list(self._history)[-10:],
            }


# Process-singleton.
lifecycle = Lifecycle()


# ── Convenience helpers ──────────────────────────────────────────────────


def is_play_allowed() -> bool:
    """Per Zeke's rule: play is gated to idle / casual mode only."""
    return bool(lifecycle.hint("play_mode_allowed", False))


def is_responsive() -> bool:
    return bool(lifecycle.hint("respond_to_voice", False))


def can_proactively_speak() -> bool:
    return bool(lifecycle.hint("proactive_speech", False))
