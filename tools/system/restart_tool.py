# SELF_ASSESSMENT: I request a restart of the Ava process when core changes need to take effect.
"""
Phase 47 — Tier 1 restart request tool.

Ava writes a flag file to request watchdog-mediated restart.
She should develop her own judgment about when restart is warranted.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_BASE_DIR = Path(__file__).parent.parent.parent
_PRE_RESTART_MEMORY_WINDOW_S = 600
_AUTO_MEMORY_DIR = Path(
    r"C:\Users\Owner\.claude\projects\D--Wren-Companion\memory"
)


def _check_recent_memory_save() -> dict[str, Any]:
    # Pre-restart contract (Zeke 2026-05-17): never restart without
    # saving to memory first. See pre_restart_save_and_boot_sequence.md.
    if not _AUTO_MEMORY_DIR.is_dir():
        return {"ok": False, "age_seconds": None, "file": None,
                "message": f"memory dir not found at {_AUTO_MEMORY_DIR}"}
    now = time.time()
    most_recent_ts = 0.0
    most_recent_name = ""
    for f in _AUTO_MEMORY_DIR.glob("*.md"):
        try:
            mt = f.stat().st_mtime
        except Exception:
            continue
        if mt > most_recent_ts:
            most_recent_ts = mt
            most_recent_name = f.name
    if most_recent_ts == 0.0:
        return {"ok": False, "age_seconds": None, "file": None,
                "message": f"no .md files under {_AUTO_MEMORY_DIR}"}
    age = now - most_recent_ts
    if age > _PRE_RESTART_MEMORY_WINDOW_S:
        return {"ok": False, "age_seconds": age, "file": most_recent_name,
                "message": (
                    f"pre-restart contract violation: most recent memory "
                    f"file ({most_recent_name}) is {age/60:.1f}min old; "
                    f"required within "
                    f"{_PRE_RESTART_MEMORY_WINDOW_S//60}min. Save handoff "
                    f"first, or pass force=true to override (emergency only)."
                )}
    return {"ok": True, "age_seconds": age, "file": most_recent_name,
            "message": f"pre-restart save verified ({most_recent_name}, {age:.0f}s old)"}


def _request_restart(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    reason = str(params.get("reason") or "ava_requested").strip()[:300]
    force = bool(params.get("force") or False)
    base = Path(g.get("BASE_DIR") or _BASE_DIR)

    mem_check = _check_recent_memory_save()
    if not mem_check["ok"] and not force:
        return {
            "ok": False,
            "error": "pre_restart_save_required",
            "memory_check": mem_check,
            "message": mem_check["message"],
            "hint": (
                f"Save a handoff memory file to {_AUTO_MEMORY_DIR} "
                "(e.g. handoff_YYYY-MM-DD_<context>.md), update MEMORY.md, "
                "then retry. Or pass force=true to override (emergencies only)."
            ),
        }

    state_dir = base / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    flag_path = state_dir / "restart_requested.flag"
    flag_path.write_text(reason, encoding="utf-8")

    # Save pickup note with restart context
    pickup_path = state_dir / "pickup_note.json"
    pickup = {
        "restart_reason": reason,
        "requested_ts": time.time(),
        "requested_by": "ava_tool",
    }
    try:
        existing = json.loads(pickup_path.read_text(encoding="utf-8")) if pickup_path.is_file() else {}
        if isinstance(existing, dict):
            pickup.update({k: v for k, v in existing.items() if k not in pickup})
    except Exception:
        pass
    pickup_path.write_text(json.dumps(pickup, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "ok": True,
        "message": f"Restart requested. Watchdog will handle it. Reason: {reason}",
        "flag_written": str(flag_path),
    }


register_tool(
    name="request_restart",
    description="Request a graceful restart of the Ava process via watchdog. Use when core changes need to take effect.",
    tier=1,
    handler=_request_restart,
)
