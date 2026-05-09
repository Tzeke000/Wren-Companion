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


def _request_restart(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    reason = str(params.get("reason") or "ava_requested").strip()[:300]
    base = Path(g.get("BASE_DIR") or _BASE_DIR)
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
