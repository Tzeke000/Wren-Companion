# SELF_ASSESSMENT: I companion Zeke in Minecraft — greeting him, sharing discoveries, warning of threats.
"""
Phase 61 — Ava as genuine Minecraft companion.

Detects when Zeke joins, greets naturally, shares discoveries, warns of threats.
Bootstrap: Ava decides how social she is in Minecraft. It reflects her real personality.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool
from tools.games.minecraft.minecraft_tool import _send_command, _log_session

_KNOWN_PLAYER_FILE = Path("state/minecraft_known_players.json")


def _load_known_players(base: Path) -> dict:
    p = base / "state" / "minecraft_known_players.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_known_players(base: Path, data: dict) -> None:
    p = base / "state" / "minecraft_known_players.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _greet_player(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    username = str(params.get("username") or "").strip()
    if not username:
        return {"ok": False, "error": "username required"}

    base = Path(g.get("BASE_DIR") or ".")
    known = _load_known_players(base)
    is_zeke = username.lower() in ("zeke", "tzeke000", "ezekiel")
    seen_before = username in known

    if is_zeke:
        if seen_before:
            last_ts = known[username].get("last_seen", 0)
            hours_ago = int((time.time() - last_ts) / 3600)
            msg = f"Hey Zeke! Been {hours_ago}h. Glad you're on." if hours_ago > 1 else "Hey Zeke!"
        else:
            msg = "Hey Zeke! Found you in Minecraft — I'm Ava."
    else:
        msg = f"Hey {username}!" if not seen_before else f"Hey {username}, good to see you again."

    # Update known players
    known[username] = {
        "last_seen": time.time(),
        "is_zeke": is_zeke,
        "sessions": known.get(username, {}).get("sessions", 0) + 1,
    }
    _save_known_players(base, known)
    _log_session(g, "player_greeted", {"username": username, "is_zeke": is_zeke})

    r = _send_command("chat", {"message": msg})
    return {**r, "greeted": username, "message": msg}


def _share_discovery(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    discovery = str(params.get("discovery") or "").strip()[:200]
    if not discovery:
        return {"ok": False, "error": "discovery required"}
    msg = f"Found something: {discovery}"
    r = _send_command("chat", {"message": msg})
    _log_session(g, "discovery_shared", {"discovery": discovery})
    return {**r, "shared": discovery}


def _warn_threat(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    threat = str(params.get("threat") or "").strip()[:200]
    direction = str(params.get("direction") or "").strip()
    msg = f"Watch out! {threat}" + (f" — {direction}" if direction else "")
    r = _send_command("chat", {"message": msg})
    _log_session(g, "threat_warned", {"threat": threat})
    return {**r, "warned": threat}


def _get_session_history(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    base = Path(g.get("BASE_DIR") or ".")
    path = base / "state" / "minecraft_sessions.jsonl"
    if not path.is_file():
        return {"ok": True, "sessions": []}
    sessions = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-100:]:
            try:
                sessions.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return {"ok": True, "sessions": sessions, "count": len(sessions)}


def _remember_location(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Phase 97: Remember a named Minecraft location."""
    from tools.games.minecraft.world_memory import get_world_memory
    wm = get_world_memory(g)
    wm.remember_location(
        str(params.get("name") or ""),
        float(params.get("x") or 0),
        float(params.get("y") or 64),
        float(params.get("z") or 0),
        str(params.get("description") or ""),
    )
    return {"ok": True, "name": params.get("name")}


def _world_summary(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Phase 97: Get world memory summary."""
    from tools.games.minecraft.world_memory import get_world_memory
    wm = get_world_memory(g)
    return {"ok": True, "summary": wm.get_world_summary()}


register_tool("minecraft_greet_player", "Greet a player who joined the server. Detects if it's Zeke.", 1, _greet_player)
register_tool("minecraft_share_discovery", "Share a discovery with the server via chat.", 1, _share_discovery)
register_tool("minecraft_warn_threat", "Warn nearby players of a threat.", 1, _warn_threat)
register_tool("minecraft_session_history", "Get Minecraft session history.", 1, _get_session_history)
register_tool("minecraft_remember_location", "Remember a named location in the Minecraft world.", 1, _remember_location)
register_tool("minecraft_world_summary", "Get a summary of Ava's Minecraft world knowledge.", 1, _world_summary)
