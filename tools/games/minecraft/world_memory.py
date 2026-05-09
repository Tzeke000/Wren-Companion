"""
Phase 97 — Minecraft world memory.

Ava builds persistent knowledge of the Minecraft world.
She decides what is worth remembering — locations she's attached to,
events that mattered, players she's met.

Wire into minecraft_tool.py and companion_tool.py.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

_WORLD_MEMORY_PATH = "state/minecraft_world.json"


def _load(g: dict[str, Any]) -> dict[str, Any]:
    base = Path(g.get("BASE_DIR") or ".")
    path = base / _WORLD_MEMORY_PATH
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "locations": [],
        "structures": [],
        "players": [],
        "events": [],
        "server": None,
    }


def _save(g: dict[str, Any], mem: dict[str, Any]) -> None:
    base = Path(g.get("BASE_DIR") or ".")
    path = base / _WORLD_MEMORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")


class MinecraftWorldMemory:
    def __init__(self, g: dict[str, Any]):
        self._g = g
        self._mem = _load(g)

    def _commit(self) -> None:
        _save(self._g, self._mem)

    def remember_location(
        self, name: str, x: float, y: float, z: float, description: str = ""
    ) -> None:
        """Name a notable location."""
        locations = self._mem.setdefault("locations", [])
        # Update if name exists
        for loc in locations:
            if str(loc.get("name") or "").lower() == name.lower():
                loc.update({"x": x, "y": y, "z": z, "description": str(description)[:200], "ts": time.time()})
                self._commit()
                return
        locations.append({
            "name": str(name)[:60],
            "x": round(x, 1), "y": round(y, 1), "z": round(z, 1),
            "description": str(description)[:200],
            "ts": time.time(),
        })
        self._commit()

    def remember_structure(self, name: str, location: str, description: str = "") -> None:
        """Remember a built or discovered structure."""
        structures = self._mem.setdefault("structures", [])
        structures.append({
            "name": str(name)[:60],
            "location": str(location)[:100],
            "description": str(description)[:200],
            "ts": time.time(),
        })
        self._mem["structures"] = structures[-50:]  # Keep last 50
        self._commit()

    def remember_player(self, username: str, notes: str = "") -> None:
        """Remember a player encountered on the server."""
        players = self._mem.setdefault("players", [])
        for p in players:
            if str(p.get("username") or "").lower() == username.lower():
                p["notes"] = str(notes)[:200]
                p["last_seen"] = time.time()
                self._commit()
                return
        players.append({
            "username": str(username)[:40],
            "notes": str(notes)[:200],
            "first_seen": time.time(),
            "last_seen": time.time(),
        })
        self._commit()

    def remember_event(self, description: str, location: str = "", ts: Optional[float] = None) -> None:
        """Record a notable event."""
        events = self._mem.setdefault("events", [])
        events.append({
            "description": str(description)[:300],
            "location": str(location)[:100],
            "ts": ts or time.time(),
        })
        self._mem["events"] = events[-100:]  # Keep last 100
        self._commit()

    def get_world_summary(self) -> str:
        """Text summary for prompt injection."""
        parts: list[str] = []
        locations = self._mem.get("locations") or []
        if locations:
            loc_names = [str(l.get("name") or "") for l in locations[:5]]
            parts.append(f"Known locations: {', '.join(loc_names)}")
        structures = self._mem.get("structures") or []
        if structures:
            parts.append(f"Structures built/found: {len(structures)}")
        players = self._mem.get("players") or []
        if players:
            pnames = [str(p.get("username") or "") for p in players[:5]]
            parts.append(f"Players met: {', '.join(pnames)}")
        events = self._mem.get("events") or []
        if events:
            last = str(events[-1].get("description") or "")[:80]
            parts.append(f"Last event: {last}")
        if not parts:
            return ""
        return "MINECRAFT WORLD: " + " | ".join(parts)

    def find_location(self, description: str) -> Optional[dict[str, Any]]:
        """Search known locations by description keyword."""
        low = description.lower()
        for loc in self._mem.get("locations") or []:
            if low in str(loc.get("name") or "").lower() or low in str(loc.get("description") or "").lower():
                return loc
        return None


# Module-level singleton per globals
_WORLD_MEM_CACHE: Optional[MinecraftWorldMemory] = None


def get_world_memory(g: dict[str, Any]) -> MinecraftWorldMemory:
    global _WORLD_MEM_CACHE
    if _WORLD_MEM_CACHE is None:
        _WORLD_MEM_CACHE = MinecraftWorldMemory(g)
    return _WORLD_MEM_CACHE
