"""resilience — the self-healing supervisor for the month Zeke is away
(brain/resilience_supervisor.py; Zeke 10-05: "go ahead and do 2, 3 and 5").

status — running?, last tick, heals in budget windows, pairing codes alerted, stand-downs
start  — start the supervisor thread (also auto-started at boot via iris_bootstrap)
stop   — stop it (e.g. while hand-debugging the robot or the orb listener)
tick   — run one check pass now and return what it decided
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _resilience(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    from brain import resilience_supervisor as rs
    a = str(params.get("action") or "status").lower()
    sup = rs.get(g)
    if a == "start":
        sup.start(delay_s=float(params.get("delay_s", 0)))
        return {"ok": True, **rs.status()}
    if a == "stop":
        sup.stop()
        return {"ok": True, **rs.status()}
    if a == "tick":
        return {"ok": True, "tick": sup.tick()}
    return {"ok": True, **rs.status()}


register_tool(
    "resilience",
    "SELF-HEALING SUPERVISOR: Vector senses (vic-cloud wedge -> verified recipe), orb :5876 listener "
    "(restart_orb_http -> bounce), Discord pairing alert to Zeke (read-only). status | start | stop | tick.",
    1,
    _resilience,
)
