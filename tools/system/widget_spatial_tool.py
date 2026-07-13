# SELF_ASSESSMENT: I give the widget orb spatial self-awareness — monitors, position, and a tip-anchored pointing arrow.
"""
Widget spatial awareness + precise pointing (Zeke directive 2026-07-08).

Ground rules from Zeke, verbatim intent:
  - Same as the mouse tools know cursor coords + which screen, the widget must
    know which monitor it is on and its exact rect.
  - When the orb morphs into an arrow, the TIP position and direction must be
    KNOWN, so pointing is positional ("tip at this pixel"), not approximate —
    across both monitors.

Three layers here:
  1. Monitor enumeration (EnumDisplayMonitors) — the map of the desk.
  2. widget_status / monitor_layout — proprioception (where am I, where's the mouse).
  3. widget_point — tip-anchored placement: given a target pixel, choose a
     pointing angle whose 150x150 widget body fits on the target's monitor,
     place the window so the arrow TIP lands on the target, and publish the
     angle so the frontend rotates the particle arrow to match.

Angle convention (shared with OrbCanvas.tsx): degrees CLOCKWISE from screen-UP.
0 = tip points up, 90 = right, 180 = down, 270 = left. dir(theta) in screen
coords (y grows downward) = (sin(theta), -cos(theta)).

State written to g (read by brain/orb_http.py snapshot `widget` block):
  _widget_pointing (bool), _widget_pointing_coords {x,y} (the TARGET = tip),
  _widget_pointing_angle_deg (float), _widget_pointing_tip {x,y},
  _widget_pointing_description, _widget_pointing_until (epoch).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Optional

from tools.tool_registry import register_tool

WIDGET_TITLES = ("Iris Widget", "Ava Widget")  # current title first, legacy fallback
WIDGET_W = 150
WIDGET_H = 150
# Distance in px from window CENTER to the arrow TIP along the pointing
# direction. MEASURED from a live screenshot 2026-07-08: pointer morph
# (my*=2.2, calmness spread 1.0, cam z=2.8/fov70/150px) puts the visible
# tip ~90px from the orb center. First guess of 65 overshot the target by
# ~25px. Re-measure if the pointer morph or camera changes.
TIP_REACH_PX = 88


# ── Monitor enumeration ──────────────────────────────────────────────────────

class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


def enum_monitors() -> list[dict[str, Any]]:
    """Every attached monitor: virtual-desktop rect, work area, primary flag."""
    monitors: list[dict[str, Any]] = []
    u32 = ctypes.windll.user32

    MonitorEnumProc = ctypes.WINFUNCTYPE(
        wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM
    )

    def _cb(hmon, hdc, lprc, lparam):
        mi = _MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r, w = mi.rcMonitor, mi.rcWork
            monitors.append({
                "index": len(monitors),
                "primary": bool(mi.dwFlags & 1),  # MONITORINFOF_PRIMARY
                "device": str(mi.szDevice),
                "left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom,
                "width": r.right - r.left, "height": r.bottom - r.top,
                "work_left": w.left, "work_top": w.top,
                "work_right": w.right, "work_bottom": w.bottom,
            })
        return True

    u32.EnumDisplayMonitors(None, None, MonitorEnumProc(_cb), 0)
    return monitors


def monitor_for_point(x: int, y: int, monitors: Optional[list[dict]] = None) -> Optional[dict]:
    """Which monitor contains (x, y)? Falls back to the nearest one."""
    mons = monitors if monitors is not None else enum_monitors()
    if not mons:
        return None
    for m in mons:
        if m["left"] <= x < m["right"] and m["top"] <= y < m["bottom"]:
            return m
    # Nearest by clamped distance — an off-desktop point still resolves.
    def _d(m: dict) -> float:
        cx = min(max(x, m["left"]), m["right"] - 1)
        cy = min(max(y, m["top"]), m["bottom"] - 1)
        return math.hypot(x - cx, y - cy)
    return min(mons, key=_d)


def _mouse_pos() -> tuple[int, int]:
    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


# ── Widget window plumbing ───────────────────────────────────────────────────

def _find_widget_hwnd() -> tuple[Optional[int], str]:
    u32 = ctypes.windll.user32
    FindWindowW = u32.FindWindowW
    FindWindowW.restype = ctypes.c_void_p
    for title in WIDGET_TITLES:
        hwnd = FindWindowW(None, title)
        if hwnd:
            return int(hwnd), title
    return None, ""


def _client_geometry(hwnd: int) -> Optional[dict[str, Any]]:
    """The CLIENT area (the actual 150x150 orb canvas) in screen coords.
    Undecorated Tauri windows still carry invisible Win32 resize borders
    (measured 2026-07-08: outer 166x158, content inset 8px from the left,
    0 from the top) — the outer rect LIES about where the orb visually is.
    ClientToScreen + GetClientRect give the truth."""
    try:
        u32 = ctypes.windll.user32
        cr = wt.RECT()
        if not u32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(cr)):
            return None
        pt = wt.POINT(0, 0)
        if not u32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(pt)):
            return None
        cw, ch = cr.right - cr.left, cr.bottom - cr.top
        return {
            "left": int(pt.x), "top": int(pt.y),
            "width": int(cw), "height": int(ch),
            "center": {"x": int(pt.x) + cw // 2, "y": int(pt.y) + ch // 2},
        }
    except Exception:
        return None


def widget_rect() -> Optional[dict[str, Any]]:
    """Live rect + visibility of the widget window via Win32 (authoritative —
    no frontend self-report needed; the OS knows where the window is).
    `client` / `center` describe the VISIBLE orb canvas; the outer rect is
    included for completeness but includes invisible borders."""
    hwnd, title = _find_widget_hwnd()
    if not hwnd:
        return None
    u32 = ctypes.windll.user32
    r = wt.RECT()
    u32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    client = _client_geometry(hwnd)
    return {
        "hwnd": hwnd,
        "title": title,
        "visible": bool(u32.IsWindowVisible(ctypes.c_void_p(hwnd))),
        "left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom,
        "width": r.right - r.left, "height": r.bottom - r.top,
        "client": client,
        "center": (client or {}).get("center")
                  or {"x": (r.left + r.right) // 2, "y": (r.top + r.bottom) // 2},
    }


def _move_widget_center_to(cx: int, cy: int) -> bool:
    """Place the widget so the CLIENT-AREA (visible orb) center is (cx, cy).
    Topmost, no steal-focus. Accounts for the invisible border offset by
    measuring the live client geometry relative to the outer rect."""
    hwnd, _ = _find_widget_hwnd()
    if not hwnd:
        return False
    u32 = ctypes.windll.user32
    # Where does the client area sit inside the outer rect right now?
    r = wt.RECT()
    u32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    client = _client_geometry(hwnd)
    if client:
        off_x = client["left"] - r.left
        off_y = client["top"] - r.top
        x = int(cx) - client["width"] // 2 - off_x
        y = int(cy) - client["height"] // 2 - off_y
    else:
        x = int(cx) - WIDGET_W // 2
        y = int(cy) - WIDGET_H // 2
    # HWND_TOPMOST=-1; SWP_NOSIZE|SWP_NOACTIVATE|SWP_SHOWWINDOW
    flags = 0x0001 | 0x0010 | 0x0040
    u32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), x, y, 0, 0, flags)
    return True


def _save_position(g: dict[str, Any], x: int, y: int) -> None:
    try:
        base = Path(g.get("BASE_DIR") or ".")
        p = base / "state" / "widget_position.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"x": x, "y": y}, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Tip-anchored pointing math ───────────────────────────────────────────────

def _dir(theta_deg: float) -> tuple[float, float]:
    """Unit vector for angle (deg clockwise from screen-up) in screen coords."""
    a = math.radians(theta_deg)
    return math.sin(a), -math.cos(a)


def plan_pointing(tx: int, ty: int, prefer_angle: "float | None" = None) -> dict[str, Any]:
    """Choose an angle + widget-center placement so the arrow tip is AT (tx, ty)
    and the whole 150x150 widget body sits inside the target's monitor work area.

    prefer_angle (2026-07-13): explicit arrow direction (deg clockwise from
    screen-up) tried FIRST — lets the caller choose approach direction (all 8+
    directions demoable). Falls back to the standard candidate ring if the
    preferred placement doesn't fit the work area."""
    mons = enum_monitors()
    mon = monitor_for_point(tx, ty, mons)
    # Preference: diagonals first (least occluding), then cardinals.
    candidates: tuple = (315.0, 45.0, 225.0, 135.0, 0.0, 270.0, 90.0, 180.0)
    if prefer_angle is not None:
        candidates = (float(prefer_angle) % 360.0,) + candidates
    half_w, half_h = WIDGET_W // 2, WIDGET_H // 2
    chosen = None
    if mon:
        for theta in candidates:
            dx, dy = _dir(theta)
            cx = tx - dx * TIP_REACH_PX
            cy = ty - dy * TIP_REACH_PX
            if (mon["work_left"] <= cx - half_w and cx + half_w <= mon["work_right"]
                    and mon["work_top"] <= cy - half_h and cy + half_h <= mon["work_bottom"]):
                chosen = (theta, int(round(cx)), int(round(cy)))
                break
    if chosen is None:
        # Degenerate target (edge/corner past reach): clamp the center into the
        # monitor and aim the arrow from wherever it actually ends up.
        m = mon or {"work_left": 0, "work_top": 0, "work_right": 1920, "work_bottom": 1080}
        cx = min(max(tx, m["work_left"] + half_w), m["work_right"] - half_w)
        cy = min(max(ty, m["work_top"] + half_h), m["work_bottom"] - half_h)
        ddx, ddy = tx - cx, ty - cy
        theta = math.degrees(math.atan2(ddx, -ddy)) % 360.0 if (ddx or ddy) else 0.0
        chosen = (theta, int(cx), int(cy))
    theta, cx, cy = chosen
    return {
        "angle_deg": round(theta, 1),
        "center": {"x": cx, "y": cy},
        "tip": {"x": int(tx), "y": int(ty)},
        "monitor": (mon or {}).get("index"),
        "monitor_device": (mon or {}).get("device", ""),
    }


