"""weather — current conditions + short forecast for ANY place (Zeke 2026-10-07: "the ability to know
what the weather is in a given place").

Primary source: Open-Meteo (free, no key): its geocoder turns a place name into coordinates, its
forecast API gives current conditions + up to 7 days. Fallback: wttr.in. No place given => wttr.in's
IP-based guess of where the tower is (the original behaviour).

params: place (str, optional) · days (int 1-7, default 3) · units ("f"|"c", default "f")
"""
from __future__ import annotations

import time
from typing import Any

import requests

from tools.tool_registry import register_tool

UA = {"User-Agent": "IrisCompanion/1.0 (personal assistant; light use)"}
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 600.0  # 10 min — both services ask for polite use

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
       81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}


def geocode(place: str) -> dict | None:
    """Place name -> {name, lat, lon, country, admin1, timezone}. Open-Meteo first, Nominatim fallback."""
    try:
        r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": place, "count": 1, "language": "en", "format": "json"},
                         headers=UA, timeout=8)
        res = (r.json() or {}).get("results") or []
        if res:
            x = res[0]
            return {"name": x.get("name"), "lat": x["latitude"], "lon": x["longitude"],
                    "country": x.get("country"), "admin1": x.get("admin1"), "timezone": x.get("timezone")}
    except Exception:
        pass
    try:  # landmarks / full addresses: Open-Meteo only knows place names
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": place, "format": "json", "limit": 1}, headers=UA, timeout=10)
        res = r.json() or []
        if res:
            x = res[0]
            return {"name": x.get("display_name", place).split(",")[0], "lat": float(x["lat"]),
                    "lon": float(x["lon"]), "country": x.get("display_name", "").split(",")[-1].strip(),
                    "admin1": None, "timezone": "auto", "display": x.get("display_name")}
    except Exception:
        pass
    return None


def _open_meteo(loc: dict, days: int, units: str) -> dict:
    f = units == "f"
    r = requests.get("https://api.open-meteo.com/v1/forecast", headers=UA, timeout=10, params={
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": days,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,"
                   "wind_speed_10m,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "temperature_unit": "fahrenheit" if f else "celsius", "wind_speed_unit": "mph" if f else "kmh"})
    d = r.json()
    if "current" not in d:
        raise RuntimeError(str(d)[:200])
    u, w = ("°F", "mph") if f else ("°C", "km/h")
    c = d["current"]
    now = (f"{round(c['temperature_2m'])}{u} (feels {round(c['apparent_temperature'])}{u}), "
           f"{WMO.get(c.get('weather_code'), 'code ' + str(c.get('weather_code')))}, "
           f"humidity {c.get('relative_humidity_2m')}%, wind {round(c.get('wind_speed_10m') or 0)} {w}")
    daily = []
    dd = d.get("daily") or {}
    for i, day in enumerate(dd.get("time") or []):
        daily.append({"date": day, "summary": WMO.get(dd["weather_code"][i], "?"),
                      "high": round(dd["temperature_2m_max"][i]), "low": round(dd["temperature_2m_min"][i]),
                      "rain_chance_pct": (dd.get("precipitation_probability_max") or [None] * 99)[i]})
    return {"now": now, "daily": daily, "local_time": c.get("time"), "timezone": d.get("timezone"),
            "units": u, "source": "open-meteo"}


def _tool_weather(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    place = str(params.get("place") or "").strip()
    days = max(1, min(7, int(params.get("days") or 3)))
    units = "c" if str(params.get("units") or "f").lower().startswith("c") else "f"
    key = f"{place.lower()}|{days}|{units}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    if place:
        loc = geocode(place)
        if not loc:
            return {"ok": False, "spoken": f"I couldn't find a place called {place}."}
        try:
            w = _open_meteo(loc, days, units)
        except Exception as e:  # noqa: BLE001
            try:
                txt = requests.get(f"https://wttr.in/{loc['lat']},{loc['lon']}?format=3", headers=UA, timeout=8).text.strip()
                w = {"now": txt, "daily": [], "source": "wttr.in", "error_primary": repr(e)[:120]}
            except Exception:
                return {"ok": False, "spoken": "The weather services aren't answering right now."}
        where = ", ".join(x for x in (loc.get("name"), loc.get("admin1"), loc.get("country")) if x)
        spoken = f"{where}: {w['now']}."
        if w.get("daily"):
            t = w["daily"][0]
            spoken += f" Today {t['summary']}, high {t['high']}, low {t['low']}"
            spoken += (f", {t['rain_chance_pct']}% chance of rain." if t.get("rain_chance_pct") is not None else ".")
        out = {"ok": True, "place": where, "lat": loc["lat"], "lon": loc["lon"], "spoken": spoken, **w}
    else:
        try:
            r = requests.get("https://wttr.in/?format=3", headers=UA, timeout=8)
            text = (r.text or "").strip()
            if r.status_code != 200 or not text:
                return {"ok": False, "spoken": "The weather service is having trouble right now."}
        except Exception:
            return {"ok": False, "spoken": "I can't reach the weather service right now."}
        out = {"ok": True, "spoken": text, "raw": text, "source": "wttr.in (IP location)"}
    _CACHE[key] = (time.time(), out)
    return out


register_tool(
    "weather",
    "Current weather + a short forecast for ANY place: params place='Tokyo' (city, town or landmark), "
    "days=1-7 (default 3), units='f'|'c'. Open-Meteo (free, no key) with wttr.in fallback. No place = "
    "the tower's own location guessed from its IP.",
    1,
    _tool_weather,
)
