# SELF_ASSESSMENT: I take screenshots when visual context matters — errors, games, navigation. I decide when it's worth it.
"""Phase 52 — Tier 1 screenshot tool."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from tools.tool_registry import register_tool


def _take_screenshot(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    reason = str(params.get("reason") or "unspecified").strip()[:200]
    category = str(params.get("category") or "general").strip()
    extract = bool(params.get("extract_knowledge", True))
    try:
        from brain.visual_episodic import EpisodicVisualMemory
        base = Path(g.get("BASE_DIR") or ".")
        mem = EpisodicVisualMemory(base_dir=base)
        path = mem.capture_and_store(reason, category)
        if path is None:
            return {"ok": False, "error": "screenshot capture failed"}
        knowledge = ""
        if extract:
            knowledge = mem.extract_knowledge(path, delete_after=True)
        return {"ok": True, "path": path, "knowledge": knowledge, "reason": reason}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


register_tool(
    name="take_screenshot",
    description="Capture a screenshot when visual context is useful. Tier 1 — use with judgment.",
    tier=1,
    handler=_take_screenshot,
)
