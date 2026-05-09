from __future__ import annotations

import time
from typing import Any

import requests

from tools.tool_registry import register_tool


_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}
_CACHE_TTL_SEC = 600.0  # 10 min — wttr.in asks for polite usage


def _fetch_weather_text() -> tuple[bool, str]:
    """Returns (ok, message). On failure, message is a user-facing line.

    Uses wttr.in's `?format=3` short response which auto-detects location
    from the request IP — no API key, no separate geolocation call. Output
    looks like: "Lebanon, NH: ☀ +72°F".
    """
    now = time.time()
    cached = _CACHE.get("value")
    if cached and (now - float(_CACHE.get("ts") or 0.0)) < _CACHE_TTL_SEC:
        return True, str(cached)
    try:
        r = requests.get("https://wttr.in/?format=3", timeout=8)
    except requests.exceptions.ConnectionError:
        return False, "I can't reach the internet right now to check the weather."
    except requests.exceptions.Timeout:
        return False, "The weather service didn't respond in time."
    except Exception:
        return False, "I couldn't reach the weather service."
    if r.status_code != 200:
        return False, "The weather service is having trouble right now."
    text = (r.text or "").strip()
    if not text:
        return False, "I got an empty response from the weather service."
    _CACHE["ts"] = now
    _CACHE["value"] = text
    return True, text


def _tool_weather(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    ok, message = _fetch_weather_text()
    return {"ok": ok, "spoken": message, "raw": message}


register_tool(
    "weather",
    "Fetch current weather for the user's location via wttr.in. No API key. "
    "Auto-detects city from request IP.",
    1,
    _tool_weather,
)
