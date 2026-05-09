"""
Per-person chat session cadence (gaps between visits) for natural timing.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

CADENCE_SESSION_GAP_HOURS = 4.0

DEFAULT_CADENCE: dict[str, Any] = {
    "avg_days_between_sessions": 0.0,
    "longest_gap_days": 0.0,
    "total_sessions": 0,
    "last_gap_days": 0.0,
}


def update_cadence_on_visit(profile: dict) -> dict:
    """Call on each user turn; bumps session stats when gap exceeds CADENCE_SESSION_GAP_HOURS."""
    now = datetime.now()
    raw_prev = profile.get("last_activity_at")
    prev: datetime | None = None
    if raw_prev:
        try:
            prev = datetime.fromisoformat(str(raw_prev).replace("Z", "")[:19])
        except Exception:
            prev = None

    cadence = dict(profile.get("cadence") or DEFAULT_CADENCE)
    for k, v in DEFAULT_CADENCE.items():
        cadence.setdefault(k, v)
    gap_count = int(cadence.get("_gap_count", 0) or 0)
    sum_gaps = float(cadence.get("_sum_gap_days", 0.0) or 0.0)

    if prev is None:
        cadence["total_sessions"] = max(1, int(cadence.get("total_sessions", 0) or 0) or 1)
        cadence["last_gap_days"] = 0.0
    else:
        delta_sec = (now - prev).total_seconds()
        if delta_sec > CADENCE_SESSION_GAP_HOURS * 3600:
            gap_days = delta_sec / 86400.0
            gap_count += 1
            sum_gaps += gap_days
            cadence["_gap_count"] = gap_count
            cadence["_sum_gap_days"] = round(sum_gaps, 4)
            cadence["avg_days_between_sessions"] = round(sum_gaps / gap_count, 2) if gap_count else 0.0
            cadence["longest_gap_days"] = round(
                max(float(cadence.get("longest_gap_days", 0.0) or 0.0), gap_days), 2
            )
            cadence["last_gap_days"] = round(gap_days, 2)
            cadence["total_sessions"] = int(cadence.get("total_sessions", 0) or 0) + 1

    profile["cadence"] = cadence
    profile["last_activity_at"] = now.isoformat(timespec="seconds")
    return profile


def should_offer_long_absence_checkin(cadence: dict | None) -> bool:
    """True when the last return gap was unusually long vs this person's history."""
    if not cadence:
        return False
    last_gap = float(cadence.get("last_gap_days", 0.0) or 0.0)
    avg = float(cadence.get("avg_days_between_sessions", 0.0) or 0.0)
    total = int(cadence.get("total_sessions", 0) or 0)
    if total < 3 or last_gap < 1.0:
        return False
    threshold = max(4.0, 1.75 * max(avg, 0.35))
    return last_gap >= threshold
