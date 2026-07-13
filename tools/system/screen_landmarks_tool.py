# SELF_ASSESSMENT: I am Iris's spatial memory for pointing — a persistent store of
# named screen targets (state/screen_landmarks.json) so known things get pointed at
# INSTANTLY instead of re-found by screenshot every time. Zeke work order 2026-07-13:
# "things on your app you should already know where they are."
"""
screen_landmarks — remember where things are; point without looking.

Two scopes:
  - "window": stored RELATIVE to a window (fractions of its client rect), resolved
    against the live rect at point-time — survives moves/resizes. `window` holds a
    title substring (e.g. "Iris" for the main app).
  - "desktop": absolute virtual-desktop pixels (icons, taskbar, second monitor).
    Can go stale if Zeke rearranges; verified_ts records the last time I confirmed
    it by eye. landmark_point returns age so I can decide when to re-verify.

Store shape (state/screen_landmarks.json):
  {"<name>": {"scope": "window"|"desktop", "window": "Iris"?, "fx": 0.5, "fy": 0.886,
              "x": 795, "y": 195, "monitor": 1, "description": "...",
              "angle_deg": null|float, "verified_ts": 1783961000.0}}

Tools:
  landmark_set    {name, x?, y?, window?, description?, angle_deg?} — window given →
                  converts (x,y) to fractions of that window's client rect
  landmark_point  {name, duration_s?, description?} — resolve + widget_point, no screenshot
  landmark_list   {} — everything known, with staleness ages
  landmark_delete {name}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool
from tools.system.widget_spatial_tool import point_widget_at

LANDMARK_FILE = Path(r"D:\Wren-Companion\state\screen_landmarks.json")


def _load() -> dict[str, Any]:
    try:
        if LANDMARK_FILE.exists():
            return json.loads(LANDMARK_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save(d: dict[str, Any]) -> None:
    LANDMARK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LANDMARK_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    json.loads(LANDMARK_FILE.read_text(encoding="utf-8"))   # real-loader verify


def _find_window_client(title_substr: str) -> "dict | None":
    """Client rect (screen coords) of the first visible window whose title
    contains title_substr (case-insensitive). Excludes the widget window."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: list = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        t = buf.value
        if title_substr.lower() in t.lower() and "widget" not in t.lower():
            found.append((hwnd, t))
        return True

    user32.EnumWindows(_enum, 0)
    if not found:
        return None
    hwnd, title = found[0]
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    return {"hwnd": int(hwnd), "title": title,
            "left": int(pt.x), "top": int(pt.y), "width": int(w), "height": int(h)}


def _resolve(name: str, lm: dict[str, Any]) -> dict[str, Any]:
    """Resolve a landmark to absolute (x, y). Raises ValueError with a reason."""
    if lm.get("scope") == "window":
        win = _find_window_client(str(lm.get("window") or "Iris"))
        if win is None:
            raise ValueError(f"window '{lm.get('window')}' not found/visible for landmark '{name}'")
        x = win["left"] + int(round(float(lm["fx"]) * win["width"]))
        y = win["top"] + int(round(float(lm["fy"]) * win["height"]))
        return {"x": x, "y": y, "window": win}
    return {"x": int(lm["x"]), "y": int(lm["y"]), "window": None}


def _landmark_set(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "").strip().lower().replace(" ", "_")
    if not name:
        return {"ok": False, "error": "name required"}
    x, y = params.get("x"), params.get("y")
    if x is None or y is None:
        return {"ok": False, "error": "x and y (virtual-desktop pixels) required"}
    x, y = int(x), int(y)
    d = _load()
    lm: dict[str, Any] = {
        "description": str(params.get("description") or ""),
        "verified_ts": time.time(),
    }
    if params.get("angle_deg") is not None:
        lm["angle_deg"] = float(params["angle_deg"])
    window = params.get("window")
    if window:
        win = _find_window_client(str(window))
        if win is None:
            return {"ok": False, "error": f"window '{window}' not found — is it open and visible?"}
        lm.update({"scope": "window", "window": str(window),
                   "fx": round((x - win["left"]) / win["width"], 4),
                   "fy": round((y - win["top"]) / win["height"], 4)})
    else:
        lm.update({"scope": "desktop", "x": x, "y": y})
    d[name] = lm
    try:
        _save(d)
    except Exception as e:
        return {"ok": False, "error": f"save failed: {e!r}"}
    return {"ok": True, "name": name, "landmark": lm}


