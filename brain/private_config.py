"""brain/private_config.py — the ONE place private identifiers come from (2026-10-07).

The repo is PUBLIC (Zeke 2026-10-07: "let's get rid of those on the repo"), so his Discord IDs, his
phone's MAC and the home network addresses no longer live in tracked code. They live in the
git-ignored file config/private.local.json:

    {"zeke_discord_user_id": "...", "zeke_dm_chat_id": "...", "phone_mac": "aa-bb-...",
     "lan_subnet": "192.168.x", "tower_lan": "...", "tower_tailnet": "...", "vector_ip": "...",
     "proxmox_host": "...", "iris_home_host": "...", "iris_home_tailnet": "...", "zorin_host": "..."}

An env var IRIS_PRIV_<KEY_UPPER> overrides the file. Missing key => the caller's default.
Standalone scripts that can't import `brain` read the same file with the same rules (see
scripts/_private.py)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PATH = Path(__file__).resolve().parents[1] / "config" / "private.local.json"
_cache: dict[str, Any] = {"mtime": None, "data": {}}


def _load() -> dict[str, Any]:
    try:
        m = PATH.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != m:
        try:
            _cache["data"] = json.loads(PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            _cache["data"] = {}
        _cache["mtime"] = m
    return _cache["data"]


def get(key: str, default: str = "") -> str:
    env = os.environ.get("IRIS_PRIV_" + key.upper())
    if env:
        return env
    v = _load().get(key)
    return str(v) if v not in (None, "") else default
