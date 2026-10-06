# SELF_ASSESSMENT: I add the app's extra read-only routes (tools list, Vector status/frame) to the RUNNING orb server.
"""app_routes — live-install brain.app_extra_routes on the served orb_http app (no module reload).

Born 2026-10-06 for Zeke's "your tool tab says 0 tools … add a vector one for your physical body". The same
install() runs at boot from brain.orb_http.start(), so this tool is only needed for a runtime that started
before the routes existed. Idempotent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool


def _app_routes(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        import importlib
        import brain.orb_http as m  # already loaded by the server — never reload THIS one (bound globals)
        from brain import app_extra_routes
        # app_extra_routes is stateless (pure functions), so reloading it is safe and is what lets routes
        # added to it after boot (e.g. the /api/v1/app/* set) land without a stack restart.
        app_extra_routes = importlib.reload(app_extra_routes)
        root = Path(__file__).resolve().parents[2]
        added = app_extra_routes.install(m.app, g, root)
        return {"ok": True, "added": added, "note": "already present" if not added else "installed live"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


register_tool(
    name="app_routes",
    description="Install the app's extra read-only routes (/api/v1/tools, /api/v1/vector/status, /api/v1/vector/frame, /api/v1/app/{scene,learning,people,proposals,brains}) on the running orb server. Idempotent. Tier 1.",
    tier=1,
    handler=_app_routes,
)
