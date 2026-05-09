"""
Phase 46 — Hot-reload tool registry.

Tools that call register_tool() on import go live immediately when dropped into tools/.
A background FileWatcher polls tools/ every 5 seconds for new or modified .py files.

Optional SELF_ASSESSMENT block in a tool file (used if description not provided):
    # SELF_ASSESSMENT: I search the web for current information on any topic.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ToolResult = dict[str, Any]
ToolHandler = Callable[[dict[str, Any], dict[str, Any]], ToolResult]

_POLL_INTERVAL = 5.0
_TOOLS_ROOT = Path(__file__).parent


@dataclass
class ToolDef:
    name: str
    description: str
    tier: int
    handler: ToolHandler


_REGISTRY: dict[str, ToolDef] = {}
_REGISTRY_LOCK = threading.Lock()

# mtimes of loaded tool files to detect changes
_LOADED_MTIMES: dict[str, float] = {}
_RELOAD_LOG: list[dict[str, Any]] = []


def register_tool(name: str, description: str, tier: int, handler: ToolHandler) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY[name] = ToolDef(name=name, description=description, tier=int(tier), handler=handler)


def _read_self_assessment(path: Path) -> str:
    """Read optional # SELF_ASSESSMENT: line from a tool file."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:30]:
            m = re.match(r"#\s*SELF_ASSESSMENT:\s*(.+)", line.strip())
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return ""


def _try_load_file(path: Path) -> tuple[bool, str]:
    """Attempt to import/reload a tool file. Returns (success, message)."""
    rel = str(path.relative_to(_TOOLS_ROOT.parent)).replace("\\", "/").replace("/", ".").rstrip(".py")
    # Convert path to module name
    parts = path.relative_to(_TOOLS_ROOT.parent).with_suffix("").parts
    mod_name = ".".join(parts)

    before = set(_REGISTRY.keys())
    try:
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                return False, f"no spec for {path}"
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    except Exception as ex:
        return False, f"import error: {ex!r}"

    after = set(_REGISTRY.keys())
    new_tools = after - before
    _LOADED_MTIMES[str(path)] = path.stat().st_mtime
    msg = f"loaded {path.name}" + (f" — registered: {', '.join(sorted(new_tools))}" if new_tools else " (no new tools)")
    return True, msg


def _scan_and_reload() -> list[dict[str, Any]]:
    """Scan tools/ for new or modified .py files and reload them."""
    results: list[dict[str, Any]] = []
    skip = {"__init__.py", "tool_registry.py"}
    for py_file in sorted(_TOOLS_ROOT.rglob("*.py")):
        if py_file.name in skip or py_file.name.startswith("_"):
            continue
        try:
            mtime = py_file.stat().st_mtime
        except OSError:
            continue
        cached = _LOADED_MTIMES.get(str(py_file))
        if cached is not None and abs(mtime - cached) < 0.5:
            continue
        ok, msg = _try_load_file(py_file)
        entry = {"file": py_file.name, "ok": ok, "msg": msg, "ts": time.time()}
        results.append(entry)
        with _REGISTRY_LOCK:
            _RELOAD_LOG.append(entry)
            if len(_RELOAD_LOG) > 200:
                _RELOAD_LOG.pop(0)
        print(f"[tool_registry] hot-reload: {msg}")
    return results


def load_builtin_tools() -> None:
    """Load all tool files under tools/ — called once at startup."""
    _scan_and_reload()


class _FileWatcher(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="tool-registry-watcher", daemon=True)
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.wait(timeout=_POLL_INTERVAL):
            try:
                _scan_and_reload()
            except Exception as ex:
                print(f"[tool_registry] watcher error: {ex!r}")

    def stop(self) -> None:
        self._stop_evt.set()


_watcher: _FileWatcher | None = None


def start_file_watcher() -> None:
    global _watcher
    if _watcher is None or not _watcher.is_alive():
        _watcher = _FileWatcher()
        _watcher.start()


def reload_all_tools() -> list[dict[str, Any]]:
    """Force reload of all tool files. Called by POST /api/v1/tools/reload."""
    _LOADED_MTIMES.clear()
    return _scan_and_reload()


class ToolRegistry:
    def __init__(self) -> None:
        load_builtin_tools()
        start_file_watcher()

    def list_tools(self) -> list[dict[str, Any]]:
        with _REGISTRY_LOCK:
            return [
                {"name": d.name, "description": d.description, "tier": d.tier}
                for d in sorted(_REGISTRY.values(), key=lambda x: x.name)
            ]

    def get_tool(self, name: str) -> ToolDef | None:
        with _REGISTRY_LOCK:
            return _REGISTRY.get(str(name or "").strip())

    def execute_tool(self, name: str, params: dict[str, Any] | None, g: dict[str, Any]) -> ToolResult:
        tool = self.get_tool(name)
        if tool is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            result = tool.handler(params or {}, g)
            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        g["_desktop_last_tool_used"] = tool.name
        g["_desktop_last_tool_result"] = str(result)[:300]
        g["_desktop_tool_execution_count"] = int(g.get("_desktop_tool_execution_count", 0) or 0) + 1
        return result

    def get_reload_log(self) -> list[dict[str, Any]]:
        with _REGISTRY_LOCK:
            return list(_RELOAD_LOG[-50:])
