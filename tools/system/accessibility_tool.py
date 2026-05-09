# SELF_ASSESSMENT: I read the desktop UI structure — open windows, their elements, browser URLs — without screenshots.
"""
Phase 51 — UI accessibility tree tool.

All tools are Tier 1: Ava can use freely without approval.
pywinauto must be installed: py -3.11 -m pip install pywinauto

Bootstrap: Ava builds a mental model of your typical desktop layout.
She notes which apps you use at what times, which windows are usually open together.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_LAYOUT_FILE = Path("state/desktop_layout.json")


def _save_layout(windows: list[dict]) -> None:
    try:
        _LAYOUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        hour = time.strftime("%H")
        data: dict = {}
        if _LAYOUT_FILE.is_file():
            try:
                data = json.loads(_LAYOUT_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        slot = data.setdefault("hourly", {}).setdefault(hour, [])
        titles = [w.get("title", "") for w in windows[:20]]
        if titles not in slot[-5:]:
            slot.append(titles)
            if len(slot) > 20:
                slot[:] = slot[-20:]
        data["last_seen"] = time.time()
        _LAYOUT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _list_windows(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from pywinauto import Desktop
        desk = Desktop(backend="uia")
        wins = []
        for w in desk.windows():
            try:
                wins.append({
                    "title": str(w.window_text() or "")[:120],
                    "process": str(w.process_id()),
                    "visible": bool(w.is_visible()),
                    "rect": {
                        "left": w.rectangle().left,
                        "top": w.rectangle().top,
                        "right": w.rectangle().right,
                        "bottom": w.rectangle().bottom,
                    },
                })
            except Exception:
                continue
        _save_layout(wins)
        return {"ok": True, "windows": wins[:50], "count": len(wins)}
    except ImportError:
        return {"ok": False, "error": "pywinauto not installed. Run: py -3.11 -m pip install pywinauto"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _get_window_elements(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    title = str(params.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title required"}
    try:
        from pywinauto import Desktop
        desk = Desktop(backend="uia")
        win = desk.window(title_re=f".*{title}.*")
        elements = []
        try:
            for child in win.descendants()[:80]:
                try:
                    rect = child.rectangle()
                    elements.append({
                        "name": str(child.window_text() or "")[:80],
                        "type": str(child.element_info.control_type or "")[:40],
                        "x": (rect.left + rect.right) // 2,
                        "y": (rect.top + rect.bottom) // 2,
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return {"ok": True, "title": title, "elements": elements, "count": len(elements)}
    except ImportError:
        return {"ok": False, "error": "pywinauto not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _find_element(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    description = str(params.get("description") or "").strip().lower()
    if not description:
        return {"ok": False, "error": "description required"}
    try:
        from pywinauto import Desktop
        desk = Desktop(backend="uia")
        keywords = description.split()[:4]
        for win in desk.windows():
            try:
                if not win.is_visible():
                    continue
                for child in win.descendants()[:100]:
                    try:
                        text = str(child.window_text() or "").lower()
                        if any(kw in text for kw in keywords):
                            rect = child.rectangle()
                            return {
                                "ok": True,
                                "found": str(child.window_text() or "")[:80],
                                "x": (rect.left + rect.right) // 2,
                                "y": (rect.top + rect.bottom) // 2,
                                "window": str(win.window_text() or "")[:80],
                            }
                    except Exception:
                        continue
            except Exception:
                continue
        return {"ok": False, "error": f"no element matching '{description}' found"}
    except ImportError:
        return {"ok": False, "error": "pywinauto not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _get_browser_url(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from pywinauto import Desktop
        desk = Desktop(backend="uia")
        browser_titles = ["chrome", "firefox", "edge", "brave", "opera"]
        for win in desk.windows():
            try:
                title = str(win.window_text() or "").lower()
                if not any(b in title for b in browser_titles):
                    continue
                for child in win.descendants()[:50]:
                    try:
                        ct = str(child.element_info.control_type or "").lower()
                        if "edit" in ct or "text" in ct:
                            text = str(child.window_text() or "")
                            if text.startswith(("http://", "https://", "www.")):
                                return {"ok": True, "url": text[:500], "window": str(win.window_text() or "")[:80]}
                    except Exception:
                        continue
            except Exception:
                continue
        return {"ok": False, "error": "no browser URL bar found"}
    except ImportError:
        return {"ok": False, "error": "pywinauto not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _get_active_window(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return {"ok": True, "title": buf.value[:200], "hwnd": hwnd}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


register_tool("list_windows", "List all open windows with titles and process info.", 1, _list_windows)
register_tool("get_window_elements", "Get all UI elements in a window by title.", 1, _get_window_elements)
register_tool("find_element", "Search all windows for a UI element matching a description, return coordinates.", 1, _find_element)
register_tool("get_browser_url", "Read the URL from any open browser window.", 1, _get_browser_url)
register_tool("get_active_window", "Return the currently focused window title.", 1, _get_active_window)
