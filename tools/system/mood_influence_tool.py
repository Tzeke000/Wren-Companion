"""mood_influence — inspect how my mood is biasing behaviour (brain/mood_influence.py).

CTEM-borrowed (10-05): mood biases bounded decisions, and every influence is logged
so the loop is inspectable. status = modifiers now, my learned baselines, the last
factor applied per decision; recent = the trace.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _mood_influence(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    from brain import mood_influence as mi
    a = str(params.get("action") or "status").lower()
    if a == "recent":
        return {"ok": True, "rows": mi.recent(int(params.get("n", 20)))}
    return {"ok": True, **mi.status()}


register_tool(
    "mood_influence",
    "MOOD -> BEHAVIOUR TRACE: which mood modifier biased which decision, by how much "
    "(bounded, relative to my own learned baseline). status | recent n.",
    1,
    _mood_influence,
)
