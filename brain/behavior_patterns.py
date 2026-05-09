"""brain/behavior_patterns.py — Pattern inference from behavior (B2).

Watching Zeke over time, Ava notices patterns:
  - Opens Spotify around 9am most weekdays
  - Goes quiet between 11pm and 2am (focus time)
  - Asks similar weather/calendar questions Mondays
  - Switches between code editor and Discord when stuck

These aren't preferences (B6) — they're observations. Surfacing
them is a careful move: too eager and Ava becomes creepy ("you're
doing your morning routine again"); too quiet and the observation
is wasted.

This module ACCUMULATES the observations and exposes them through
two channels:

  1. count_pattern(domain, label) — bumps a counter; pattern emerges
     when N >= threshold within a relevant window
  2. is_pattern_active(domain, label) — query at decision points
     ("should I greet him with morning-routine context?")

Domains today:
  app_open      — which apps he opens, hour-of-day quantized
  topic_query   — which topics he asks about, day-of-week quantized
  quiet_window  — when he's away from the keyboard
  switch_chain  — which app sequences happen ("editor → discord → editor")

Bootstrap-friendly: empty by default. First weeks observe without
acting. After 2+ weeks of data, patterns start emerging. After 4+
weeks, Ava can use them in conversation.

Storage: state/behavior_patterns.json (PERSISTENT — represents
slow accumulating model of how Zeke moves through the day)

API:
    from brain.behavior_patterns import (
        count_pattern, is_pattern_active, top_patterns,
        pattern_summary,
    )

    count_pattern("app_open", "spotify", hour=9, dow="mon")

    if is_pattern_active("app_open", "spotify", min_count=4, window_days=14):
        # offer morning-routine framing
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PatternObservation:
    domain: str
    label: str
    hour: int  # 0..23, or -1 if not relevant
    dow: str  # "mon".."sun" or ""
    count: int = 0
    last_observed_ts: float = 0.0
    first_observed_ts: float = 0.0


_lock = threading.RLock()
_base_dir: Path | None = None
# (domain, label, hour, dow) -> observation
_observations: dict[tuple[str, str, int, str], PatternObservation] = {}
_STATE_FILE = "behavior_patterns.json"


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / _STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _key(domain: str, label: str, hour: int, dow: str) -> tuple[str, str, int, str]:
    return (domain.strip().lower(), label.strip().lower(), int(hour), dow.strip().lower())


def _load_locked() -> None:
    global _observations
    p = _path()
    if p is None or not p.exists():
        _observations = {}
        return
    out: dict[tuple[str, str, int, str], PatternObservation] = {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            for k, v in d.items():
                if not isinstance(v, dict):
                    continue
                domain = str(v.get("domain") or "")
                label = str(v.get("label") or "")
                hour = int(v.get("hour") or -1)
                dow = str(v.get("dow") or "")
                obs = PatternObservation(
                    domain=domain, label=label, hour=hour, dow=dow,
                    count=int(v.get("count") or 0),
                    last_observed_ts=float(v.get("last_observed_ts") or 0.0),
                    first_observed_ts=float(v.get("first_observed_ts") or 0.0),
                )
                out[_key(domain, label, hour, dow)] = obs
    except Exception as e:
        print(f"[behavior_patterns] load error: {e!r}")
    _observations = out


def _persist_locked() -> None:
    p = _path()
    if p is None:
        return
    try:
        d: dict[str, dict[str, Any]] = {}
        for (dom, lab, hr, dw), obs in _observations.items():
            key_str = f"{dom}::{lab}::{hr}::{dw}"
            d[key_str] = {
                "domain": obs.domain, "label": obs.label,
                "hour": obs.hour, "dow": obs.dow,
                "count": obs.count,
                "last_observed_ts": obs.last_observed_ts,
                "first_observed_ts": obs.first_observed_ts,
            }
        p.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[behavior_patterns] save error: {e!r}")


def count_pattern(
    domain: str,
    label: str,
    *,
    hour: int = -1,
    dow: str = "",
) -> None:
    """Bump the counter for this observation."""
    if not domain or not label:
        return
    k = _key(domain, label, hour, dow)
    now = time.time()
    with _lock:
        existing = _observations.get(k)
        if existing is None:
            _observations[k] = PatternObservation(
                domain=domain, label=label, hour=hour, dow=dow,
                count=1, last_observed_ts=now, first_observed_ts=now,
            )
        else:
            existing.count += 1
            existing.last_observed_ts = now
        _persist_locked()


def is_pattern_active(
    domain: str,
    label: str,
    *,
    hour: int | None = None,
    dow: str | None = None,
    min_count: int = 4,
    window_days: float = 14.0,
) -> bool:
    """Has this pattern been observed enough recently?"""
    cutoff = time.time() - window_days * 86400
    matched = 0
    with _lock:
        for k, obs in _observations.items():
            if obs.domain.lower() != domain.strip().lower():
                continue
            if obs.label.lower() != label.strip().lower():
                continue
            if hour is not None and obs.hour != hour:
                continue
            if dow is not None and obs.dow != dow.strip().lower():
                continue
            if obs.last_observed_ts < cutoff:
                continue
            matched += obs.count
    return matched >= min_count


def top_patterns(
    *,
    domain: str | None = None,
    limit: int = 10,
    window_days: float = 14.0,
) -> list[dict[str, Any]]:
    """Top observed patterns in the recent window."""
    cutoff = time.time() - window_days * 86400
    with _lock:
        candidates = [
            obs for obs in _observations.values()
            if obs.last_observed_ts >= cutoff
            and (domain is None or obs.domain.lower() == domain.strip().lower())
        ]
    candidates.sort(key=lambda o: o.count, reverse=True)
    return [
        {
            "domain": o.domain, "label": o.label,
            "hour": o.hour, "dow": o.dow, "count": o.count,
            "last_observed_ts": o.last_observed_ts,
            "first_observed_ts": o.first_observed_ts,
        }
        for o in candidates[:limit]
    ]


def observe_now(domain: str, label: str) -> None:
    """Convenience — observes the pattern with current hour + dow."""
    import datetime as _dt
    now = _dt.datetime.now()
    dow = now.strftime("%a").lower()  # "mon", "tue", etc
    count_pattern(domain, label, hour=now.hour, dow=dow)


def pattern_hint(person_id: str | None = None) -> str:
    """Build a system-prompt fragment from active patterns.

    Bootstrap-friendly: returns "" until enough observations land.
    """
    import datetime as _dt
    now = _dt.datetime.now()
    dow = now.strftime("%a").lower()
    hour = now.hour

    fragments: list[str] = []
    with _lock:
        recent_apps = [
            obs for obs in _observations.values()
            if obs.domain == "app_open"
            and obs.dow == dow
            and abs(obs.hour - hour) <= 1
            and obs.count >= 4
        ]
    if recent_apps:
        recent_apps.sort(key=lambda o: o.count, reverse=True)
        labels = [o.label for o in recent_apps[:3]]
        fragments.append(f"BEHAVIOR PATTERN: Around this hour on {dow}, Zeke usually opens {', '.join(labels)}.")
    if not fragments:
        return ""
    return "\n".join(fragments)


def pattern_summary() -> dict[str, Any]:
    with _lock:
        items = list(_observations.values())
    by_domain: dict[str, int] = {}
    total_count = 0
    for o in items:
        by_domain[o.domain] = by_domain.get(o.domain, 0) + o.count
        total_count += o.count
    return {
        "total_observations": total_count,
        "unique_patterns": len(items),
        "by_domain": by_domain,
    }
