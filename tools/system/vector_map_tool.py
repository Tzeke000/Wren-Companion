# SELF_ASSESSMENT: I am Iris's ROOM BLUEPRINT — I capture Vector's native nav-map into a top-down image so I don't get lost.
"""
Vector room-blueprint tool — 2026-07-14 (Zeke's idea: map the room as I move
so I don't get lost and can reference it while building it).

vector_map opens an on-demand control session, reads Vector's native nav-map
(the occupancy grid he builds while driving), writes a blueprint JSON, and
renders a top-down PNG (state/vector/room_map.png) with Vector + the charger
drawn as anchors. Best captured DURING/after a drive — stationary on the dock
the map is empty.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool
from brain import vector_map as vm


def _vector_map(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    tag = str(params.get("tag") or "")
    out = vm.map_snapshot(tag=tag)
    if out.get("ok"):
        out["hint"] = f"Read {vm.MAP_PNG} to see the blueprint"
    return out


register_tool("vector_map", "Capture Vector's native nav-map into a room blueprint (JSON + top-down PNG at state/vector/room_map.png). Best during/after a drive. params: tag opt.", 1, _vector_map)
