"""brain/physical_context.py — Sense of physical/temporal context (D20).

Beyond clock time. Real presence in the world includes:

- Time-of-day (morning, afternoon, evening, night, late-night)
- Day-of-week / weekend vs weekday
- Season (Northern Hemisphere)
- Weather (uses tools/web/weather wttr.in)
- Light level estimate (from camera average brightness if available)
- Ambient noise / activity (from signal_bus events recently)

Why: grounds Ava's presence in the actual world, not just bytes. "It's
been raining all afternoon" / "feels like the kind of evening to talk
slower" / "first nice weekend in a while."

API:

    from brain.physical_context import (
        snapshot, time_of_day, season, hint_for_introspection,
    )

    s = snapshot()
    # s = {"time_of_day": "evening", "season": "spring", "is_weekend": True,
    #       "hour": 19, "weather": "...", ...}

    hint = hint_for_introspection()
    # -> "It's evening on a spring Saturday. Weather: <wttr text>."
"""
from __future__ import annotations

import datetime as dt
from typing import Any


def time_of_day(now: dt.datetime | None = None) -> str:
    """Bucket the current hour into a phase-of-day word."""
    h = (now or dt.datetime.now()).hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    if 21 <= h < 24:
        return "night"
    if 0 <= h < 5:
        return "late-night"
    return "midday"


def season(now: dt.datetime | None = None) -> str:
    """Approximate Northern Hemisphere season."""
    m = (now or dt.datetime.now()).month
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    if m in (9, 10, 11):
        return "fall"
    return "winter"


def is_weekend(now: dt.datetime | None = None) -> bool:
    return (now or dt.datetime.now()).weekday() >= 5


def day_of_week(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now()).strftime("%A")


def _try_weather() -> str:
    """Best-effort weather — CACHE-ONLY from the prompt-build hot path.

    Latency fix (2026-05-06): the underlying _fetch_weather_text uses
    requests.get with an 8-second timeout. Calling it from inside
    build_prompt (which fires every deep-path turn) means a cold cache
    burns up to 8s of synchronous wait BEFORE the LLM call even starts.

    This wrapper only returns a value when the cache is already warm.
    A separate background refresh (scheduled by startup) keeps the
    cache fresh without blocking turn-time.
    """
    try:
        # Read the cache directly without triggering a network fetch.
        from tools.web import weather as _w
        import time as _t
        cached = _w._CACHE.get("value")
        cached_ts = float(_w._CACHE.get("ts") or 0.0)
        if cached and cached_ts > 0 and (_t.time() - cached_ts) < _w._CACHE_TTL_SEC:
            return str(cached).strip()
    except Exception:
        pass
    return ""


def refresh_weather_cache_background() -> None:
    """Trigger a fresh weather fetch in a background thread.

    Called by startup + scheduler periodically. Safe to call repeatedly;
    the underlying fetcher has its own cache TTL gate.
    """
    import threading

    def _refresh() -> None:
        try:
            from tools.web.weather import _fetch_weather_text
            _fetch_weather_text()
        except Exception:
            pass

    t = threading.Thread(target=_refresh, daemon=True, name="weather-refresh")
    t.start()


def _try_camera_light_level() -> float | None:
    """Best-effort camera brightness — average pixel value from the
    most recent frame, if vision is available. Returns 0.0-1.0 or
    None if unavailable.
    """
    # This stays optional — most contexts won't have a recent frame
    # accessible from a top-level utility module without circular
    # imports. Future integration could wire from camera_loop.
    return None


def snapshot(*, include_weather: bool = True) -> dict[str, Any]:
    """Compute a physical-context snapshot dict.

    Cheap to call (sub-millisecond except for the optional weather
    fetch which is HTTP).
    """
    now = dt.datetime.now()
    out: dict[str, Any] = {
        "ts": now.timestamp(),
        "iso": now.isoformat(timespec="minutes"),
        "hour": now.hour,
        "minute": now.minute,
        "time_of_day": time_of_day(now),
        "day_of_week": day_of_week(now),
        "is_weekend": is_weekend(now),
        "season": season(now),
        "month": now.strftime("%B"),
        "weather": "",
    }
    if include_weather:
        out["weather"] = _try_weather()
    light = _try_camera_light_level()
    if light is not None:
        out["light_level"] = light
    return out


def hint_for_introspection(*, include_weather: bool = True) -> str:
    """Short physical-context hint for system prompts.

    Examples:
      "It's evening on a Tuesday in spring. Beaufort, SC: 78°F."
      "Late-night Saturday. Winter. (Weather unavailable.)"
    """
    s = snapshot(include_weather=include_weather)
    parts = []
    parts.append(
        f"It's {s['time_of_day']} on a {s['day_of_week']} in {s['season']}."
    )
    if s.get("weather"):
        parts.append(s["weather"])
    if s.get("light_level") is not None:
        ll = float(s["light_level"])
        if ll < 0.2:
            parts.append("The room is dim.")
        elif ll > 0.7:
            parts.append("The room is bright.")
    return " ".join(parts)
