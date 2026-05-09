# SELF_ASSESSMENT: I move the floating Ava widget orb to positions on screen.
"""
Widget orb positional control — tier 1 tool.

Moves the floating widget window to named screen positions.
Works by: (1) saving position to state/widget_position.json and
(2) moving the live window via Windows user32 SetWindowPos.
"""
from __future__ import annotations

import json
import ctypes
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_WIDGET_TITLE = "Ava Widget"
_WIDGET_W = 150
_WIDGET_H = 150
_MARGIN = 10


def _screen_size() -> tuple[int, int]:
    try:
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def _resolve_position(label: str) -> tuple[int | None, int | None]:
    sw, sh = _screen_size()
    m = _MARGIN
    cx = sw // 2 - _WIDGET_W // 2
    cy = sh // 2 - _WIDGET_H // 2

    positions: dict[str, tuple[int, int]] = {
        "top_left":      (m, m),
        "top_right":     (sw - _WIDGET_W - m, m),
        "bottom_left":   (m, sh - _WIDGET_H - m),
        "bottom_right":  (sw - _WIDGET_W - m, sh - _WIDGET_H - m),
        "center":        (cx, cy),
        "left":          (m, cy),
        "right":         (sw - _WIDGET_W - m, cy),
        "top":           (cx, m),
        "bottom":        (cx, sh - _WIDGET_H - m),
    }

    # Normalise free-form label → canonical key
    label = label.lower().strip().replace(" ", "_").replace("-", "_")
    aliases: dict[str, str] = {
        "top_left": "top_left",
        "upper_left": "top_left",
        "upper_right": "top_right",
        "top_right": "top_right",
        "bottom_left": "bottom_left",
        "lower_left": "bottom_left",
        "lower_right": "bottom_right",
        "bottom_right": "bottom_right",
        "centre": "center",
        "middle": "center",
        "aside": "right",
        "side": "right",
        "out_of_the_way": "bottom_right",
        "out_of_way": "bottom_right",
        "get_out": "bottom_right",
        "come_here": "center",
        "here": "center",
        "come_back": "center",
    }
    key = aliases.get(label, label)
    if key in positions:
        return positions[key]
    # fuzzy: partial match
    for k, v in positions.items():
        if k in label or label in k:
            return v
    return None, None


def _save_position(g: dict[str, Any], x: int, y: int) -> None:
    try:
        base = Path(g.get("BASE_DIR") or ".")
        p = base / "state" / "widget_position.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"x": x, "y": y}, indent=2), encoding="utf-8")
    except Exception:
        pass


def _move_window_win32(title: str, x: int, y: int) -> bool:
    """Move a live window to (x, y) using Windows user32. No-op on non-Windows."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        # SWP_NOSIZE=0x0001 | SWP_NOZORDER=0x0004 — move only, preserve size and z-order
        user32.SetWindowPos(hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004)
        return True
    except Exception:
        return False


def _tool_move_widget(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    position = str(params.get("position") or "").strip()
    if not position:
        return {"ok": False, "error": "position parameter required. Valid: top_left, top_right, bottom_left, bottom_right, center, left, right, top, bottom"}

    x, y = _resolve_position(position)
    if x is None:
        return {
            "ok": False,
            "error": f"Unknown position: {position!r}",
            "valid_positions": ["top_left", "top_right", "bottom_left", "bottom_right", "center", "left", "right", "top", "bottom"],
        }

    _save_position(g, x, y)
    moved_live = _move_window_win32(_WIDGET_TITLE, x, y)
    sw, sh = _screen_size()
    return {
        "ok": True,
        "position": position,
        "x": x,
        "y": y,
        "screen": f"{sw}x{sh}",
        "live_window_moved": moved_live,
    }


register_tool(
    "move_widget",
    (
        "Move the floating Ava orb widget to a named screen position. "
        "Valid positions: top_left, top_right, bottom_left, bottom_right, center, left, right, top, bottom. "
        "Also understands: 'out_of_the_way' (bottom_right), 'come_here'/'come_back' (center), 'side' (right)."
    ),
    1,
    _tool_move_widget,
)
