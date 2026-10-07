"""tests/test_jarvis_tools.py — offline tests for the 2026-10-07 tools + their app routes.
No network, no scheduled tasks, no audio changes: network calls are monkeypatched.
Run: .venv\\Scripts\\python.exe -m pytest -q tests/test_jarvis_tools.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── reminders: time parsing + validation ─────────────────────────────────────
from tools.system import reminder_task_tool as rt  # noqa: E402

NOW = datetime(2026, 10, 7, 10, 0)


@pytest.mark.parametrize("s,want", [
    ("in 20m", datetime(2026, 10, 7, 10, 20)),
    ("in 2h", datetime(2026, 10, 7, 12, 0)),
    ("in 1d", datetime(2026, 10, 8, 10, 0)),
    ("in 90 seconds", datetime(2026, 10, 7, 10, 1, 30)),
    ("at 17:30", datetime(2026, 10, 7, 17, 30)),
    ("at 9:00", datetime(2026, 10, 8, 9, 0)),          # already past today -> tomorrow
    ("tomorrow 9:00", datetime(2026, 10, 8, 9, 0)),
    ("5:30pm", datetime(2026, 10, 7, 17, 30)),
    ("12:15am", datetime(2026, 10, 8, 0, 15)),
    ("2026-10-08 07:15", datetime(2026, 10, 8, 7, 15)),
])
def test_parse_when(s, want):
    assert rt.parse_when(s, NOW) == want


@pytest.mark.parametrize("bad", ["soonish", "", "in -5m", "at 25:99x"])
def test_parse_when_rejects(bad):
    with pytest.raises(ValueError):
        rt.parse_when(bad, NOW)


def test_reminder_add_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "STORE", tmp_path / "reminders.json")
    monkeypatch.setattr(rt, "_ps", lambda cmd: (_ for _ in ()).throw(AssertionError("must not register a task")))
    assert rt._tool_reminder({"action": "add", "text": "", "when": "in 1h"}, {})["ok"] is False
    assert rt._tool_reminder({"action": "add", "text": "x", "when": "in 5s"}, {})["ok"] is False
    assert rt._tool_reminder({"action": "add", "text": "x", "when": "nonsense"}, {})["ok"] is False
    assert rt._tool_reminder({"action": "cancel", "id": "nope"}, {})["ok"] is False
    assert rt._tool_reminder({"action": "list"}, {}) == {"ok": True, "reminders": []}


def test_reminder_add_registers_and_lists(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "STORE", tmp_path / "reminders.json")
    seen = {}
    monkeypatch.setattr(rt, "_ps", lambda cmd: (seen.setdefault("cmd", cmd), (0, "Ready"))[1])
    r = rt._tool_reminder({"action": "add", "text": "stretch", "when": "in 3h"}, {})
    assert r["ok"] and r["task"].startswith("Iris-Reminder-")
    assert "StartWhenAvailable" in seen["cmd"] and "pythonw.exe" in seen["cmd"]
    lst = rt._tool_reminder({"action": "list"}, {})["reminders"]
    assert lst and lst[0]["text"] == "stretch" and lst[0]["status"] == "pending"


# ── app routes: security helpers ─────────────────────────────────────────────
from brain import app_jarvis_routes as jr  # noqa: E402


@pytest.mark.parametrize("origin,ok", [
    (None, True), ("", True), ("http://tauri.localhost", True), ("https://tauri.localhost/", True),
    ("http://localhost:5173", True), ("https://evil.example", False), ("http://localhost:8080", False),
    ("null", False),
])
def test_origin_ok(origin, ok):
    assert jr.origin_ok(origin) is ok


def test_rate_limit():
    jr._RATE.clear()
    assert all(jr.rate_ok("t", 3, 60) for _ in range(3))
    assert jr.rate_ok("t", 3, 60) is False


@pytest.mark.parametrize("name,ok", [
    ("eiffel_tower_2d_z16.jpg", True), ("../config/private.local.json", False), ("a.png", False),
    ("..%2Fx.jpg", False), ("UPPER.jpg", False), ("x" * 90 + ".jpg", False),
])
def test_map_name_guard(name, ok):
    assert bool(jr.MAP_NAME.match(name)) is ok


# ── weather: offline with a fake network ─────────────────────────────────────
from tools.web import weather as wx  # noqa: E402


class _Resp:
    def __init__(self, data, status=200, text=""):
        self._d, self.status_code, self.text = data, status, text

    def json(self):
        return self._d


def test_weather_any_place(monkeypatch, tmp_path):
    monkeypatch.setattr(wx, "_GEO_FILE", tmp_path / "geo.json")
    wx._CACHE.clear()
    wx._GEO_CACHE.clear()

    def fake_get(url, params=None, headers=None, timeout=None):
        if "nominatim" in url:
            return _Resp([{"name": "Tokyo", "lat": "35.68", "lon": "139.69",
                           "address": {"country": "Japan", "state": "Tokyo"}, "display_name": "Tokyo, Japan"}])
        if "api.open-meteo.com" in url:
            return _Resp({"timezone": "Asia/Tokyo",
                          "current": {"time": "2026-10-07T23:00", "temperature_2m": 61.4, "apparent_temperature": 60.2,
                                      "relative_humidity_2m": 70, "weather_code": 3, "wind_speed_10m": 4.4},
                          "daily": {"time": ["2026-10-07"], "weather_code": [61], "temperature_2m_max": [70.2],
                                    "temperature_2m_min": [58.9], "precipitation_probability_max": [40]}})
        raise AssertionError(url)
    monkeypatch.setattr(wx.requests, "get", fake_get)
    monkeypatch.setattr(wx.time, "sleep", lambda s: None)
    r = wx._tool_weather({"place": "Tokyo", "days": 1}, {})
    assert r["ok"] and r["place"] == "Tokyo, Tokyo, Japan"
    assert "61°F" in r["now"] and "overcast" in r["now"]
    assert r["daily"][0] == {"date": "2026-10-07", "summary": "light rain", "high": 70, "low": 59, "rain_chance_pct": 40}


def test_weather_unknown_place(monkeypatch, tmp_path):
    monkeypatch.setattr(wx, "_GEO_FILE", tmp_path / "geo.json")
    wx._CACHE.clear()
    wx._GEO_CACHE.clear()
    monkeypatch.setattr(wx.requests, "get", lambda *a, **k: _Resp([] if "nominatim" in a[0] else {"results": []}))
    monkeypatch.setattr(wx.time, "sleep", lambda s: None)
    r = wx._tool_weather({"place": "zzqqxx"}, {})
    assert r["ok"] is False and "couldn't find" in r["spoken"]


# ── maps: tile math + render offline ─────────────────────────────────────────
from tools.web import place_map as pm  # noqa: E402


def test_place_map_render_offline(monkeypatch, tmp_path):
    from PIL import Image
    monkeypatch.setattr(pm, "OUT", tmp_path)
    monkeypatch.setattr(pm, "TILES", tmp_path / "tiles")
    monkeypatch.setattr(pm, "_tile", lambda kind, z, x, y: Image.new("RGB", (256, 256), (x % 255, y % 255, 9)))
    r = pm._tool_place_map({"lat": 48.8583, "lon": 2.2945, "zoom": 16, "kind": "both"}, {})
    assert r["ok"] and set(r["files"]) == {"2d", "satellite"}
    im = Image.open(r["files"]["2d"])
    assert im.size == (pm.W, pm.H)
    assert im.getpixel((pm.W // 2, pm.H // 2))[0] > 200          # the red centre marker
    assert r["earth_3d_url"].startswith("https://earth.google.com/web/@48.858300,2.294500")


def test_place_map_needs_input():
    assert pm._tool_place_map({}, {})["ok"] is False


# ── pc tool: argument handling (no audio changes) ────────────────────────────
from tools.system import pc_control_tool as pc  # noqa: E402


def test_pc_rejects_unknown():
    r = pc._tool_pc({"action": "format_c"}, {})
    assert r["ok"] is False and "actions" in r


def test_pc_media_rejects_bad_key():
    assert pc._tool_pc({"action": "media", "key": "explode"}, {})["ok"] is False


def test_pc_window_missing():
    assert pc._tool_pc({"action": "window", "title": "no-such-window-zz9", "op": "focus"}, {})["ok"] is False


def test_reminder_lock_and_update_row(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "STORE", tmp_path / "reminders.json")
    rt._save({"abc12345": {"id": "abc12345", "text": "t", "status": "pending", "due_ts": 0}})
    assert rt.update_row("abc12345", status="fired")["status"] == "fired"
    assert rt.update_row("nope") is None
    # a held lock times out instead of corrupting the store; a stale one (>30 s) is broken
    lock = rt.STORE.with_suffix(".lock")
    lock.write_text("x")
    import os, time as _t
    old = _t.time() - 60
    os.utime(lock, (old, old))
    assert rt.update_row("abc12345", status="cancelled")["status"] == "cancelled"
    assert not lock.exists()
