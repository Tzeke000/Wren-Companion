"""attention_arbiter — the wake/attention ownership readout (Iris_fixes #6 + addition A).

A readout, not a controller: it answers Zeke's seven questions (what wants my attention,
how important, why, needs cognition now?, can it wait?, already handled by another event?,
what did I choose not to act on?) from the situation ledger + the wake_salience ledger,
and shows the ownership table. `decide` runs the arbitration dry (record=false) so I can
see what a producer WOULD be told.

Actions: explain (default) | recent [n, situation] | ownership | decide {situation, source,
primary_alive?} (dry-run) | claim {situation, source, detail, wake}
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _attention_arbiter(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import attention_arbiter as arb
    except Exception as e:
        return {"ok": False, "error": f"import failed: {e!r}"}
    p = params or {}
    action = str(p.get("action") or "explain").strip().lower()
    try:
        if action == "explain":
            return {"ok": True, **arb.explain(g=g)}
        if action == "recent":
            return {"ok": True, "rows": arb.recent(int(p.get("n") or 30),
                                                    situation=(str(p.get("situation") or "") or None))}
        if action == "ownership":
            return {"ok": True, "owners": arb.OWNERSHIP, "host_eyes_alive_s": arb.HOST_EYES_ALIVE_S}
        if action == "decide":
            sit, src = str(p.get("situation") or ""), str(p.get("source") or "")
            if not sit or not src:
                return {"ok": False, "error": "situation and source required"}
            pa = p.get("primary_alive")
            return {"ok": True, "dry_run": True,
                    **arb.decide(sit, src, detail=str(p.get("detail") or ""),
                                 primary_alive=(None if pa is None else bool(pa)), record=False)}
        if action == "claim":
            sit, src = str(p.get("situation") or ""), str(p.get("source") or "")
            if not sit or not src:
                return {"ok": False, "error": "situation and source required"}
            return {"ok": True, "row": arb.claim(sit, src, detail=str(p.get("detail") or ""),
                                                  wake=bool(p.get("wake", True)))}
        return {"ok": False, "error": f"unknown action {action!r}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


register_tool(
    "attention_arbiter",
    "WAKE/ATTENTION OWNERSHIP readout (Iris_fixes #6): who owns each overlapping situation "
    "(camera vs wifi vs runtime channel), what wants attention now, what was a duplicate, what I "
    "chose not to act on. Actions: explain | recent | ownership | decide (dry-run) | claim.",
    1,
    _attention_arbiter,
)
