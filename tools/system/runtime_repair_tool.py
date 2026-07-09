# SELF_ASSESSMENT: I repair live-process wiring faults (module-global rebinds) without a restart.
"""
Runtime repair tool — surgical fixes for the running iris_runtime.

Born 2026-07-08: brain_hot_swap(module='brain.orb_http') RELOADED the module,
which re-executed its top-level `_g` placeholder assignment — so the FastAPI
snapshot routes (whose function objects share the module's globals dict) started
reading an EMPTY state dict. Symptom: snapshot served pointing=false, uptime
0H 0M, while the real _g (which iris_tool_call hands to every registry tool)
had the true state. The fix is a one-line rebind — but it must execute INSIDE
the runtime process, hence this tool.

LESSON (files under memory/ carry the full note): reload-based hot-swap resets
module-level state; any module whose globals are BOUND at start() needs a
rebind after reload, or hot_swap must re-run the binding.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool


def _rebind_orb_http_state(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Point brain.orb_http._g back at the LIVE runtime state dict (g), and
    reclaim state/iris.pid for this process. Idempotent — safe to re-run."""
    out: dict[str, Any] = {"ok": True}
    try:
        import brain.orb_http as m
        was_same = getattr(m, "_g", None) is g
        stale_keys = len(getattr(m, "_g", {}) or {})
        m._g = g
        out["orb_http_rebound"] = not was_same
        out["was_already_live"] = was_same
        out["stale_dict_keys"] = stale_keys
        out["live_dict_keys"] = len(g)
    except Exception as e:
        out["ok"] = False
        out["orb_http_error"] = str(e)
    try:
        pid_file = Path(g.get("BASE_DIR") or ".") / "state" / "iris.pid"
        prev = pid_file.read_text(encoding="utf-8").strip() if pid_file.is_file() else ""
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        out["pidfile_was"] = prev
        out["pidfile_now"] = os.getpid()
    except Exception as e:
        out["pidfile_error"] = str(e)
    return out


register_tool(
    name="rebind_orb_http_state",
    description=(
        "Repair: re-point brain.orb_http's module _g at the live runtime state dict and "
        "reclaim state/iris.pid. Use after any reload of brain.orb_http leaves the snapshot "
        "serving empty state (pointing=false, uptime 0H 0M). Idempotent. Tier 1."
    ),
    tier=1,
    handler=_rebind_orb_http_state,
)
