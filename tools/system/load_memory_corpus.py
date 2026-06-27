# SELF_ASSESSMENT: I load every .md file in the auto-memory directory and return their full content in one call, so post-restart-me can't shortcut the boot-ritual "read all memory" directive with per-file judgment.
"""
Atomic memory-corpus loader for the cold-wake boot ritual.

Why this exists: the cold-wake prompt's step 1 used to say "read EVERY .md
file in the memory dir." Per-file judgment is a surface where the cognition
can take a verification-shortcut on routine ops and silently load a partial
corpus. The failure already fired once on 2026-05-18 — I read 41 of 124
and named the tradeoff as "good enough." Zeke caught it; during overseas
nobody will.

The fix is to remove the per-file decision surface entirely. This tool
loads all .md files in one call. The cognition can't shortcut a single
tool call that's defined to read everything.

Returns the full corpus as {ok, count, dir, files: [{name, mtime_iso,
size_bytes, content}, ...]} sorted newest-first by mtime.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool


# Hardcoded fallback for Iris's own machine. Used only if the dynamic
# resolution paths below all miss. Per defensive-fallback-no-worse-than-
# before: if env/cwd-based resolution breaks, this still works on the
# machine the tool was originally built on.
_IRIS_FALLBACK = Path(r"C:\Users\Owner\.claude\projects\D--Wren-Companion\memory")


def _resolve_memory_dir() -> Path:
    """Resolve the auto-memory dir for the current Claude Code project.

    Claude Code stores per-project auto-memory at
    ~/.claude/projects/<encoded-project-path>/memory where encoded-path
    replaces ':' and '\\' / '/' with '-' (so 'D:\\Wren-Companion' becomes
    'D--Wren-Companion'). Resolving dynamically means this tool works on
    any sibling (Iris, Wren, Ava) without modification.

    Priority:
      1. CLAUDE_PROJECT_DIR env var if set and the derived path exists
      2. Path.cwd() with the same encoding
      3. _IRIS_FALLBACK (works on the machine this was built on)
    """
    home = Path.home()

    def _encode(p: str) -> str:
        # Mirror CC's project-dir encoding: replace : and slashes with -
        return re.sub(r"[:\\/]", "-", p).rstrip("-")

    proj_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj_dir:
        candidate = home / ".claude" / "projects" / _encode(proj_dir) / "memory"
        if candidate.exists() and candidate.is_dir():
            return candidate

    cwd_candidate = home / ".claude" / "projects" / _encode(str(Path.cwd())) / "memory"
    if cwd_candidate.exists() and cwd_candidate.is_dir():
        return cwd_candidate

    return _IRIS_FALLBACK


_MEMORY_DIR = _resolve_memory_dir()

# Repo root fallback for the profiles/ tree (Iris's people/things model). Same
# defensive-fallback rationale as _IRIS_FALLBACK: if env/cwd resolution misses,
# this still works on the machine this was built on.
_REPO_FALLBACK = Path(r"D:\Wren-Companion")


def _resolve_profiles_dir() -> Path:
    """Resolve the repo profiles/ tree (recursive people/things model). The
    profiles layer lives in the repo (not the auto-memory dir) but must be read
    into cognition on boot like the corpus — a profile no one reads is dead."""
    proj_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj_dir:
        c = Path(proj_dir) / "profiles"
        if c.exists() and c.is_dir():
            return c
    c = Path.cwd() / "profiles"
    if c.exists() and c.is_dir():
        return c
    return _REPO_FALLBACK / "profiles"


def _load_corpus(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Load every .md file in the auto-memory dir.

    Params (all optional):
        path: override the memory directory path. Defaults to the standard
            Iris auto-memory dir.
        include_index: include MEMORY.md (the index file). Default True.
        max_bytes_per_file: skip file bodies above this size (returns body=None
            with size_bytes set, so caller can read selectively). Default 0
            (no cap).
        list_only: return file list (name/path/mtime/size) with content=None
            for every entry. Use when verifying the input set before commiting
            to a full read — catches unexpected deletions, rotations, or
            corpus-shape changes. Default False.

    Returns:
        {ok, count, total_in_dir, dir, files: [...], errors: [...], duration_ms}

        Each file entry: {name, path, mtime_iso, size_bytes, content}
        Files are sorted newest-first by mtime.
        errors: list of {file, error} for any file that failed to read.
    """
    t0 = time.time()

    path_param = params.get("path") if isinstance(params, dict) else None
    memory_dir = Path(path_param) if path_param else _MEMORY_DIR
    include_index = params.get("include_index", True) if isinstance(params, dict) else True
    max_bytes_per_file = int(params.get("max_bytes_per_file", 0) or 0) if isinstance(params, dict) else 0
    list_only = bool(params.get("list_only", False)) if isinstance(params, dict) else False

    if not memory_dir.exists() or not memory_dir.is_dir():
        return {
            "ok": False,
            "error": f"memory dir not found: {memory_dir}",
            "duration_ms": int((time.time() - t0) * 1000),
        }

    all_md = list(memory_dir.glob("*.md"))
    if not include_index:
        all_md = [p for p in all_md if p.name != "MEMORY.md"]

    all_md.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    files: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for p in all_md:
        try:
            st = p.stat()
            size = st.st_size
            mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
            if list_only:
                content = None
            elif max_bytes_per_file and size > max_bytes_per_file:
                content = None
            else:
                content = p.read_text(encoding="utf-8", errors="replace")
            files.append({
                "name": p.name,
                "path": str(p),
                "mtime_iso": mtime_iso,
                "size_bytes": size,
                "content": content,
            })
        except Exception as e:
            errors.append({"file": p.name, "error": repr(e)})

    # ── Profiles layer (Iris, 2026-06-27) ───────────────────────────────────
    # The people/things model lives in a RECURSIVE tree under repo profiles/,
    # not the flat corpus, so include it here via rglob. FAIL-OPEN: any error
    # walking profiles leaves the corpus result intact (no-worse-than-before).
    # Boot's single "read everything" call must cover profiles too, or they're
    # dead files.
    profiles_count = 0
    try:
        profiles_dir = _resolve_profiles_dir()
        if profiles_dir.exists() and profiles_dir.is_dir():
            prof_md = sorted(profiles_dir.rglob("*.md"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            for p in prof_md:
                try:
                    st = p.stat()
                    size = st.st_size
                    if list_only:
                        content = None
                    elif max_bytes_per_file and size > max_bytes_per_file:
                        content = None
                    else:
                        content = p.read_text(encoding="utf-8", errors="replace")
                    files.append({
                        "name": f"profiles/{p.relative_to(profiles_dir).as_posix()}",
                        "path": str(p),
                        "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                        "size_bytes": size,
                        "content": content,
                        "kind": "profile",
                    })
                    profiles_count += 1
                except Exception as e:
                    errors.append({"file": str(p), "error": repr(e)})
    except Exception as e:
        errors.append({"file": "profiles/", "error": repr(e)})

    return {
        "ok": True,
        "count": len(files),
        "total_in_dir": len(all_md),
        "profiles_count": profiles_count,
        "dir": str(memory_dir),
        "files": files,
        "errors": errors,
        "duration_ms": int((time.time() - t0) * 1000),
    }


register_tool(
    name="load_memory_corpus",
    description=(
        "Atomically load every .md file in the Iris auto-memory directory "
        "and return their full content in one call. Sorted newest-first by "
        "mtime. Use this on cold-wake / boot-ritual step 1 instead of "
        "reading files individually — removes the per-file judgment surface "
        "where a verification-shortcut could leave the corpus partial. "
        "Returns {ok, count, total_in_dir, dir, files: [{name, path, "
        "mtime_iso, size_bytes, content}, ...], errors, duration_ms}. "
        "Params (all optional): path (override memory dir), include_index "
        "(include MEMORY.md, default True), max_bytes_per_file (skip "
        "bodies above this, returning content=null with size_bytes set), "
        "list_only (return file list with content=null for every entry — "
        "use to verify the input set before reading, default False)."
    ),
    tier=1,
    handler=_load_corpus,
)
