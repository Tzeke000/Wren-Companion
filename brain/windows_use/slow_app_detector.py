"""brain/windows_use/slow_app_detector.py — Task B6.

Heuristic: classify the state of a launching app so the orchestrator
knows whether to keep waiting (and narrate) or escalate to the next
strategy. See docs/WINDOWS_USE_INTEGRATION.md §9.
"""
from __future__ import annotations

import time

from brain.windows_use import primitives


# Classifications.
STARTING = "starting"
SLOW_BUT_WORKING = "slow_but_working"
VERY_SLOW_STILL_WORKING = "very_slow_still_working"
FAILED_NO_WINDOW = "failed_no_window"
STARTING_UNRESPONSIVE = "starting_unresponsive"
HUNG = "hung"


def classify_app_state(
    *,
    name: str,
    started_at: float,
    estimate_seconds: float,
) -> tuple[str, dict]:
    """Classify the app's current state.

    Returns (classification, info_dict).
    info_dict contains "window" (control or None) and "responsive" (bool).
    """
    elapsed = time.time() - started_at
    candidate = primitives.find_window_by_title_substring(name, timeout=0.3)

    info = {"window": candidate, "responsive": None, "elapsed": elapsed}

    if candidate is None:
        if elapsed < estimate_seconds:
            return STARTING, info
        return FAILED_NO_WINDOW, info

    # Window exists — check responsiveness.
    handle = getattr(candidate, "NativeWindowHandle", 0) or 0
    responsive = primitives.is_app_responsive(int(handle)) if handle else True
    info["responsive"] = responsive

    if responsive:
        if elapsed < estimate_seconds * 2:
            return SLOW_BUT_WORKING, info
        return VERY_SLOW_STILL_WORKING, info

    # Not responsive.
    if elapsed < estimate_seconds:
        return STARTING_UNRESPONSIVE, info
    return HUNG, info


def is_terminal(classification: str) -> bool:
    """True if this classification means the orchestrator should stop
    waiting (either succeed-and-narrate or escalate)."""
    return classification in (FAILED_NO_WINDOW, HUNG)


def is_success_so_far(classification: str) -> bool:
    """True if the app appears to be running, even if slowly."""
    return classification in (SLOW_BUT_WORKING, VERY_SLOW_STILL_WORKING)
