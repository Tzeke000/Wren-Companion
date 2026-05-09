from __future__ import annotations

import subprocess
from typing import Any

from tools.tool_registry import register_tool


def list_processes(limit: int = 120) -> list[dict[str, Any]]:
    proc = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True, text=True, timeout=15)
    rows = [r.strip() for r in proc.stdout.splitlines() if r.strip()]
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for line in rows[1 : 1 + max(1, int(limit or 120))]:
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) >= 2:
            out.append({"name": cols[0], "pid": cols[1]})
    return out


def process_info(pid: int) -> dict[str, Any]:
    proc = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV"], capture_output=True, text=True, timeout=15)
    return {"stdout": proc.stdout[:4000], "stderr": proc.stderr[:1200], "code": proc.returncode}


def _tool_list(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "processes": list_processes(int(params.get("limit") or 120))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tool_info(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "info": process_info(int(params.get("pid") or 0))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


register_tool("list_processes", "List local running processes.", 1, _tool_list)
register_tool("process_info", "Get info for a process by PID.", 1, _tool_info)

