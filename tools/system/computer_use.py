# SELF_ASSESSMENT: I drive Ava's computer-use layer — opening apps, clicking, typing, reading windows, controlling volume, and navigating File Explorer with safety guards.
"""tools/system/computer_use.py — Tier 2 tool registration for the
brain/windows_use/ orchestrator. Auto-loaded by tools.tool_registry.

These tools front-end the WindowsUseAgent so Ava can call them from
her tool routing layer the same way she calls open_app or web_search.
The orchestrator handles: deny-list, retry cascade, temporal-sense
hooks, event emission, TTS narration. These wrappers are just thin
JSON-result adapters.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _get_agent(g: dict[str, Any]):
    from brain.windows_use import get_agent
    return get_agent(g)


def _to_result(r) -> dict[str, Any]:
    try:
        return r.as_dict()
    except Exception:
        return {"ok": False, "error": "bad_result"}


def _tool_cu_open_app(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("app_name") or params.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "app_name required"}
    return _to_result(_get_agent(g).open_app(name, context=str(params.get("context") or "")))


def _tool_cu_click(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    win = str(params.get("window") or params.get("window_title") or "").strip()
    if not win:
        return {"ok": False, "error": "window required"}
    crit = params.get("control") or {}
    if isinstance(crit, str):
        crit = {"title": crit}
    return _to_result(_get_agent(g).click(win, crit, context=str(params.get("context") or "")))


def _tool_cu_type(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    win = str(params.get("window") or params.get("window_title") or "").strip()
    text = str(params.get("text") or "")
    if not win:
        return {"ok": False, "error": "window required"}
    return _to_result(_get_agent(g).type_text(win, text, context=str(params.get("context") or "")))


def _tool_cu_navigate(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    path = str(params.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "path required"}
    return _to_result(_get_agent(g).navigate(path, context=str(params.get("context") or "")))


def _tool_cu_set_volume(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        pct = int(params.get("percent") or params.get("level") or -1)
    except Exception:
        pct = -1
    if pct < 0 or pct > 100:
        return {"ok": False, "error": "percent must be 0-100"}
    return _to_result(_get_agent(g).set_volume(pct, context=str(params.get("context") or "")))


def _tool_cu_volume_up(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _to_result(_get_agent(g).volume_up())


def _tool_cu_volume_down(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _to_result(_get_agent(g).volume_down())


def _tool_cu_volume_mute(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _to_result(_get_agent(g).volume_mute())


def _tool_cu_read_window(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    win = str(params.get("window") or params.get("window_title") or "").strip()
    if not win:
        return {"ok": False, "error": "window required"}
    return _to_result(_get_agent(g).read_window(win))


def _tool_cu_list_apps(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        wins = _get_agent(g).list_running_apps()
        return {"ok": True, "windows": wins}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _tool_cu_clipboard_write(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    text = str(params.get("text") or "")
    return _to_result(_get_agent(g).clipboard_write(text, context=str(params.get("context") or "")))


def _tool_cu_clipboard_paste(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    win = str(params.get("window") or params.get("window_title") or "").strip()
    if not win:
        return {"ok": False, "error": "window required"}
    return _to_result(_get_agent(g).clipboard_paste(win, context=str(params.get("context") or "")))


def _tool_cu_type_clipboard(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Atomic clipboard-paste alternative to cu_type. Prefer for text >10 chars."""
    win = str(params.get("window") or params.get("window_title") or "").strip()
    text = str(params.get("text") or "")
    if not win:
        return {"ok": False, "error": "window required"}
    return _to_result(_get_agent(g).type_via_clipboard(win, text, context=str(params.get("context") or "")))


def _tool_cu_close_app(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Close app or browser tab(s) by name. Returns ok=False reason='ambiguous'
    with `candidates` populated if multiple kinds/processes match without an
    explicit target. The agent layer renders that as a 'which one?' question."""
    name = str(params.get("name") or params.get("app_name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    target = params.get("target")  # None | "desktop" | "browser_tab" | "all" | "<handle>"
    force = bool(params.get("force"))
    last_n = params.get("last_n")
    if isinstance(last_n, str) and last_n.isdigit():
        last_n = int(last_n)
    return _to_result(_get_agent(g).close_app(
        name, target=target if target is None else str(target),
        force=force, last_n=last_n if isinstance(last_n, int) else None,
        context=str(params.get("context") or ""),
    ))


register_tool(
    "cu_open_app",
    "Open an app via the computer-use layer (multi-strategy: PowerShell → Win-search → direct path; multi-attempt with retry; emits temporal-sense estimates and TTS narration on slow starts). Use this for any 'open X' request that needs full integration with Ava's awareness layers; for simple bare launches use open_app instead. Pass app_name string.",
    2,
    _tool_cu_open_app,
)

register_tool(
    "cu_click",
    "Click a control inside a window. window=substring of window title; control=dict with optional keys title/control_type/automation_id (any subset, all must match).",
    2,
    _tool_cu_click,
)

register_tool(
    "cu_type",
    "Type text into a window. window=substring of window title; text=string to type. Sends keystrokes to the focused control after bringing the window forward.",
    2,
    _tool_cu_type,
)

register_tool(
    "cu_navigate",
    "Open File Explorer at a path. Goes through the two-tier safety guard: refuses (and backs out) on protected paths; alerts once-per-session on sensitive prefixes. Pass path=absolute path string.",
    2,
    _tool_cu_navigate,
)

register_tool(
    "cu_set_volume",
    "Set system master volume to a percentage 0-100. Uses pycaw for precision.",
    1,
    _tool_cu_set_volume,
)

register_tool("cu_volume_up", "Nudge system volume up one step.", 1, _tool_cu_volume_up)
register_tool("cu_volume_down", "Nudge system volume down one step.", 1, _tool_cu_volume_down)
register_tool("cu_volume_mute", "Toggle system volume mute.", 1, _tool_cu_volume_mute)

register_tool(
    "cu_read_window",
    "Read visible text from a window via the accessibility tree. window=substring of window title. Returns concatenated control names and value-pattern values.",
    2,
    _tool_cu_read_window,
)

register_tool(
    "cu_list_apps",
    "List visible top-level windows. Returns [{title, handle}, ...].",
    1,
    _tool_cu_list_apps,
)

register_tool(
    "cu_clipboard_write",
    "Write text to the Windows clipboard. Pass text=string. Use as the first half of an atomic-paste flow when typing >10 characters into a window.",
    1,
    _tool_cu_clipboard_write,
)

register_tool(
    "cu_clipboard_paste",
    "Bring window forward and send Ctrl+V. Pass window=substring of window title. Pairs with cu_clipboard_write for atomic paste.",
    2,
    _tool_cu_clipboard_paste,
)

register_tool(
    "cu_type_clipboard",
    "Atomic alternative to cu_type — sets clipboard, focuses window, sends Ctrl+V, restores prior clipboard. ~50ms regardless of length. Prefer over cu_type for any text >10 characters. Pass window=substring of window title, text=string.",
    2,
    _tool_cu_type_clipboard,
)

register_tool(
    "cu_close_app",
    "Close an app or browser tab(s) by name. Pass name=substring of window title. Optional target='desktop' | 'browser_tab' | 'all' | '<window_handle>' to disambiguate. Optional force=true to terminate process. Optional last_n=N to close only the N most recent matching browser tabs. Returns ok=false reason='ambiguous' with candidates= when multiple kinds/processes match — Ava asks 'which one?' rather than guessing.",
    2,
    _tool_cu_close_app,
)
