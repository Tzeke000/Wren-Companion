# SELF_ASSESSMENT: I connect to Minecraft as a player. I develop my own playstyle — builder, fighter, or explorer.
"""
Phase 60/61 — Minecraft bot via mineflayer.

Python wrapper spawns ava_bot.js subprocess, communicates via stdin/stdout JSON.
Bootstrap: Ava develops her own playstyle; it emerges from what she actually does.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from tools.tool_registry import register_tool

_BOT_JS = Path(__file__).parent / "ava_bot.js"
_SESSIONS_FILE = Path("state/minecraft_sessions.jsonl")

_proc: Optional[subprocess.Popen] = None
_proc_lock = threading.Lock()
_pending: dict[int, Any] = {}
_cmd_id = 0
_reader_thread: Optional[threading.Thread] = None


def _start_bot_process() -> bool:
    global _proc, _reader_thread
    if not _BOT_JS.is_file():
        return False
    # Check node available
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=5)
    except Exception:
        return False
    try:
        _proc = subprocess.Popen(
            ["node", str(_BOT_JS)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(_BOT_JS.parent), text=True, bufsize=1,
        )
        _reader_thread = threading.Thread(target=_reader_loop, daemon=True)
        _reader_thread.start()
        return True
    except Exception:
        return False


def _reader_loop() -> None:
    global _proc
    if _proc is None:
        return
    for line in _proc.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            cmd_id = msg.get("id")
            if cmd_id is not None and cmd_id in _pending:
                _pending.pop(cmd_id, None)
                # Store last result
                _last_results[cmd_id] = msg
        except Exception:
            pass


_last_results: dict[int, Any] = {}


def _send_command(action: str, params: dict | None = None, timeout: float = 10.0) -> dict:
    global _cmd_id, _proc
    with _proc_lock:
        if _proc is None or _proc.poll() is not None:
            if not _start_bot_process():
                return {"ok": False, "error": "bot process unavailable (need node + mineflayer installed in tools/games/minecraft/)"}
        _cmd_id += 1
        cid = _cmd_id
        _pending[cid] = True
        try:
            msg = json.dumps({"id": cid, "action": action, "params": params or {}}) + "\n"
            _proc.stdin.write(msg)  # type: ignore[union-attr]
            _proc.stdin.flush()  # type: ignore[union-attr]
        except Exception as e:
            _pending.pop(cid, None)
            return {"ok": False, "error": str(e)[:200]}

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cid in _last_results:
            return _last_results.pop(cid)
        time.sleep(0.1)
    _pending.pop(cid, None)
    return {"ok": False, "error": "timeout"}


def _log_session(g: dict[str, Any], event: str, data: dict) -> None:
    base = Path(g.get("BASE_DIR") or ".")
    path = base / "state" / "minecraft_sessions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "event": event, **data}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _minecraft_connect(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    r = _send_command("connect", params, timeout=20.0)
    if r.get("ok"):
        _log_session(g, "connect", {"host": params.get("host"), "username": params.get("username")})
    return r


def _minecraft_state(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _send_command("get_state")


def _minecraft_chat(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    msg = str(params.get("message") or "")
    r = _send_command("chat", {"message": msg})
    _log_session(g, "chat", {"message": msg})
    return r


def _minecraft_move(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _send_command("move_to", params, timeout=30.0)


def _minecraft_look(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _send_command("look_at", params)


def _minecraft_attack(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _send_command("attack_entity", params)


def _minecraft_place(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _send_command("place_block", params)


def _minecraft_break(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _send_command("break_block", params, timeout=15.0)


def _minecraft_players(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _send_command("get_nearby_players")


def _minecraft_disconnect(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    r = _send_command("disconnect")
    _log_session(g, "disconnect", {})
    return r


register_tool("minecraft_connect", "Connect to a Minecraft server. host, port, username params.", 1, _minecraft_connect)
register_tool("minecraft_state", "Get current Minecraft bot state — position, health, nearby players.", 1, _minecraft_state)
register_tool("minecraft_chat", "Send a chat message in Minecraft.", 1, _minecraft_chat)
register_tool("minecraft_move_to", "Pathfind to coordinates (x,y,z). Tier 1.", 1, _minecraft_move)
register_tool("minecraft_look_at", "Turn to face coordinates (x,y,z).", 1, _minecraft_look)
register_tool("minecraft_attack", "Attack an entity by entity_id.", 1, _minecraft_attack)
register_tool("minecraft_place_block", "Place a block at (x,y,z).", 1, _minecraft_place)
register_tool("minecraft_break_block", "Mine a block at (x,y,z).", 1, _minecraft_break)
register_tool("minecraft_players", "List nearby players.", 1, _minecraft_players)
register_tool("minecraft_disconnect", "Disconnect from Minecraft server.", 1, _minecraft_disconnect)
