"""
Connectivity check tool — Tier 1.
SELF_ASSESSMENT: Tier 1 — Ava checks internet connectivity before attempting cloud tasks.
"""
from __future__ import annotations

from typing import Any

from config.ava_tuning import DEFAULT_MODEL_CAPABILITY_PROFILES


def _check_connectivity_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain.connectivity import get_monitor
        mon = get_monitor(g)
        online = mon.is_online()
        quality = mon.get_connection_quality()
        cloud_ok = mon.check_ollama_cloud() if online else False
    except Exception:
        online = bool(g.get("_is_online", False))
        quality = str(g.get("_connection_quality") or "offline")
        cloud_ok = bool(g.get("_ollama_cloud_reachable", False))

    cloud_models = [
        p.model_name
        for p in DEFAULT_MODEL_CAPABILITY_PROFILES
        if getattr(p, "requires_internet", False)
    ]
    return {
        "online": online,
        "quality": quality,
        "cloud_reachable": cloud_ok,
        "available_cloud_models": cloud_models,
        "last_check": g.get("_connectivity_last_check"),
    }


try:
    from tools.tool_registry import register_tool
    register_tool(
        "check_connectivity",
        "Check internet connectivity and cloud model availability.",
        1,
        _check_connectivity_fn,
    )
except Exception:
    pass