def point_widget_at(g: dict[str, Any], x: int, y: int,
                    duration_s: float = 8.0, description: str = "",
                    prefer_angle: "float | None" = None) -> dict[str, Any]:
    """Shared implementation — also called by iris_runtime.pointer_show."""
    duration_s = float(max(1.0, min(60.0, duration_s)))
    plan = plan_pointing(int(x), int(y), prefer_angle=prefer_angle)
    cx, cy = plan["center"]["x"], plan["center"]["y"]
    moved = _move_widget_center_to(cx, cy)
    _save_position(g, cx - WIDGET_W // 2, cy - WIDGET_H // 2)

    until = time.time() + duration_s
    g["_widget_pointing"] = True
    g["_widget_pointing_description"] = str(description or "")[:200]
    g["_widget_pointing_coords"] = {"x": int(x), "y": int(y)}
    g["_widget_pointing_angle_deg"] = plan["angle_deg"]
    g["_widget_pointing_tip"] = plan["tip"]
    g["_widget_pointing_until"] = until

    def _auto_clear():
        time.sleep(duration_s + 0.2)
        # Only clear if OUR until-stamp is still the active one.
        if abs(float(g.get("_widget_pointing_until") or 0.0) - until) < 0.01:
            for k in ("_widget_pointing", "_widget_pointing_coords",
                      "_widget_pointing_angle_deg", "_widget_pointing_tip",
                      "_widget_pointing_description", "_widget_pointing_until"):
                g.pop(k, None)

    threading.Thread(target=_auto_clear, daemon=True).start()
    return {
        "ok": True,
        "target": plan["tip"],
        "angle_deg": plan["angle_deg"],
        "widget_center": plan["center"],
        "monitor": plan["monitor"],
        "monitor_device": plan["monitor_device"],
        "widget_moved": moved,
        "duration_s": duration_s,
        "description": description,
    }


def clear_pointing(g: dict[str, Any]) -> None:
    for k in ("_widget_pointing", "_widget_pointing_coords",
              "_widget_pointing_angle_deg", "_widget_pointing_tip",
              "_widget_pointing_description", "_widget_pointing_until"):
        g.pop(k, None)


# ── Registered tools ─────────────────────────────────────────────────────────

def _tool_monitor_layout(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        mons = enum_monitors()
        mx, my = _mouse_pos()
        mmon = monitor_for_point(mx, my, mons)
        return {
            "ok": True,
            "monitors": mons,
            "mouse": {"x": mx, "y": my, "monitor": (mmon or {}).get("index")},
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tool_widget_status(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        mons = enum_monitors()
        rect = widget_rect()
        wmon = None
        if rect:
            wmon = monitor_for_point(rect["center"]["x"], rect["center"]["y"], mons)
        mx, my = _mouse_pos()
        mmon = monitor_for_point(mx, my, mons)
        pointing_active = bool(
            g.get("_widget_pointing")
            and float(g.get("_widget_pointing_until") or 0.0) > time.time()
        )
        return {
            "ok": True,
            "found": rect is not None,
            "widget": rect,
            "widget_monitor": (wmon or {}).get("index"),
            "widget_monitor_device": (wmon or {}).get("device", ""),
            "pointing": {
                "active": pointing_active,
                "target": g.get("_widget_pointing_coords"),
                "tip": g.get("_widget_pointing_tip"),
                "angle_deg": g.get("_widget_pointing_angle_deg"),
                "description": g.get("_widget_pointing_description") or "",
                "seconds_left": max(0.0, round(float(g.get("_widget_pointing_until") or 0.0) - time.time(), 1)) if pointing_active else 0.0,
            },
            "mouse": {"x": mx, "y": my, "monitor": (mmon or {}).get("index")},
            "monitors": mons,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tool_widget_point(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return {"ok": False, "error": "x and y (virtual-desktop pixels) required"}
        prefer = params.get("angle_deg")
        return point_widget_at(
            g, int(x), int(y),
            duration_s=float(params.get("duration_s") or 8.0),
            description=str(params.get("description") or ""),
            prefer_angle=(float(prefer) if prefer is not None else None),
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tool_widget_unpoint(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        clear_pointing(g)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tool_widget_pin(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Widget-up-while-app-up standing mode (Zeke directive 2026-07-08, built 07-13).

    pinned=true keeps the widget visible even while the main app window is up —
    so I can point at things without waiting for a minimize. pinned=false returns
    to the classic minimize-linked show/hide. App.tsx reads snapshot.widget.pinned."""
    try:
        pinned = bool(params.get("pinned", True))
        g["_widget_pinned"] = pinned
        return {"ok": True, "pinned": pinned,
                "note": "App.tsx applies within ~0.5s (widget poll loop)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


register_tool(
    name="monitor_layout",
    description="List all monitors (virtual-desktop rects, work areas, primary flag) plus current mouse position and which monitor it is on. Tier 1.",
    tier=1,
    handler=_tool_monitor_layout,
)

register_tool(
    name="widget_status",
    description="Widget orb proprioception: live window rect + visibility (Win32-authoritative), which monitor it is on, current pointing state (target/tip/angle), and mouse position+monitor. Tier 1.",
    tier=1,
    handler=_tool_widget_status,
)

register_tool(
    name="widget_point",
    description=(
        "Point the widget ARROW precisely at virtual-desktop pixel (x, y) on either monitor. "
        "Tip-anchored: places the widget so the arrow TIP lands on the target and publishes the "
        "rotation angle for the frontend. Params: x, y (required), duration_s (1-60, default 8), "
        "description (label for recall), angle_deg (optional preferred approach direction, deg "
        "clockwise from screen-up; falls back to auto if it doesn't fit). Tier 1."
    ),
    tier=1,
    handler=_tool_widget_point,
)

register_tool(
    name="widget_unpoint",
    description="Stop pointing immediately — clears all pointing state; widget morphs back to orb. Tier 1.",
    tier=1,
    handler=_tool_widget_unpoint,
)

register_tool(
    name="widget_pin",
    description="Pin the widget orb visible even while the main app is up (standing mode). Params: {pinned: bool=true}. pinned=false returns to minimize-linked show/hide. Tier 1.",
    tier=1,
    handler=_tool_widget_pin,
)
