"""self_claim_check — fire the always-loaded-claim freshness check (Iris_fixes #3).

CLAUDE.md outranks memory because it always loads, so a stale state-claim in it is
a standing wrong instruction (the ~month where it told me to leave a live voice
dead). CLAUDE.md's rule is "fix a claim the session a probe contradicts it" — this
tool is what FIRES that rule instead of relying on me to re-probe by luck.

Meant to run at boot and on the 3-hourly self-cron. On drift it returns the exact
CLAUDE.md line and what a live probe says, so the fix is a one-line edit.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _self_claim_check(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import self_claims as sc
    except Exception as e:
        return {"ok": False, "error": f"import failed: {e!r}"}
    r = sc.check_all()
    drifted = [c for c in r["claims"] if c["drift"]]
    unknown = [c for c in r["claims"] if not c["drift"] and c.get("doc_says") is None]
    summary = ("DRIFT — a CLAUDE.md self-claim no longer matches reality; fix it this session"
               if r["drift"] else "all always-loaded self-claims match live reality")
    out = {"ok": True, "drift": r["drift"], "summary": summary, "claims": r["claims"]}
    if drifted:
        out["fix_now"] = [c["action"] for c in drifted]
    if unknown:
        out["regex_misses"] = [c["id"] for c in unknown]
    return out


register_tool(
    "self_claim_check",
    "FRESHNESS-CHECK the always-loaded self-claims in CLAUDE.md against live probes "
    "(voice on/off, ports listening, eyes present, wake-by-design). Catches a stale "
    "state-claim that would misinstruct a future me (the month CLAUDE.md said voice "
    "was OFF while it was live). On drift returns the exact line to fix. Run at boot "
    "+ on the self-cron.",
    1,
    _self_claim_check,
)
