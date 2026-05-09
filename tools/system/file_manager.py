from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

PROJECT_ROOT = Path("D:/AvaAgentv2").resolve()


def _safe(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    rp = p.resolve()
    rp.relative_to(PROJECT_ROOT)
    return rp


def read_file(path: str) -> str:
    return _safe(path).read_text(encoding="utf-8", errors="replace")


def write_file(path: str, content: str) -> int:
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return len(content.encode("utf-8"))


def list_files(path: str = ".") -> list[dict[str, Any]]:
    p = _safe(path)
    if not p.is_dir():
        return []
    return [{"name": c.name, "is_dir": c.is_dir(), "size": c.stat().st_size if c.exists() else 0} for c in p.iterdir()][:500]


def search_files(path: str, pattern: str) -> list[str]:
    p = _safe(path)
    hits = glob.glob(str(p / "**" / pattern), recursive=True)
    return hits[:800]


def _tool_read(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        text = read_file(str(params.get("path") or ""))
        return {"ok": True, "content": text[:20000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tool_write(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        n = write_file(str(params.get("path") or ""), str(params.get("content") or ""))
        return {"ok": True, "bytes_written": n}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tool_list(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "items": list_files(str(params.get("path") or "."))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tool_search(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "matches": search_files(str(params.get("path") or "."), str(params.get("pattern") or "*"))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


register_tool("read_file", "Read a local text file.", 1, _tool_read)
register_tool("write_file", "Write a local text file.", 2, _tool_write)
register_tool("list_files", "List files in a directory.", 1, _tool_list)
register_tool("search_files", "Search files by glob pattern.", 1, _tool_search)

