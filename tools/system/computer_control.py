# SELF_ASSESSMENT: I control the keyboard and mouse. I start cautious and become more fluid as I succeed.
"""
Phase 53 — PyAutoGUI computer control. Tier 2 (verbal check-in before executing).

Requires: py -3.11 -m pip install pyautogui

Bootstrap: Ava tracks success/failure per action type. She decides when she's
confident enough to chain actions without pausing.
"""
from __future__ import annotations

import time
from typing import Any
from tools.tool_registry import register_tool

_SUCCESS_COUNTS: dict[str, int] = {}
_FAILURE_COUNTS: dict[str, int] = {}
_ACTION_DELAY = 0.1


def _get_pyautogui():
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = _ACTION_DELAY
        return pyautogui
    except ImportError:
        return None


def _track(action: str, ok: bool) -> None:
    if ok:
        _SUCCESS_COUNTS[action] = _SUCCESS_COUNTS.get(action, 0) + 1
    else:
        _FAILURE_COUNTS[action] = _FAILURE_COUNTS.get(action, 0) + 1


def _screen_bounds():
    try:
        import ctypes
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)
        return w, h
    except Exception:
        return 3840, 2160  # safe large default


def _bounds_check(x: int, y: int) -> bool:
    sw, sh = _screen_bounds()
    return 0 <= x < sw and 0 <= y < sh


def _type_text(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    pg = _get_pyautogui()
    if pg is None:
        return {"ok": False, "error": "pyautogui not installed"}
    text = str(params.get("text") or "")
    try:
        pg.typewrite(text, interval=0.05)
        _track("type_text", True)
        return {"ok": True, "typed": len(text)}
    except Exception as e:
        _track("type_text", False)
        return {"ok": False, "error": str(e)[:200]}


def _press_key(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    pg = _get_pyautogui()
    if pg is None:
        return {"ok": False, "error": "pyautogui not installed"}
    key = str(params.get("key") or "")
    try:
        if "+" in key:
            parts = [p.strip() for p in key.split("+")]
            pg.hotkey(*parts)
        else:
            pg.press(key)
        _track("press_key", True)
        return {"ok": True, "key": key}
    except Exception as e:
        _track("press_key", False)
        return {"ok": False, "error": str(e)[:200]}


def _click(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    pg = _get_pyautogui()
    if pg is None:
        return {"ok": False, "error": "pyautogui not installed"}
    x, y = int(params.get("x") or 0), int(params.get("y") or 0)
    if not _bounds_check(x, y):
        return {"ok": False, "error": f"coordinates ({x},{y}) outside screen bounds"}
    try:
        pg.click(x, y)
        _track("click", True)
        return {"ok": True, "x": x, "y": y}
    except Exception as e:
        _track("click", False)
        return {"ok": False, "error": str(e)[:200]}


def _right_click(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    pg = _get_pyautogui()
    if pg is None:
        return {"ok": False, "error": "pyautogui not installed"}
    x, y = int(params.get("x") or 0), int(params.get("y") or 0)
    if not _bounds_check(x, y):
        return {"ok": False, "error": "coordinates outside screen bounds"}
    try:
        pg.rightClick(x, y)
        _track("right_click", True)
        return {"ok": True, "x": x, "y": y}
    except Exception as e:
        _track("right_click", False)
        return {"ok": False, "error": str(e)[:200]}


def _double_click(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    pg = _get_pyautogui()
    if pg is None:
        return {"ok": False, "error": "pyautogui not installed"}
    x, y = int(params.get("x") or 0), int(params.get("y") or 0)
    if not _bounds_check(x, y):
        return {"ok": False, "error": "coordinates outside screen bounds"}
    try:
        pg.doubleClick(x, y)
        _track("double_click", True)
        return {"ok": True, "x": x, "y": y}
    except Exception as e:
        _track("double_click", False)
        return {"ok": False, "error": str(e)[:200]}


def _scroll(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    pg = _get_pyautogui()
    if pg is None:
        return {"ok": False, "error": "pyautogui not installed"}
    x, y = int(params.get("x") or 0), int(params.get("y") or 0)
    amount = int(params.get("amount") or 3)
    if not _bounds_check(x, y):
        return {"ok": False, "error": "coordinates outside screen bounds"}
    try:
        pg.scroll(amount, x=x, y=y)
        _track("scroll", True)
        return {"ok": True, "x": x, "y": y, "amount": amount}
    except Exception as e:
        _track("scroll", False)
        return {"ok": False, "error": str(e)[:200]}


def _drag(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    pg = _get_pyautogui()
    if pg is None:
        return {"ok": False, "error": "pyautogui not installed"}
    x1, y1 = int(params.get("x1") or 0), int(params.get("y1") or 0)
    x2, y2 = int(params.get("x2") or 0), int(params.get("y2") or 0)
    if not _bounds_check(x1, y1) or not _bounds_check(x2, y2):
        return {"ok": False, "error": "coordinates outside screen bounds"}
    try:
        pg.moveTo(x1, y1)
        time.sleep(0.1)
        pg.dragTo(x2, y2, duration=0.5)
        _track("drag", True)
        return {"ok": True, "from": [x1, y1], "to": [x2, y2]}
    except Exception as e:
        _track("drag", False)
        return {"ok": False, "error": str(e)[:200]}


def _stats(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "success_counts": dict(_SUCCESS_COUNTS),
        "failure_counts": dict(_FAILURE_COUNTS),
        "note": "Ava tracks this to calibrate her own confidence in chaining actions.",
    }


register_tool("type_text", "Type text at current cursor position. Tier 2 — verbal check-in.", 2, _type_text)
register_tool("press_key", "Press a key or key combo (e.g. ctrl+c, alt+tab). Tier 2.", 2, _press_key)
register_tool("click", "Click at screen coordinates (x, y). Tier 2.", 2, _click)
register_tool("right_click", "Right-click at screen coordinates. Tier 2.", 2, _right_click)
register_tool("double_click", "Double-click at screen coordinates. Tier 2.", 2, _double_click)
register_tool("scroll", "Scroll at position (x, y) by amount. Tier 2.", 2, _scroll)
register_tool("drag", "Click-drag from (x1,y1) to (x2,y2). Tier 2.", 2, _drag)
register_tool("computer_control_stats", "View Ava's action success/failure history. Tier 1.", 1, _stats)
