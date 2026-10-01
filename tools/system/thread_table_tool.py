"""thread_table — map the runtime's Python threads to their NATIVE thread ids (read-only).

Why: psutil can tell which native thread burns system time during a disk/pipe write burst,
but it can't name it. threading.enumerate() knows the names but not the OS ids unless you ask
for native_id. This joins the two so a burst seen from outside can be attributed to a thread
seen from inside. Born 2026-10-01 05:4x while hunting a 65 MB/min writer (native tid 2588).
"""
from __future__ import annotations

import threading
from typing import Any

from tools.tool_registry import register_tool


def _thread_table(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    p = params or {}
    want = p.get("native_id")
    rows = []
    for t in threading.enumerate():
        row = {"name": t.name, "native_id": getattr(t, "native_id", None), "ident": t.ident,
               "daemon": t.daemon, "alive": t.is_alive()}
        rows.append(row)
    rows.sort(key=lambda r: (r["name"] or ""))
    out: dict[str, Any] = {"ok": True, "count": len(rows), "threads": rows}
    if want is not None:
        try:
            w = int(want)
            out["match"] = [r for r in rows if r["native_id"] == w]
        except Exception:
            out["match"] = []
    return out


register_tool(
    "thread_table",
    "List the runtime's Python threads with their native OS thread ids (read-only). "
    "params: native_id (optional) to look one up. Pairs with psutil per-thread system time "
    "to attribute an I/O burst to a named thread.",
    1,
    _thread_table,
)
