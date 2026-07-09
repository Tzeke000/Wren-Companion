# SELF_ASSESSMENT: I start/inspect the unknown-face auto-capture watcher (my eyes' stranger-photographer).
"""
Tool bridge for brain/unknown_capture.py (Zeke directive 2026-07-09).

Exists so the watcher can be activated on a LIVE runtime via iris_tool_reload +
iris_tool_call (the voice_daemon_tool hot-reload pattern) — no restart needed.
Future boots start it from iris_bootstrap; this tool is then just status/restart.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _start(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    import importlib
    from brain import unknown_capture
    if bool(params.get("reload")):
        importlib.reload(unknown_capture)
    return {"ok": True, **unknown_capture.start(g)}


def _status(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    from brain import unknown_capture
    return {"ok": True, **unknown_capture.status(g)}


register_tool(
    name="unknown_capture_start",
    description=("Start (idempotently) the unknown-face auto-capture watcher: "
                 "known+unknown or multi-unknown faces held in frame -> auto photo "
                 "draft in faces/_drafts/ + 'unknown_capture' signal nudge. "
                 "Param reload=true to hot-reload the module first."),
    tier=1,
    handler=_start,
)

register_tool(
    name="unknown_capture_status",
    description="Status of the unknown-face auto-capture watcher (condition hold, cooldown, last event).",
    tier=1,
    handler=_status,
)
