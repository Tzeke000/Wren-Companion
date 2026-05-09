"""brain/windows_use/retry_cascade.py — Task B3.

Strategy cascade for opening apps:
    1. PowerShell Start-Process
    2. UI search (Win key, type, Enter)
    3. Direct path via pywinauto
    4. Escalate (return ok=False)

Each strategy gets up to 3 attempts with exponential backoff
(250ms / 500ms / 1s). Total worst-case ~13s for full cascade with
default estimate of 8s — temporal_sense's 25%-overrun fires at ~10s
so the slow-app TTS lands while we're still trying.

Window-baseline discipline: before issuing a strategy we capture the
set of currently-visible window titles. Success requires a NEW window
to appear that wasn't in the baseline — this defeats the false-positive
where the Win-search strategy types the app name into the search bar
(which then becomes a window whose title contains the substring).

See docs/WINDOWS_USE_INTEGRATION.md §6 for the spec.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from brain.windows_use import primitives, slow_app_detector


# Window-title prefixes/substrings that should never count as a "real"
# match for an app-launch attempt — these are the OS shell's transient
# search/start surfaces.
_NON_APP_WINDOW_FILTERS = (
    "search",          # Cortana / Windows Search popups
    "start",           # Start menu surface
    "task switching",  # Alt-Tab overlay
    "program manager", # explorer's hidden root
)


def _baseline_titles() -> set[str]:
    return {w["title"].lower() for w in primitives.list_visible_windows()}


def _real_app_window_appeared(name: str, baseline: set[str]) -> bool:
    """True if a window matching `name` exists that wasn't in `baseline`
    AND isn't on the OS-shell filter list.
    """
    needle = name.lower()
    for w in primitives.list_visible_windows():
        title_l = w["title"].lower()
        if title_l in baseline:
            continue
        if any(f in title_l for f in _NON_APP_WINDOW_FILTERS):
            continue
        if needle in title_l:
            return True
    return False


STRATEGIES = ("powershell", "search", "direct_path")
ATTEMPTS_PER_STRATEGY = 3
BACKOFF_MS = (250, 500, 1000)


def _resolve_direct_path(name: str, g: dict[str, Any]) -> str | None:
    """Reuse the existing app_launcher discovery logic to find a
    canonical exe path for `name`. Falls back through the same chain:
    APP_MAP → learned mappings → discoverer fuzzy match → glob search.
    """
    try:
        from tools.system import app_launcher as al
        exe, _canonical = al._resolve_app(name)
        if exe and "{}" not in exe:
            return exe
        learned = al._check_learned(name, g)
        if learned:
            return learned
        disc = g.get("_app_discoverer")
        if disc is not None:
            try:
                entry = disc.fuzzy_match(name)
                if entry and entry.get("exe_path"):
                    return str(entry["exe_path"])
            except Exception:
                pass
        return al._filesystem_glob_search(name)
    except Exception:
        return None


def _attempt_strategy(strategy: str, name: str, g: dict[str, Any]) -> bool:
    """Run one attempt of one strategy. Returns True if the issuance
    didn't immediately fail (verification of "did the app actually
    appear" is done by the orchestrator via slow_app_detector).
    """
    if strategy == "powershell":
        return primitives.open_via_powershell(name)
    if strategy == "search":
        return primitives.open_via_search(name)
    if strategy == "direct_path":
        path = _resolve_direct_path(name, g)
        if not path:
            return False
        return primitives.open_via_direct_path(path)
    return False


def run_open_app_cascade(
    *,
    name: str,
    g: dict[str, Any],
    estimate_seconds: float,
    on_strategy_transition: Callable[[str, str], None] | None = None,
    verify_window_seconds: float = 4.0,
) -> dict[str, Any]:
    """Execute the cascade. Returns a dict:
        {
            "ok": bool,
            "strategy_used": str | None,
            "attempts": int,
            "elapsed": float,
            "window_found": bool,
            "last_classification": str | None,
            "error": str | None,
        }

    on_strategy_transition: optional callback (from_strategy, to_strategy)
    invoked between strategies so the orchestrator can emit a THOUGHT
    event and (per cooldown) narrate the transition.
    """
    started = time.time()
    last_error: str | None = None
    total_attempts = 0
    last_classification: str | None = None

    for s_idx, strategy in enumerate(STRATEGIES):
        for a_idx in range(ATTEMPTS_PER_STRATEGY):
            total_attempts += 1
            # Baseline BEFORE issuing — defeats search-bar false positives.
            baseline = _baseline_titles()
            try:
                issued = _attempt_strategy(strategy, name, g)
            except Exception as e:
                issued = False
                last_error = repr(e)

            if issued:
                # Verify window appears within verify_window_seconds AND
                # the matching window is genuinely new (not in baseline).
                deadline = time.time() + verify_window_seconds
                while time.time() < deadline:
                    cls, _info = slow_app_detector.classify_app_state(
                        name=name, started_at=started, estimate_seconds=estimate_seconds,
                    )
                    last_classification = cls
                    if cls in (
                        slow_app_detector.SLOW_BUT_WORKING,
                        slow_app_detector.VERY_SLOW_STILL_WORKING,
                    ):
                        # Window is responsive and visible. Confirm it's a
                        # NEW window before declaring victory.
                        if _real_app_window_appeared(name, baseline):
                            return {
                                "ok": True,
                                "strategy_used": strategy,
                                "attempts": total_attempts,
                                "elapsed": time.time() - started,
                                "window_found": True,
                                "last_classification": cls,
                                "error": None,
                            }
                        # Visible but it's a transient (search bar etc.) —
                        # keep waiting for the real one.
                    elif cls in (slow_app_detector.HUNG, slow_app_detector.FAILED_NO_WINDOW):
                        break
                    # else: STARTING / STARTING_UNRESPONSIVE — keep polling.
                    time.sleep(0.3)

            time.sleep(BACKOFF_MS[a_idx] / 1000.0)

        # Strategy exhausted; transition.
        if s_idx + 1 < len(STRATEGIES) and on_strategy_transition is not None:
            try:
                on_strategy_transition(strategy, STRATEGIES[s_idx + 1])
            except Exception:
                pass

    return {
        "ok": False,
        "strategy_used": None,
        "attempts": total_attempts,
        "elapsed": time.time() - started,
        "window_found": False,
        "last_classification": last_classification,
        "error": last_error or "no strategy succeeded",
    }
