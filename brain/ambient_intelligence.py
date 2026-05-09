"""
Phase 88 — Ambient intelligence. Ava passively learns your patterns without being intrusive.

AmbientIntelligence observes session context (time, active window, system stats, Ava's state)
and builds a pattern model. Ava decides what patterns are interesting to track and reference.

Wire into prompt_builder.py fast path: inject get_context_hint() when non-empty.
Wire into heartbeat: observe_session() each tick.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

_PATTERNS_PATH = "state/ambient_patterns.json"


def _patterns_path(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".") / _PATTERNS_PATH


def _load_patterns(g: dict[str, Any]) -> dict[str, Any]:
    path = _patterns_path(g)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_patterns(g: dict[str, Any], patterns: dict[str, Any]) -> None:
    path = _patterns_path(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(patterns, indent=2, ensure_ascii=False), encoding="utf-8")


def observe_session(g: dict[str, Any]) -> None:
    """
    Called each heartbeat tick. Records current session context.
    Builds pattern model over time without prescribing what patterns matter.
    """
    patterns = _load_patterns(g)
    now = time.time()
    dt = datetime.now()
    hour = dt.hour
    weekday = dt.weekday()  # 0=Monday

    # Track active hours histogram
    hourly = patterns.get("hourly_activity") or {}
    key = str(hour)
    hourly[key] = int(hourly.get(key) or 0) + 1
    patterns["hourly_activity"] = hourly

    # Track weekday activity
    weekday_activity = patterns.get("weekday_activity") or {}
    wk = str(weekday)
    weekday_activity[wk] = int(weekday_activity.get(wk) or 0) + 1
    patterns["weekday_activity"] = weekday_activity

    # Track active window patterns
    active_window = str(g.get("_active_window_title") or "").strip()
    if active_window:
        window_counts = patterns.get("window_counts") or {}
        # Truncate to first 60 chars to avoid spurious keys
        wk = active_window[:60]
        window_counts[wk] = int(window_counts.get(wk) or 0) + 1
        patterns["window_counts"] = window_counts

    # Track conversation frequency (conversation_count per day)
    today = dt.strftime("%Y-%m-%d")
    daily_conv = patterns.get("daily_conversations") or {}
    # Increment if there was a recent user interaction (within 5 min)
    last_interaction = float(g.get("_last_user_interaction_ts") or 0)
    if (now - last_interaction) < 300:
        daily_conv[today] = int(daily_conv.get(today) or 0) + 1
    # Keep only last 30 days
    all_days = sorted(daily_conv.keys())
    if len(all_days) > 30:
        for old_day in all_days[:-30]:
            del daily_conv[old_day]
    patterns["daily_conversations"] = daily_conv

    # Update last observed timestamp
    patterns["last_observed"] = now
    _save_patterns(g, patterns)


def get_context_hint(g: dict[str, Any]) -> str:
    """
    Returns a brief context hint for prompt injection based on observed patterns.
    Returns empty string if no relevant pattern.
    Ava decides what's worth mentioning — this is her interpretation of her own observations.
    """
    patterns = _load_patterns(g)
    if not patterns:
        return ""

    dt = datetime.now()
    hour = dt.hour
    weekday = dt.weekday()

    hints: list[str] = []

    # Typical active hours
    hourly = patterns.get("hourly_activity") or {}
    if hourly:
        top_hours = sorted(hourly.items(), key=lambda x: int(x[1]), reverse=True)[:3]
        top_hour_nums = [int(h) for h, _ in top_hours]
        if hour in top_hour_nums:
            hints.append(f"Zeke is usually active around this time (hour {hour})")

    # App usage pattern
    window_counts = patterns.get("window_counts") or {}
    if window_counts:
        top_window = max(window_counts.items(), key=lambda x: int(x[1]), default=("", 0))
        if top_window[0] and int(top_window[1]) >= 5:
            hints.append(f"Zeke often has '{top_window[0][:40]}' open")

    # Weekly rhythm
    weekday_activity = patterns.get("weekday_activity") or {}
    if weekday_activity:
        avg = sum(int(v) for v in weekday_activity.values()) / max(1, len(weekday_activity))
        current = int(weekday_activity.get(str(weekday)) or 0)
        if current > avg * 1.3:
            day_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][weekday]
            hints.append(f"{day_name} tends to be an active day")
        elif current < avg * 0.7 and avg > 5:
            day_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][weekday]
            hints.append(f"{day_name} is usually quieter")

    if not hints:
        return ""
    return "AMBIENT CONTEXT: " + "; ".join(hints[:2]) + "."
