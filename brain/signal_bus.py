"""
Lightweight event bus for Ava.

Replaces the old "poll every N seconds" loops with an event-driven model:
something happens, a thread fires a signal (cheap — just stores a dict),
and Ava decides during her heartbeat whether to act on it. Like peripheral
vision: aware that something moved, but doesn't turn to look unless it
matters.

Priority semantics:
  low      — record only; Ava notices on heartbeat consume()
  medium   — same; only meaning is human-readable triage
  high     — same; nudges Ava to peek sooner if she's awake
  urgent   — dispatched immediately to any registered handler in a
             daemon thread (e.g. a reminder due → speak it now)

Bootstrap-friendly: this module has no opinions about which signals matter.
The heartbeat decides.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Optional


# ── Signal type constants ─────────────────────────────────────────────────────

# Clipboard / input
SIGNAL_CLIPBOARD_CHANGED = "clipboard_changed"

# Vision
SIGNAL_FACE_APPEARED = "face_appeared"
SIGNAL_FACE_LOST = "face_lost"
SIGNAL_FACE_CHANGED = "face_changed"
SIGNAL_EXPRESSION_CHANGED = "expression_changed"
SIGNAL_ATTENTION_CHANGED = "attention_changed"

# Desktop / windowing
SIGNAL_APP_OPENED = "app_opened"
SIGNAL_APP_CLOSED = "app_closed"
SIGNAL_ACTIVE_WINDOW_CHANGED = "window_changed"
SIGNAL_SCREEN_IDLE = "screen_idle"
SIGNAL_SCREEN_ACTIVE = "screen_active"
SIGNAL_NEW_APP_INSTALLED = "app_installed"
SIGNAL_FILE_CREATED = "file_created"

# Audio
SIGNAL_VOICE_DETECTED = "voice_detected"
SIGNAL_CLAP_DETECTED = "clap_detected"

# Time / system
SIGNAL_REMINDER_DUE = "reminder_due"
SIGNAL_BATTERY_LOW = "battery_low"
SIGNAL_NETWORK_CHANGED = "network_changed"
SIGNAL_USB_CONNECTED = "usb_connected"

# Subsystem health — Task 2 (2026-05-02). Fired when a subsystem
# (camera, mem0, models, mood, initiative, tts, stt, etc.) reports a
# degraded/error/critical state. Consumed by the emotion pipeline so
# Ava's tracked mood reflects the situation when something is failing
# even if she hasn't verbalized it.
#
# data shape: {
#     "subsystem": "camera",
#     "severity":  "warning" | "error" | "critical",  # health.py severity scale
#     "message":   "VideoCapture(0) failed or not opened",
#     "kind":      "runtime" | "startup" | "light",   # which check triggered it
# }
SIGNAL_SUBSYSTEM_DEGRADED = "subsystem_degraded"


PRIORITIES = ("low", "medium", "high", "urgent")


class SignalBus:
    """
    Lightweight event bus. Signals fire when things happen.
    Ava decides whether to act on them during her heartbeat tick.
    fire() is fast (just stores a dict); only urgent signals dispatch
    handlers immediately.
    """

    def __init__(self, maxlen: int = 200) -> None:
        self._signals: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._handlers: dict[str, list[Callable[[dict[str, Any]], Any]]] = {}
        self._last_seen: dict[str, float] = {}  # signal_type → fire ts
        self._fire_count: dict[str, int] = {}

    # ── publishing ────────────────────────────────────────────────────────────

    def fire(
        self,
        signal_type: str,
        data: Optional[dict[str, Any]] = None,
        priority: str = "low",
    ) -> None:
        """Fire a signal. O(1) — store in the ring buffer + record last-seen ts.

        Only urgent signals invoke registered handlers right now.
        Everything else waits for someone to call consume() / peek().
        """
        if priority not in PRIORITIES:
            priority = "low"
        signal = {
            "type": str(signal_type),
            "ts": time.time(),
            "data": dict(data or {}),
            "priority": priority,
            "seen": False,
        }
        with self._lock:
            self._signals.append(signal)
            self._last_seen[signal_type] = signal["ts"]
            self._fire_count[signal_type] = self._fire_count.get(signal_type, 0) + 1

        if priority == "urgent":
            self._dispatch(signal)

    # ── reading ───────────────────────────────────────────────────────────────

    def peek(
        self,
        signal_type: Optional[str] = None,
        since: Optional[float] = None,
    ) -> Any:
        """Look without consuming. Like peripheral awareness.

        - peek()                         → list of all signals (ref-safe copies)
        - peek(signal_type)              → wall-clock ts of last fire (or 0.0)
        - peek(signal_type, since=ts)    → list of signals since `ts`
        """
        with self._lock:
            if signal_type and since is None:
                return float(self._last_seen.get(signal_type, 0.0))
            if signal_type and since is not None:
                return [
                    dict(s) for s in self._signals
                    if s["type"] == signal_type and s["ts"] >= since
                ]
            return [dict(s) for s in self._signals]

    def consume(
        self,
        signal_type: Optional[str] = None,
        max_age: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Take signals — marks them seen, like turning to look. Returns
        only previously-unseen signals matching the filter."""
        result: list[dict[str, Any]] = []
        cutoff = (time.time() - max_age) if max_age else None
        with self._lock:
            for s in self._signals:
                if s["seen"]:
                    continue
                if signal_type and s["type"] != signal_type:
                    continue
                if cutoff is not None and s["ts"] < cutoff:
                    continue
                s["seen"] = True
                result.append(dict(s))
        return result

    def get_unseen_count(self, priority: Optional[str] = None) -> int:
        with self._lock:
            return sum(
                1 for s in self._signals
                if not s["seen"] and (priority is None or s["priority"] == priority)
            )

    def stats(self) -> dict[str, Any]:
        with self._lock:
            unseen = sum(1 for s in self._signals if not s["seen"])
            return {
                "total_in_buffer": len(self._signals),
                "unseen": unseen,
                "fire_count": dict(self._fire_count),
                "last_seen": dict(self._last_seen),
            }

    # ── handlers ──────────────────────────────────────────────────────────────

    def register_urgent_handler(
        self,
        signal_type: str,
        fn: Callable[[dict[str, Any]], Any],
    ) -> None:
        """Register a synchronous handler for urgent signals.

        Called from a daemon thread immediately when the signal fires.
        Most signals don't need this — Ava reads them at heartbeat time.
        """
        with self._lock:
            self._handlers.setdefault(signal_type, []).append(fn)

    def _dispatch(self, signal: dict[str, Any]) -> None:
        with self._lock:
            handlers = list(self._handlers.get(signal["type"], []))
        for fn in handlers:
            try:
                threading.Thread(
                    target=fn,
                    args=(signal,),
                    daemon=True,
                    name=f"signal-{signal['type']}",
                ).start()
            except Exception as e:
                print(f"[signal_bus] handler error: {e}")


# ── singleton + bootstrap ─────────────────────────────────────────────────────

_SINGLETON: Optional[SignalBus] = None
_LOCK = threading.Lock()


def get_signal_bus() -> Optional[SignalBus]:
    return _SINGLETON


def bootstrap_signal_bus(g: dict[str, Any]) -> SignalBus:
    """Create the bus, store in globals. Idempotent."""
    global _SINGLETON
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = SignalBus()
    g["_signal_bus"] = _SINGLETON
    return _SINGLETON
