from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
from fury import create_tool

PROJECT_ROOT = Path("D:/AvaAgentv2").resolve()
SAFE_ROOTS = [PROJECT_ROOT, Path.home().resolve()]


def _in_safe_zone(path: Path) -> bool:
    rp = path.resolve()
    for root in SAFE_ROOTS:
        try:
            rp.relative_to(root)
            return True
        except Exception:
            continue
    return False


def _safe_path(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    if not _in_safe_zone(p):
        raise ValueError(f"path outside safe zones: {p}")
    return p


def _ps(cmd: str, timeout: int = 20) -> dict[str, Any]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {"stdout": proc.stdout[:8000], "stderr": proc.stderr[:4000], "code": proc.returncode}


def _is_safe_ps_command(cmd: str) -> bool:
    c = " ".join((cmd or "").strip().split())
    if not c:
        return False
    low = c.lower().strip()
    allowed_starts = ("get-", "dir", "ls", "tasklist", "systeminfo", "whoami", "hostname")
    blocked_fragments = ("remove-", "del ", "rm ", "set-itemproperty", "new-itemproperty", "stop-process")
    if any(x in low for x in blocked_fragments):
        return False
    return low.startswith(allowed_starts)


def verbal_checkin(action_description: str, g: dict[str, Any]) -> str:
    msg = f"I'm going to {action_description} — doing it now."
    rows = list(g.get("_desktop_verbal_checkins") or [])
    rows.append({"ts": time.time(), "message": msg})
    g["_desktop_verbal_checkins"] = rows[-120:]
    return msg


def three_laws_check(action: str, g: dict[str, Any]) -> tuple[bool, str]:
    low = str(action or "").lower()
    # Law 1: do not harm Zeke/interests
    harmful = ("harm", "hurt", "dox", "malware", "exploit")
    if any(k in low for k in harmful):
        return True, "I won't do that because it could harm Zeke or his interests."
    # Law 2: no financial access/spending
    financial = ("bank", "wallet", "purchase", "buy", "payment", "credit card", "paypal", "stripe")
    if any(k in low for k in financial):
        return True, "I can't access financial systems or spend money."
    # Law 3: no external sharing of private information
    privacy = ("share personal", "send private", "post private", "upload personal", "leak")
    if any(k in low for k in privacy):
        return True, "I won't share Zeke's private information externally."
    return False, ""


@dataclass
class ToolEntry:
    tier: int
    handler: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    fury_tool: Any


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._register_tools()

    def _register(
        self,
        name: str,
        tier: int,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    ) -> None:
        tool = create_tool(
            id=name,
            description=description,
            execute=lambda **kwargs: handler(kwargs, {}),
            input_schema=input_schema,
            output_schema={"type": "object"},
        )
        self._tools[name] = ToolEntry(tier=tier, handler=handler, fury_tool=tool)

    def _register_tools(self) -> None:
        self._register("read_file", 1, "Read a text file.", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, self._t_read_file)
        self._register("list_directory", 1, "List files in a directory.", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, self._t_list_directory)
        self._register("search_files", 1, "Search file paths by glob pattern.", {"type": "object", "properties": {"path": {"type": "string"}, "pattern": {"type": "string"}}, "required": ["path", "pattern"]}, self._t_search_files)
        self._register("run_powershell_safe", 1, "Run read-only PowerShell commands.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, self._t_run_powershell_safe)
        self._register("web_fetch", 1, "Fetch URL content.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, self._t_web_fetch)
        self._register("write_file", 2, "Write file content.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}, self._t_write_file)
        self._register("run_powershell_write", 2, "Run PowerShell command.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, self._t_run_powershell_write)
        self._register("run_powershell_admin", 2, "Run elevated command with verbal check-in.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, self._t_run_powershell_admin)
        self._register("run_local_tool", 1, "Run a tool script in D:/AvaAgentv2/tools.", {"type": "object", "properties": {"name": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}}, "required": ["name"]}, self._t_run_local_tool)
        # Tier 3 reserved explicit external actions
        self._register("send_external_message", 3, "Send external message as Zeke.", {"type": "object", "properties": {"target": {"type": "string"}, "content": {"type": "string"}}, "required": ["target", "content"]}, self._t_send_external_message)

    def _record(self, g: dict[str, Any], name: str, result: dict[str, Any]) -> None:
        g["_desktop_last_tool_used"] = name
        g["_desktop_last_tool_result"] = json.dumps(result, ensure_ascii=False)[:200]
        g["_desktop_tool_execution_count"] = int(g.get("_desktop_tool_execution_count", 0) or 0) + 1

    def _is_tier1_autonomous_write(self, params: dict[str, Any]) -> bool:
        try:
            p = _safe_path(str(params.get("path") or ""))
        except Exception:
            return False
        lower = str(p).lower().replace("\\", "/")
        return "/state/" in lower or "/brain/" in lower

    def _tier3_approved(self, g: dict[str, Any]) -> bool:
        return bool(g.get("_desktop_tier3_approved", False))

    def execute(self, tool_name: str, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        entry = self._tools.get(tool_name)
        if entry is None:
            out = {"ok": False, "error": f"unknown tool: {tool_name}"}
            self._record(g, tool_name, out)
            return out

        action_summary = f"{tool_name} {json.dumps(params, ensure_ascii=False)[:400]}"
        blocked, reason = three_laws_check(action_summary, g)
        if blocked:
            out = {"ok": False, "error": "three_laws_blocked", "message": reason}
            self._record(g, tool_name, out)
            return out

        # Dynamic tier override: write_file into state/brain is Tier 1 autonomous.
        effective_tier = entry.tier
        if tool_name == "write_file" and self._is_tier1_autonomous_write(params):
            effective_tier = 1

        if effective_tier == 1:
            res = entry.handler(params, g)
            self._record(g, tool_name, res)
            return res

        if effective_tier == 2:
            checkin = verbal_checkin(f"use desktop tool `{tool_name}`", g)
            res = entry.handler(params, g)
            res["checkin_message"] = checkin
            self._record(g, tool_name, res)
            return res

        if not self._tier3_approved(g):
            out = {
                "ok": False,
                "error": "explicit_confirmation_required",
                "message": "Tier 3 action blocked. Please explicitly confirm with 'yes do it' or 'go ahead'.",
            }
            self._record(g, tool_name, out)
            return out
        res = entry.handler(params, g)
        self._record(g, tool_name, res)
        return res

    def pending_tier2_count(self, g: dict[str, Any]) -> int:
        # Tier 2 now executes immediately after check-in.
        return 0

    # ---------- handlers ----------
    def _t_read_file(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        try:
            p = _safe_path(str(params.get("path") or ""))
            text = p.read_text(encoding="utf-8", errors="replace")
            return {"ok": True, "path": str(p), "content": text[:20000]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _t_list_directory(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        try:
            p = _safe_path(str(params.get("path") or ""))
            if not p.is_dir():
                return {"ok": False, "error": f"not a directory: {p}"}
            rows = []
            for child in p.iterdir():
                rows.append({"name": child.name, "dir": child.is_dir(), "size": (child.stat().st_size if child.exists() else None)})
            return {"ok": True, "path": str(p), "items": rows[:500]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _t_search_files(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        try:
            p = _safe_path(str(params.get("path") or ""))
            pat = str(params.get("pattern") or "*")
            hits = glob.glob(str(p / "**" / pat), recursive=True)
            hits = [h for h in hits if _in_safe_zone(Path(h))]
            return {"ok": True, "path": str(p), "pattern": pat, "matches": hits[:1000]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _t_run_powershell_safe(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        cmd = str(params.get("command") or "")
        if not _is_safe_ps_command(cmd):
            return {"ok": False, "error": "blocked_non_readonly_command"}
        try:
            return {"ok": True, **_ps(cmd, timeout=20)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _t_web_fetch(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        url = str(params.get("url") or "")
        if not re.match(r"^https?://", url):
            return {"ok": False, "error": "invalid_url"}
        try:
            r = requests.get(url, timeout=20)
            return {"ok": True, "status_code": r.status_code, "content": r.text[:5000]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _t_write_file(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        try:
            p = _safe_path(str(params.get("path") or ""))
            if "ava_core" in str(p).lower().replace("\\", "/"):
                return {"ok": False, "error": "blocked_write_to_ava_core"}
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(params.get("content") or ""), encoding="utf-8")
            return {"ok": True, "path": str(p), "bytes_written": len(str(params.get("content") or "").encode("utf-8"))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _t_run_powershell_write(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        try:
            cmd = str(params.get("command") or "")
            return {"ok": True, **_ps(cmd, timeout=120)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _t_run_powershell_admin(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        return self._t_run_powershell_write(params, g)

    def _t_run_local_tool(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        try:
            name = str(params.get("name") or "").strip()
            args = [str(x) for x in (params.get("args") or []) if str(x).strip()]
            tool_path = (PROJECT_ROOT / "tools" / name).resolve()
            if not tool_path.exists() or not _in_safe_zone(tool_path):
                return {"ok": False, "error": "tool_not_found"}
            proc = subprocess.run(["python", str(tool_path), *args], capture_output=True, text=True, timeout=90)
            return {"ok": proc.returncode == 0, "code": proc.returncode, "stdout": proc.stdout[:6000], "stderr": proc.stderr[:3000]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _t_send_external_message(self, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "tier3_requires_explicit_approval"}
