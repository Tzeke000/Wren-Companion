"""brain/windows_use/navigation_guards.py — Task B4.

Two-tier File Explorer guards:

    Tier 1 (preventive): on navigate() to a sensitive prefix, emit a
    confirmation prompt (once per session per prefix). Only proceeds if
    user confirms.

    Tier 2 (escalated): on navigate() to a deny-list path, refuse +
    back-out (Alt+Up) any open Explorer window at that path. No
    override.

See docs/WINDOWS_USE_INTEGRATION.md §7.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from brain.windows_use import deny_list, primitives


def _alerted_session_set(g: dict[str, Any]) -> dict[str, float]:
    """Per-session map: sensitive_prefix → timestamp_alerted.
    If older than 30 minutes the prefix re-alerts."""
    if "_windows_use_alerted" not in g or not isinstance(g.get("_windows_use_alerted"), dict):
        g["_windows_use_alerted"] = {}
    return g["_windows_use_alerted"]


def check_navigation(path: str, g: dict[str, Any]) -> dict[str, Any]:
    """Check whether navigate(path) is permitted.

    Returns:
        {
            "tier": "allow" | "tier1" | "tier2",
            "reason": str | None,
            "matched_prefix": str | None,
            "alert_required": bool,        # True iff tier1 and not yet alerted
            "back_out_required": bool,     # True iff tier2 and explorer is at path
        }
    """
    if not path:
        return {"tier": "allow", "reason": None, "matched_prefix": None,
                "alert_required": False, "back_out_required": False}

    # Tier 2 first: any path under deny-list is non-overridable.
    blocked, reason = deny_list.is_protected_for_write(path, g)
    if blocked:
        return {
            "tier": "tier2",
            "reason": reason,
            "matched_prefix": None,
            "alert_required": False,
            "back_out_required": True,
        }
    blocked_read, reason_read = deny_list.is_protected_for_read(path, g)
    if blocked_read:
        return {
            "tier": "tier2",
            "reason": reason_read,
            "matched_prefix": None,
            "alert_required": False,
            "back_out_required": True,
        }

    # Tier 1: sensitive prefix that's not in the deny-list.
    sensitive, prefix = deny_list.is_sensitive_prefix(path, g)
    if sensitive:
        alerted_map = _alerted_session_set(g)
        last = float(alerted_map.get(prefix or "", 0.0))
        # 30-minute re-alert window; otherwise once-per-session per prefix.
        re_alert_after = 30 * 60
        alert_required = (time.time() - last) > re_alert_after
        return {
            "tier": "tier1",
            "reason": "sensitive_prefix",
            "matched_prefix": prefix,
            "alert_required": alert_required,
            "back_out_required": False,
        }

    return {"tier": "allow", "reason": None, "matched_prefix": None,
            "alert_required": False, "back_out_required": False}


def mark_alerted(prefix: str | None, g: dict[str, Any]) -> None:
    if not prefix:
        return
    _alerted_session_set(g)[prefix] = time.time()


def execute_back_out(path: str) -> bool:
    """Tier 2 escalation: if an Explorer window is at `path`, back it out."""
    return primitives.back_out_explorer_window(path)