def _landmark_point(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "").strip().lower().replace(" ", "_")
    d = _load()
    lm = d.get(name)
    if lm is None:
        close = [k for k in d if name in k or k in name]
        return {"ok": False, "error": f"unknown landmark '{name}'",
                "similar": close, "known": sorted(d.keys())}
    try:
        pos = _resolve(name, lm)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    res = point_widget_at(
        g, pos["x"], pos["y"],
        duration_s=float(params.get("duration_s") or 8.0),
        description=str(params.get("description") or lm.get("description") or name),
        prefer_angle=lm.get("angle_deg"),
    )
    res["landmark"] = name
    res["verified_age_s"] = round(time.time() - float(lm.get("verified_ts") or 0), 1)
    if lm.get("scope") == "desktop" and res["verified_age_s"] > 7 * 24 * 3600:
        res["note"] = "desktop landmark >1wk unverified — consider a visual re-check"
    return res


def _landmark_list(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    d = _load()
    now = time.time()
    rows = []
    for k, lm in sorted(d.items()):
        rows.append({"name": k, "scope": lm.get("scope"),
                     "window": lm.get("window"),
                     "at": ({"fx": lm.get("fx"), "fy": lm.get("fy")}
                            if lm.get("scope") == "window" else
                            {"x": lm.get("x"), "y": lm.get("y")}),
                     "description": lm.get("description") or "",
                     "verified_age_h": round((now - float(lm.get("verified_ts") or 0)) / 3600, 1)})
    return {"ok": True, "count": len(rows), "landmarks": rows}


def _landmark_delete(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "").strip().lower().replace(" ", "_")
    d = _load()
    if name not in d:
        return {"ok": False, "error": f"unknown landmark '{name}'"}
    d.pop(name)
    try:
        _save(d)
    except Exception as e:
        return {"ok": False, "error": f"save failed: {e!r}"}
    return {"ok": True, "deleted": name}


register_tool(
    "landmark_set",
    "Remember a named screen target: params {name, x, y, window? (title substring — stores window-RELATIVE so it survives moves), description?, angle_deg?}. Landmarks make pointing instant (no screenshot).",
    2,
    _landmark_set,
)
register_tool(
    "landmark_point",
    "Point the widget at a remembered landmark by name — instant, no screenshot. Params {name, duration_s?}. Returns verified_age so stale desktop landmarks can be re-checked.",
    1,
    _landmark_point,
)
register_tool(
    "landmark_list",
    "List all remembered screen landmarks with staleness ages.",
    1,
    _landmark_list,
)
register_tool(
    "landmark_delete",
    "Forget a screen landmark by name.",
    2,
    _landmark_delete,
)


# ── Window minimize/restore by TITLE (2026-07-13) ────────────────────────────
# Scar from the finger demo: Get-Process MainWindowHandle returned the WIDGET
# window and I minimized my own finger mid-demo. These resolve by title via the
# same EnumWindows finder the landmarks use (widget excluded), so the right
# window gets hit every time.

def _find_hwnd_by_title(title_substr: str) -> "tuple[int, str] | None":
    """hwnd+title by title substring, widget excluded. Unlike _find_window_client
    this has NO client-rect requirement — a MINIMIZED window has a 0x0 client
    rect and must still be findable for restore."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: list = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        t = buf.value
        if title_substr.lower() in t.lower() and "widget" not in t.lower():
            found.append((int(hwnd), t))
        return True

    user32.EnumWindows(_enum, 0)
    return found[0] if found else None


def _window_show_cmd(params: dict[str, Any], g: dict[str, Any], cmd: int, verb: str) -> dict[str, Any]:
    title = str(params.get("window") or "").strip()
    if not title:
        return {"ok": False, "error": "window (title substring) required"}
    hit = _find_hwnd_by_title(title)
    if hit is None:
        return {"ok": False, "error": f"no visible window matching '{title}' (widget excluded)"}
    hwnd, full_title = hit
    import ctypes
    ok = bool(ctypes.windll.user32.ShowWindow(hwnd, cmd))
    return {"ok": ok, "action": verb, "hwnd": hwnd, "title": full_title}


def _window_minimize(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _window_show_cmd(params, g, 6, "minimized")     # SW_MINIMIZE


def _window_restore(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _window_show_cmd(params, g, 9, "restored")      # SW_RESTORE


register_tool(
    "window_minimize",
    "Minimize a window by title substring (resolved via EnumWindows, widget excluded — never hits the wrong window). Params {window}.",
    2,
    _window_minimize,
)
register_tool(
    "window_restore",
    "Restore (un-minimize) a window by title substring. Params {window}.",
    2,
    _window_restore,
)
