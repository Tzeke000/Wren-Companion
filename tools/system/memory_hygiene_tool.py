"""memory_hygiene — Letta-style memory maintenance (brain/memory_hygiene.py).

audit        — mechanical checks over CORE, the hubs and CLAUDE.md: size vs cap, broken
               links, RESOLVED history in CORE, stale state-claims (probe them), relative
               dates, stacked corrections (append-instead-of-fix), unreachable notes.
dream_next   — the next unprocessed slice of the conversation transcript after my
               cursor + the Letta filter. I judge it and fix memory AT THE SOURCE.
dream_commit until_ts summary — advance the cursor (nothing reflected on twice).
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _mh(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    from brain import memory_hygiene as mh
    a = str(params.get("action") or "audit").lower()
    try:
        if a == "audit":
            r = mh.audit()
            n = int(params.get("max_findings", 25))
            r["findings"] = r["findings"][:n]
            return r
        if a == "dream_next":
            return mh.dream_next(max_items=int(params.get("max_items", 80)))
        if a == "dream_commit":
            return mh.dream_commit(float(params["until_ts"]), str(params.get("summary") or ""))
    except (ValueError, KeyError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"unknown action {a!r}"}


register_tool(
    "memory_hygiene",
    "LETTA-STYLE MEMORY MAINTENANCE: audit (size/links/stale claims/relative dates/stacked "
    "corrections/unreachable notes) | dream_next (unprocessed transcript slice + filter) | "
    "dream_commit until_ts summary.",
    1,
    _mh,
)
