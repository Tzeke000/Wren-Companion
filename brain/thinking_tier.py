"""Thinking-tier coordinator — emit honest signals only when computation requires.

Component 2 of conversational naturalness (see docs/CONVERSATIONAL_DESIGN.md).
Research basis: Gonzales 2025, arXiv:2508.11781 — fillers improve perceived
naturalness ONLY when gated on real compute latency.

Tiers:
  0 = idle (no turn in progress)
  1 = streaming normally — no signal emitted
  2 = brief inter-chunk gap — short filler ("um")               [DEFERRED]
  3 = first chunk slow (>2s) — proactive "give me a second"
  4 = sustained processing (>5s) — explicit reason             [DEFERRED]

Tier 1 and Tier 3 are implemented in this first cut. Tier 2 and Tier 4
need live-audio validation to tune emission timing without sounding robotic;
deferred to a hardware-testing session.

Usage:
    coord = TierCoordinator(g, t_start, llm_label="fast:ava-personal")
    coord.start()
    try:
        for chunk in stream:
            ...
            coord.mark_chunk()
    finally:
        coord.stop()
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional


# Tier 3 trigger: how long to wait for the first chunk before emitting a
# proactive signal. Sub-2s requests should never trigger this — those are
# Tier 1 (the default).
_TIER_3_THRESHOLD_SEC = 2.0

# Tier 4 trigger: still computing this far into the turn. Currently unused
# (deferred); kept for symmetry with the design doc.
_TIER_4_THRESHOLD_SEC = 5.0

# Polling interval for the watchdog thread.
_WATCHDOG_INTERVAL_SEC = 0.1

# Default Tier 3 signal text.
_TIER_3_SIGNAL = "Give me a second."


class TierCoordinator:
    """Tracks per-turn timing; emits tier signals when computation lags.

    The watchdog thread runs alongside run_ava's streaming loop. The main
    thread calls mark_chunk() each time a sentence emits; the watchdog
    decides whether to push a filler signal into the TTS queue.

    Thread-safety: the watchdog runs in its own thread. mark_chunk() and
    stop() are safe to call from the streaming-loop thread.
    """

    def __init__(
        self,
        g: dict[str, Any],
        t_start: float,
        llm_label: str = "fast",
        tier_3_signal: str = _TIER_3_SIGNAL,
        emotion: str = "neutral",
        intensity: float = 0.5,
    ) -> None:
        self._g = g
        self._t_start = float(t_start)
        self._llm_label = str(llm_label)
        self._tier_3_signal = str(tier_3_signal)
        self._emotion = str(emotion)
        self._intensity = float(intensity)

        self._first_chunk_ts: Optional[float] = None
        self._last_chunk_ts: float = float(t_start)
        self._tier_emitted: int = 1  # default to Tier 1
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

        # Publish initial tier so UI sees something coherent.
        self._publish_tier(1)

    # ── public API ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the watchdog thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._watchdog,
            daemon=True,
            name="ava-tier-watchdog",
        )
        self._thread.start()

    def mark_chunk(self) -> None:
        """Called by the streaming loop each time a sentence emits."""
        now = time.time()
        with self._lock:
            if self._first_chunk_ts is None:
                self._first_chunk_ts = now
            self._last_chunk_ts = now

    def stop(self) -> None:
        """Stop the watchdog. Idempotent."""
        self._stop_evt.set()
        # Reset published tier to idle so UI doesn't get stuck.
        self._publish_tier(0)

    # ── internals ──────────────────────────────────────────────────────────

    def _publish_tier(self, tier: int) -> None:
        """Update the snapshot field so UI can react."""
        try:
            self._g["_thinking_tier"] = int(tier)
        except Exception:
            pass

    def _emit_filler(self, text: str) -> None:
        """Push a filler chunk into the TTS queue.

        Returns immediately; the worker plays it before the next real chunk.
        """
        worker = self._g.get("_tts_worker")
        if worker is None:
            return
        speak = getattr(worker, "speak", None)
        if not callable(speak):
            return
        try:
            speak(
                text,
                emotion=self._emotion,
                intensity=self._intensity,
                blocking=False,
            )
            print(f"[tier] emitted tier-3 filler: {text!r} (label={self._llm_label})")
        except Exception as e:
            print(f"[tier] filler emit error: {e!r}")

    def _watchdog(self) -> None:
        while not self._stop_evt.wait(_WATCHDOG_INTERVAL_SEC):
            with self._lock:
                first_ts = self._first_chunk_ts
                tier = self._tier_emitted

            now = time.time()
            elapsed = now - self._t_start

            # Tier 3 — first chunk hasn't arrived yet, and we've waited too long.
            if first_ts is None and elapsed >= _TIER_3_THRESHOLD_SEC and tier < 3:
                with self._lock:
                    if self._first_chunk_ts is None and self._tier_emitted < 3:
                        self._tier_emitted = 3
                        # Publish + emit OUTSIDE the lock to avoid holding it
                        # during TTS enqueue. Re-read here to capture the state.
                        emit_now = True
                    else:
                        emit_now = False
                if emit_now:
                    self._publish_tier(3)
                    self._emit_filler(self._tier_3_signal)
                continue

            # Tier 4 deferred — design space exists but emission semantics
            # need hardware tuning. See CONVERSATIONAL_DESIGN.md.

            # If the first chunk has arrived, we're streaming normally (Tier 1).
            # Tier 2 (mid-stream filler on inter-chunk gaps) deferred — same reason.
            if first_ts is not None and tier > 1 and tier != 3:
                # Once first chunk arrives, stay at the elevated tier we
                # already emitted (don't downgrade — the signal was honest).
                pass

            # Reset to Tier 1 only if no signal was emitted and chunks are flowing.
            if first_ts is not None and tier == 1:
                self._publish_tier(1)
