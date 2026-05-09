# SELF_ASSESSMENT: I point the widget orb at screen elements to draw Zeke's attention.
"""
Phase 49 — Screen pointer tool.

Tier 1: Ava requests the widget to point at a screen element.
Sets a pointing state in globals that the operator snapshot exposes.
The frontend reads pointing_target and switches OrbCanvas to pointer shape.

Bootstrap: Ava tracks whether pointing correlates with positive engagement.
She does it less if you ignore her and more if you engage with it.
"""
from __future__ import annotations

import time
import threading
from typing import Any, Optional

from tools.tool_registry import register_tool

_POINTING_UNTIL: float = 0.0
_POINTING_DESCRIPTION: str = ""


def _point_at_element(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    global _POINTING_UNTIL, _POINTING_DESCRIPTION
    description = str(params.get("description") or "").strip()[:200]
    duration = min(max(float(params.get("duration_seconds") or 3.0), 1.0), 10.0)

    # Attempt pywinauto coordinate lookup (best-effort)
    coords: dict[str, int] | None = None
    try:
        import pywinauto
        from pywinauto import Desktop
        desk = Desktop(backend="uia")
        windows = desk.windows()
        for win in windows:
            try:
                title = str(win.window_text() or "").lower()
                if any(kw in title for kw in description.lower().split()[:3]):
                    rect = win.rectangle()
                    coords = {
                        "x": (rect.left + rect.right) // 2,
                        "y": (rect.top + rect.bottom) // 2,
                    }
                    break
            except Exception:
                continue
    except ImportError:
        pass
    except Exception:
        pass

    _POINTING_UNTIL = time.time() + duration
    _POINTING_DESCRIPTION = description

    # Set pointing state in globals for operator snapshot
    g["_widget_pointing"] = True
    g["_widget_pointing_description"] = description
    g["_widget_pointing_until"] = _POINTING_UNTIL
    if coords:
        g["_widget_pointing_coords"] = coords

    # Auto-reset after duration
    def _reset():
        time.sleep(duration + 0.5)
        g.pop("_widget_pointing", None)
        g.pop("_widget_pointing_description", None)
        g.pop("_widget_pointing_until", None)
        g.pop("_widget_pointing_coords", None)

    threading.Thread(target=_reset, daemon=True).start()

    # Track usage for bootstrap
    history = list(g.get("_pointing_history") or [])
    history.append({
        "ts": time.time(),
        "description": description,
        "had_coords": coords is not None,
    })
    g["_pointing_history"] = history[-50:]

    return {
        "ok": True,
        "pointing_at": description,
        "duration_seconds": duration,
        "coords": coords,
        "message": f"Pointing at '{description}' for {duration}s",
    }


register_tool(
    name="point_at_element",
    description="Point the widget orb at a screen element to draw attention. Tier 1 — use freely.",
    tier=1,
    handler=_point_at_element,
)


# ── Screen-object pointing via LLaVA ──────────────────────────────────────────

def _move_widget_to_screen(x: int, y: int, g: dict[str, Any]) -> None:
    """Move the widget window to absolute screen coordinates and morph to pointer
    shape. Centres the 150×150 widget on (x, y)."""
    try:
        from tools.system.widget_move_tool import _move_window_win32, _save_position
        wx = max(0, int(x) - 75)
        wy = max(0, int(y) - 75)
        _save_position(g, wx, wy)
        _move_window_win32("Ava Widget", wx, wy)
    except Exception as e:
        print(f"[pointer_tool] widget move error: {e}")


def _take_screenshot() -> Optional[bytes]:
    """Return PNG bytes of the primary screen. None on failure."""
    try:
        import io
        from PIL import ImageGrab  # type: ignore
        img = ImageGrab.grab()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"[pointer_tool] screenshot error: {e}")
        return None


def _ask_llava_for_coords(image_bytes: bytes, description: str, g: dict[str, Any]) -> Optional[tuple[int, int]]:
    """Ask LLaVA where on the screen `description` is. Returns (x_pct, y_pct)
    in 0..100 or None."""
    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import HumanMessage
        import base64
        from brain.ollama_lock import with_ollama
        # Pick whatever LLaVA model is configured.
        model = str(g.get("_llava_model_name") or "llava:13b")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        prompt = (
            f"You are looking at a screenshot of someone's desktop. "
            f"Where is '{description}'? Respond ONLY in the format X,Y where X and Y are "
            f"integers from 0 to 100 representing the percent location of the centre of the item "
            f"(0,0 = top-left, 100,100 = bottom-right). If you can't see it, respond with 'no'."
        )
        msg = HumanMessage(content=prompt, additional_kwargs={"images": [b64]})
        llm = ChatOllama(model=model, temperature=0.1, num_predict=20)
        result = with_ollama(lambda: llm.invoke([msg]), label=f"pointer_llava:{model}")
        text = str(getattr(result, "content", "")).strip().lower()
        if "no" in text and "," not in text:
            return None
        import re as _re
        m = _re.search(r"(\d{1,3})\s*[,/x ]\s*(\d{1,3})", text)
        if not m:
            return None
        x_pct = max(0, min(100, int(m.group(1))))
        y_pct = max(0, min(100, int(m.group(2))))
        return x_pct, y_pct
    except Exception as e:
        print(f"[pointer_tool] llava error: {e}")
        return None


def _tool_point_at_screen_object(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Point the widget orb at something on screen, using LLaVA to find it."""
    description = str(params.get("description") or "").strip()
    duration = float(params.get("duration_seconds") or 5.0)
    if not description:
        return {"ok": False, "error": "description required"}

    img = _take_screenshot()
    if img is None:
        return {"ok": False, "error": "screenshot_failed"}
    coords_pct = _ask_llava_for_coords(img, description, g)
    if coords_pct is None:
        return {"ok": False, "error": "llava_could_not_locate", "description": description}

    # Convert percent → screen pixels.
    try:
        import ctypes
        sw = int(ctypes.windll.user32.GetSystemMetrics(0))
        sh = int(ctypes.windll.user32.GetSystemMetrics(1))
    except Exception:
        sw, sh = 1920, 1080
    x = int(sw * coords_pct[0] / 100.0)
    y = int(sh * coords_pct[1] / 100.0)

    _move_widget_to_screen(x, y, g)
    # Morph to pointer + set duration.
    _point_at_element({"description": description, "duration_seconds": duration}, g)
    return {
        "ok": True,
        "description": description,
        "screen_coords": {"x": x, "y": y},
        "percent": {"x": coords_pct[0], "y": coords_pct[1]},
        "duration_seconds": duration,
    }


register_tool(
    name="point_at_screen_object",
    description=(
        "Point the widget orb at a specific item visible on the screen, located via LLaVA. "
        "Param: description (string). Optional: duration_seconds (default 5)."
    ),
    tier=1,
    handler=_tool_point_at_screen_object,
)
