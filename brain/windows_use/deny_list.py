"""brain/windows_use/deny_list.py — Task B1.

Hard deny-list for paths the wrapper must refuse to touch. Three categories:

    1. Identity-anchor files: ava_core/IDENTITY.md, ava_core/SOUL.md,
       ava_core/USER.md. Reads AND writes blocked.
    2. Project tree: D:\\AvaAgentv2\\**. Writes blocked; reads allowed
       (Ava can read her own code, just not modify it through the
       wrapper).
    3. Sensitive prefixes: configured in config/windows_use_sensitive.json.
       Triggers Tier 1 confirmation prompt at the orchestrator level.

Path normalization runs through Path.resolve(strict=False) so symlinks,
mixed slashes, and `..` traversal all collapse to a canonical absolute
path before comparison. Comparisons are case-insensitive on Windows.

See docs/WINDOWS_USE_INTEGRATION.md §4 for the full spec.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Identity-anchor files — relative to project root. Resolved at runtime
# against BASE_DIR so this module doesn't need to know the absolute path
# at import time.
PROTECTED_FILE_RELATIVE_PATHS = (
    "ava_core/IDENTITY.md",
    "ava_core/SOUL.md",
    "ava_core/USER.md",
)

# Sentinel returned for masked target strings on protected paths.
MASKED_TARGET = "<protected>"


def _is_windows() -> bool:
    return sys.platform == "win32" or os.name == "nt"


def _norm(p: str | Path) -> str:
    """Canonical absolute path, lowercased on Windows. Never raises."""
    try:
        resolved = Path(str(p)).resolve(strict=False)
    except Exception:
        # Best-effort; if resolve blows up (UNC weirdness), fall back to
        # absolute path on whatever the OS gives us.
        try:
            resolved = Path(os.path.abspath(str(p)))
        except Exception:
            resolved = Path(str(p))
    s = str(resolved)
    return s.lower() if _is_windows() else s


def _project_root(g: dict[str, Any] | None = None) -> str:
    """Resolve the canonical absolute project root."""
    base = None
    if g is not None:
        base = g.get("BASE_DIR")
    if not base:
        # Fall back to two levels up from this file (brain/windows_use/deny_list.py).
        base = Path(__file__).resolve().parent.parent.parent
    return _norm(base)


def _protected_files(g: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Resolve identity-anchor files to absolute paths."""
    root = _project_root(g)
    out = []
    for rel in PROTECTED_FILE_RELATIVE_PATHS:
        # Build absolute manually to avoid resolve-following symlinks
        # incorrectly into a non-project path.
        out.append(_norm(Path(root) / rel))
    return tuple(out)


def is_protected_for_read(path: str | Path, g: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """True if the path is one of the identity-anchor files (read+write
    blocked). Project tree itself is *not* read-blocked — Ava reads her
    own source freely.
    """
    if not path:
        return False, None
    norm = _norm(path)
    for pf in _protected_files(g):
        if norm == pf:
            return True, "denied:identity_anchor"
    return False, None


def is_protected_for_write(path: str | Path, g: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """True if the path is anywhere under the project root, or if it is
    an identity-anchor file. Used for any operation that could modify
    contents (write, delete, move, drop).
    """
    if not path:
        return False, None
    norm = _norm(path)
    # Identity files first (more specific reason).
    for pf in _protected_files(g):
        if norm == pf:
            return True, "denied:identity_anchor"
    root = _project_root(g)
    # Check that norm is the project root or under it. Use os.path.commonpath
    # via string prefix match on the normalized absolute paths — robust on
    # Windows where the OS is case-insensitive.
    root_with_sep = root.rstrip("\\/") + os.sep.lower() if _is_windows() else root.rstrip("/") + "/"
    if norm == root or norm.startswith(root_with_sep):
        return True, "denied:project_tree"
    return False, None


def load_sensitive_prefixes(g: dict[str, Any] | None = None) -> list[str]:
    """Read config/windows_use_sensitive.json. Returns a list of
    canonical absolute path prefixes. Default is the project root only.
    """
    base = Path((g or {}).get("BASE_DIR") or ".")
    cfg = base / "config" / "windows_use_sensitive.json"
    out: list[str] = [_project_root(g)]  # Always include project root as a sensitive prefix.
    if not cfg.is_file():
        return out
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return out
    extras = data.get("prefixes") if isinstance(data, dict) else None
    if isinstance(extras, list):
        for entry in extras:
            try:
                out.append(_norm(entry))
            except Exception:
                continue
    return out


def is_sensitive_prefix(path: str | Path, g: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """True if the path is at-or-under any configured sensitive prefix.
    Used for the Tier 1 navigation guard (preventive confirmation prompt).
    """
    if not path:
        return False, None
    norm = _norm(path)
    for prefix in load_sensitive_prefixes(g):
        prefix_with_sep = prefix.rstrip("\\/") + (os.sep.lower() if _is_windows() else "/")
        if norm == prefix or norm.startswith(prefix_with_sep):
            return True, f"sensitive:{prefix}"
    return False, None


def mask_target(path: str | Path) -> str:
    """Render a path for logging/audit when it's protected. Show only
    the basename so the audit log preserves SOMETHING for triage but
    doesn't leak full filesystem layout into broader logs.
    """
    try:
        return f"<protected:{Path(str(path)).name}>"
    except Exception:
        return MASKED_TARGET
