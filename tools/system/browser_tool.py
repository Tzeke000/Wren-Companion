# SELF_ASSESSMENT: I open URLs and navigate the browser for the user.
"""
Browser navigation tools.

Tier 1: open_url, navigate_to, open_dino_game  — execute immediately
Tier 2: click_browser_element, type_in_browser — require verbal check-in
"""
from __future__ import annotations

import subprocess
import time
import webbrowser
from typing import Any

from tools.tool_registry import register_tool


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_pyautogui():
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        return pyautogui
    except ImportError:
        return None


def _focus_browser_window() -> bool:
    """Try to bring the browser window to focus. Returns True if found."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        browser_titles = ["Chrome", "Firefox", "Edge", "Chromium", "Opera"]
        titles: list[tuple[int, str]] = []

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                l = user32.GetWindowTextLengthW(hwnd)
                if l > 0:
                    buf = ctypes.create_unicode_buffer(l + 1)
                    user32.GetWindowTextW(hwnd, buf, l + 1)
                    t = buf.value.strip()
                    if any(b.lower() in t.lower() for b in browser_titles):
                        titles.append((hwnd, t))
            return True

        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
        user32.EnumWindows(EnumProc(cb), 0)
        if titles:
            hwnd = titles[0][0]
            user32.ShowWindow(hwnd, 9)   # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


# ── Tier 1 tools ──────────────────────────────────────────────────────────────

def _tool_open_url(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    url = str(params.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url parameter required"}
    if not url.startswith(("http://", "https://", "chrome://", "about:", "file://")):
        url = "https://" + url
    try:
        webbrowser.open(url)
        return {"ok": True, "url": url, "method": "webbrowser"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _tool_navigate_to(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Navigate the currently open browser to a URL using Ctrl+L."""
    url = str(params.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url parameter required"}
    if not url.startswith(("http://", "https://", "chrome://", "about://")):
        url = "https://" + url

    pag = _get_pyautogui()
    if pag is None:
        # Fall back to webbrowser.open if pyautogui unavailable
        webbrowser.open(url)
        return {"ok": True, "url": url, "method": "webbrowser_fallback"}

    focused = _focus_browser_window()
    if not focused:
        webbrowser.open(url)
        return {"ok": True, "url": url, "method": "webbrowser_new_window"}

    time.sleep(0.3)
    pag.hotkey("ctrl", "l")
    time.sleep(0.15)
    pag.hotkey("ctrl", "a")  # select all in address bar
    pag.typewrite(url, interval=0.02)
    pag.press("enter")
    return {"ok": True, "url": url, "method": "ctrl_l_navigate", "browser_focused": True}


def _tool_open_dino_game(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Open the Chrome dinosaur game. Works even when internet is connected."""
    dino_url = "chrome://dino"
    try:
        # Try to open chrome://dino directly in Chrome
        import shutil
        chrome_path = shutil.which("chrome") or shutil.which("google-chrome")
        if not chrome_path:
            # Search common paths
            import os
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ]
            chrome_path = next((c for c in candidates if __import__("pathlib").Path(c).is_file()), None)

        if chrome_path:
            subprocess.Popen([chrome_path, dino_url])
            return {"ok": True, "url": dino_url, "message": "Dino game opened in Chrome! 🦕"}
        else:
            # Fall back to webbrowser (may not work for chrome:// scheme)
            webbrowser.open(dino_url)
            return {"ok": True, "url": dino_url, "message": "Dino game launch attempted (Chrome not found in standard paths)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ── Tier 2 tools ──────────────────────────────────────────────────────────────

def _tool_click_browser_element(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Take a screenshot and attempt to click a described browser element. Tier 2."""
    description = str(params.get("description") or "").strip()
    if not description:
        return {"ok": False, "error": "description required"}

    pag = _get_pyautogui()
    if pag is None:
        return {"ok": False, "error": "pyautogui not available — install it first"}

    try:
        screenshot = pag.screenshot()
        # Try to find element via image matching or coordinate estimation
        # This is intentionally simple — Ava can improve this over time
        focused = _focus_browser_window()
        if not focused:
            return {"ok": False, "error": "No browser window found to click in"}

        # Store screenshot path for potential analysis
        import tempfile
        import os
        path = os.path.join(tempfile.gettempdir(), "ava_browser_screenshot.png")
        screenshot.save(path)
        return {
            "ok": False,
            "error": "Visual element clicking requires LLaVA vision — not implemented yet. Try describing the page and I'll use keyboard navigation instead.",
            "screenshot_saved": path,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _tool_type_in_browser(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Type text into the currently focused browser field. Tier 2."""
    text = str(params.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text parameter required"}

    pag = _get_pyautogui()
    if pag is None:
        return {"ok": False, "error": "pyautogui not available"}

    focused = _focus_browser_window()
    if not focused:
        return {"ok": False, "error": "No browser window found"}

    time.sleep(0.2)
    pag.click()  # click to ensure focus
    time.sleep(0.1)
    pag.typewrite(text[:500], interval=0.03)
    return {"ok": True, "typed": text[:500], "chars": len(text)}


# ── Registration ──────────────────────────────────────────────────────────────

register_tool(
    "open_url",
    "Open a URL in the default browser. Accepts full URLs or bare domains (https:// added automatically).",
    1,
    _tool_open_url,
)

register_tool(
    "navigate_to",
    "Navigate an already-open browser to a URL using Ctrl+L. Falls back to opening a new tab if no browser is focused.",
    1,
    _tool_navigate_to,
)

register_tool(
    "open_dino_game",
    "Open the Chrome dinosaur game (chrome://dino). Works even when internet is connected.",
    1,
    _tool_open_dino_game,
)

register_tool(
    "click_browser_element",
    "Click a described element in the browser (e.g. 'search box', 'submit button'). Requires visual detection.",
    2,
    _tool_click_browser_element,
)

register_tool(
    "type_in_browser",
    "Type text into the currently focused browser input field.",
    2,
    _tool_type_in_browser,
)
