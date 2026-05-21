# SELF_ASSESSMENT: I trigger a self-restart only after verifying the watchdog
# is alive AND a recent memory save exists. Refuses to trigger if either check
# fails. Replaces direct restart_self for the deployment-grade path.
"""
safe_restart tool (deployment failsafe layer 1).

Pre-checks before writing the watchdog trigger flag:
  1. Recent memory save (pre-restart contract) -- unchanged from request_restart.
  2. Watchdog heartbeat freshness (NEW 2026-05-20 night) -- aborts if watchdog
     is dead or zombie-shaped. Without this, writing the flag silently no-ops
     and CC sits dead until somebody notices.

If both checks pass, writes the trigger flag at .tmp/restart_cc.flag (the path
the watchdog actually polls). Watchdog detects within 2s and respawns.

If heartbeat is stale, returns failure with diagnostic info. Caller should:
  - Run scripts/iris_keepalive.ps1 manually (it'll respawn the watchdog), OR
  - Surface to user; do NOT loop-retry the flag write.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_BASE_DIR = Path(__file__).parent.parent.parent
_PRE_RESTART_MEMORY_WINDOW_S = 600
_HEARTBEAT_STALE_S = 30
_AUTO_MEMORY_DIR = Path(
    r"C:\Users\Owner\.claude\projects\D--Wren-Companion\memory"
)


def _check_recent_memory_save() -> dict[str, Any]:
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


def _check_watchdog_heartbeat(base: Path) -> dict[str, Any]:
    hb_path = base / ".tmp" / "watchdog_heartbeat.txt"
    if not hb_path.is_file():
        return {"ok": False, "age_seconds": None,
                "message": (
                    f"watchdog heartbeat file missing at {hb_path}. "
                    "Watchdog may not be running, or may be running an old "
                    "version without heartbeat support. Run "
                    "scripts/iris_keepalive.ps1 to repair before retrying."
                )}
    try:
        raw = hb_path.read_text(encoding="utf-8").strip()
        hb_ts = int(raw)
    except Exception as e:
        return {"ok": False, "age_seconds": None,
                "message": f"could not parse heartbeat ({e}). Run iris_keepalive.ps1 to repair."}
    age = time.time() - hb_ts
    if age > _HEARTBEAT_STALE_S:
        return {"ok": False, "age_seconds": age,
                "message": (
                    f"watchdog heartbeat stale (age {age:.1f}s; threshold "
                    f"{_HEARTBEAT_STALE_S}s). Watchdog process exists but "
                    "is not polling -- writing the trigger flag will silently "
                    "no-op. Run scripts/iris_keepalive.ps1 (or wait up to 5min "
                    "for the scheduled Iris-Keepalive task) to repair, then retry."
                )}
    return {"ok": True, "age_seconds": age,
            "message": f"watchdog heartbeat fresh ({age:.1f}s old)"}


def _safe_restart(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    reason = str(params.get("reason") or "iris_requested").strip()[:500]
    force = bool(params.get("force") or False)
    base = Path(g.get("BASE_DIR") or _BASE_DIR)

    # Check 1: memory save
    mem_check = _check_recent_memory_save()
    if not mem_check["ok"] and not force:
        return {
            "ok": False,
            "error": "pre_restart_save_required",
            "memory_check": mem_check,
            "watchdog_check": None,
            "message": mem_check["message"],
            "hint": (
                f"Save a handoff memory file to {_AUTO_MEMORY_DIR} "
                "(e.g. handoff_YYYY-MM-DD_<context>.md), update MEMORY.md, "
                "then retry. Or pass force=true to override (emergencies only)."
            ),
        }

    # Check 2: watchdog heartbeat
    wd_check = _check_watchdog_heartbeat(base)
    if not wd_check["ok"] and not force:
        return {
            "ok": False,
            "error": "watchdog_unhealthy",
            "memory_check": mem_check,
            "watchdog_check": wd_check,
            "message": wd_check["message"],
            "hint": (
                "The watchdog is the ONLY mechanism that processes the trigger "
                "flag. If it's stale, writing the flag does nothing. Fix the "
                "watchdog first (run iris_keepalive.ps1 or wait for the "
                "scheduled Iris-Keepalive task) before retrying."
            ),
        }

    # Both checks pass (or force=true) -- write the flag
    flag_path = base / ".tmp" / "restart_cc.flag"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(reason, encoding="utf-8")

    # Post-write verification: watchdog should consume the flag within ~2s
    # (poll interval) + maybe up to debounce. Poll for up to 10s for the flag
    # to disappear. If it sits, watchdog isn't acting on it (TOCTOU-stale
    # heartbeat, watchdog hung mid-poll, etc.) -- surface immediately rather
    # than letting the caller assume restart is in flight.
    deadline = time.time() + 10.0
    detection_seen = False
    detection_ts = None
    while time.time() < deadline:
        if not flag_path.exists():
            detection_seen = True
            detection_ts = time.time()
            break
        time.sleep(0.25)

    if detection_seen:
        return {
            "ok": True,
            "memory_check": mem_check,
            "watchdog_check": wd_check,
            "flag_written": str(flag_path),
            "flag_consumed_in_s": detection_ts - (deadline - 10.0),
            "message": (
                f"Restart flag written AND consumed by watchdog. "
                f"Restart is in progress. Reason: {reason}"
            ),
        }

    # Flag still sitting after 10s -- watchdog isn't processing it
    return {
        "ok": False,
        "error": "flag_not_consumed",
        "memory_check": mem_check,
        "watchdog_check": wd_check,
        "flag_written": str(flag_path),
        "flag_still_present_after_s": 10.0,
        "message": (
            f"Pre-flight checks passed and flag was written, but watchdog "
            f"has not consumed it within 10s. Watchdog may have died between "
            f"the heartbeat check and the flag write (TOCTOU), or may be "
            f"stuck in mid-loop. Flag is still at {flag_path} -- it WILL "
            f"trigger restart if/when watchdog recovers. To force recovery: "
            f"run scripts/iris_keepalive.ps1 manually."
        ),
        "hint": (
            "Iris-Keepalive Task Scheduler entry fires every 1 min -- "
            "wait up to ~1 min, then check whether the flag was consumed. "
            "Do NOT write the flag again (it's still present); a fresh "
            "write would just re-touch the same file."
        ),
    }


register_tool(
    name="safe_restart",
    description=(
        "Trigger watchdog-mediated CC restart with pre-flight safety checks. "
        "Verifies recent memory save AND watchdog heartbeat freshness before "
        "writing the trigger flag. Aborts and surfaces diagnostic if either "
        "check fails. Use this instead of mcp__iris__restart_self for any "
        "non-emergency restart -- it prevents the silent-no-op failure mode "
        "from a dead watchdog. Pass force=true to override checks (emergencies "
        "only). Reason string max 500 chars."
    ),
    tier=1,
    handler=_safe_restart,
)
