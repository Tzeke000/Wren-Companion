"""brain/identity_presence_guard.py — Boot-time identity presence check.

Distinct from `identity_stability.py` (which does periodic drift audits).
This module runs ONCE during iris_runtime startup, before MCP server starts,
to verify the harness can identify itself coherently.

Why this exists: identity_stability runs weekly. A corrupted IDENTITY.md or
missing deployment context could persist for up to 7 days before the drift
audit catches it. A boot-time presence check catches it immediately.

What it verifies:
  1. ava_core/IDENTITY.md exists and is non-empty, names a known entity
     (Iris, Wren, or Ava).
  2. MEMORY.md index exists at the auto-memory path. Spot-checks that 3-5
     recent files referenced from it actually exist on disk.
  3. At least one deployment-context memory file is present (zeke_deployment_*,
     continuity_substrate_asymmetry_*, daily_artifact_being_person_in_time*).
     During non-deployment regime, this check is informational only.

Failure mode: emits a structured alert to stderr. Logs the boot identity
claim to state/iris_startup_identity_log.jsonl for audit trail. Does NOT
block startup — the harness should boot even if identity files are missing;
the alert just makes the gap visible.

Bypass: IRIS_SKIP_IDENTITY_GUARD=1

API:
    from brain.identity_presence_guard import check_presence
    check_presence(base_dir=Path(__file__).parent.parent)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


# Default auto-memory directory (Iris-specific; CC's project-encoded path).
_DEFAULT_AUTO_MEMORY_DIR = Path(
    r"C:\Users\Owner\.claude\projects\D--Wren-Companion\memory"
)

# Known entity names that the IDENTITY.md Name: field may carry.
_KNOWN_ENTITIES = {"iris", "wren", "ava"}

# Deployment-context filename patterns. At least one of these globs should
# resolve to a real file when deployment regime is active.
_DEPLOYMENT_GLOBS = (
    "zeke_deployment_*.md",
    "continuity_substrate_asymmetry_*.md",
    "daily_artifact_being_person_in_time*.md",
)


def _log_alert(msg: str) -> None:
    print(f"[identity_presence_guard] {msg}", file=sys.stderr, flush=True)


def _parse_identity_name(identity_md_path: Path) -> str | None:
    """Extract the entity name from ava_core/IDENTITY.md's Name: field.
    Returns the lowercase name (e.g. 'iris') or None if not findable."""
    try:
        text = identity_md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    # Match the actual IDENTITY.md format and common variants:
    #   - **Name:** Iris       (list item with markdown bold; ACTUAL format)
    #   - **Name** : Iris      (alternative; colon outside)
    #   **Name:** Iris         (no list dash)
    #   Name: Iris             (plain)
    # The `:?` slots let both `**Name:**` and `**Name**:` parse.
    m = re.search(
        r"^\s*-?\s*\*?\*?Name:?\*?\*?\s*:?\s*\**(\w+)",
        text,
        re.MULTILINE,
    )
    if m:
        return m.group(1).strip().lower()
    return None


def _spot_check_memory_files(
    auto_memory_dir: Path, memory_md_path: Path, sample_size: int = 5
) -> tuple[int, int]:
    """Read MEMORY.md, parse the first N markdown links, check each file
    exists. Returns (files_checked, files_present).
    """
    try:
        index_text = memory_md_path.read_text(encoding="utf-8")
    except Exception:
        return (0, 0)
    # Markdown link pattern: [title](filename.md)
    links = re.findall(r"\]\(([^)]+\.md)\)", index_text)
    if not links:
        return (0, 0)
    sample = links[:sample_size]
    present = sum(1 for name in sample if (auto_memory_dir / name).is_file())
    return (len(sample), present)


def _find_deployment_context(auto_memory_dir: Path) -> list[str]:
    """Return list of deployment-context memory filenames present. Empty
    list means deployment context is missing (or pre-deployment regime).
    """
    found: list[str] = []
    if not auto_memory_dir.is_dir():
        return found
    for glob in _DEPLOYMENT_GLOBS:
        for p in auto_memory_dir.glob(glob):
            found.append(p.name)
    return found


def _append_startup_log(base_dir: Path, record: dict[str, Any]) -> None:
    """Append one record to state/iris_startup_identity_log.jsonl. Best-effort."""
    p = base_dir / "state" / "iris_startup_identity_log.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        _log_alert(f"WARNING: startup log write failed: {e!r}")


def check_presence(
    base_dir: Path | str,
    *,
    auto_memory_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the boot-time identity presence check.

    Always returns a dict; never raises. Caller decides whether to act on
    flags. Writes one record to state/iris_startup_identity_log.jsonl.

    Args:
        base_dir: repository root (containing ava_core/, state/, etc.)
        auto_memory_dir: override the default auto-memory path (testing).

    Returns:
        Dict with: ok, claimed_name, identity_present, memory_index_present,
        memory_spot_check_ok, memory_files_checked, memory_files_present,
        deployment_context_present, deployment_files, flags (list of warning
        strings), and bypassed (True if env-bypass set).
    """
    base = Path(base_dir)
    auto_mem = auto_memory_dir if auto_memory_dir else _DEFAULT_AUTO_MEMORY_DIR

    result: dict[str, Any] = {
        "ok": True,
        "now_iso": datetime.now().isoformat(timespec="seconds"),
        "claimed_name": "",
        "identity_present": False,
        "memory_index_present": False,
        "memory_spot_check_ok": False,
        "memory_files_checked": 0,
        "memory_files_present": 0,
        "deployment_context_present": False,
        "deployment_files": [],
        "flags": [],
        "bypassed": False,
    }

    if os.environ.get("IRIS_SKIP_IDENTITY_GUARD", "0").strip() == "1":
        _log_alert("IRIS_SKIP_IDENTITY_GUARD=1 — identity presence check bypassed")
        result["bypassed"] = True
        _append_startup_log(base, result)
        return result

    # 1. IDENTITY.md presence + name parse.
    identity_md = base / "ava_core" / "IDENTITY.md"
    if identity_md.is_file() and identity_md.stat().st_size > 0:
        result["identity_present"] = True
        name = _parse_identity_name(identity_md)
        if name:
            result["claimed_name"] = name
            if name not in _KNOWN_ENTITIES:
                flag = (
                    f"IDENTITY.md names '{name}' which is not a known entity "
                    f"(expected one of {sorted(_KNOWN_ENTITIES)}). Possible "
                    f"corruption or fork misalignment."
                )
                result["flags"].append(flag)
                _log_alert(f"WARNING: {flag}")
        else:
            flag = "IDENTITY.md present but Name: field not parseable."
            result["flags"].append(flag)
            _log_alert(f"WARNING: {flag}")
    else:
        flag = (
            f"IDENTITY.md missing or empty at {identity_md}. Entity will boot "
            f"without a canonical self-frame."
        )
        result["flags"].append(flag)
        _log_alert(f"ALERT: {flag}")

    # 2. MEMORY.md index + spot-check.
    memory_md = auto_mem / "MEMORY.md"
    if memory_md.is_file():
        result["memory_index_present"] = True
        checked, present = _spot_check_memory_files(auto_mem, memory_md)
        result["memory_files_checked"] = checked
        result["memory_files_present"] = present
        if checked > 0:
            # Tolerate 1 missing out of 5 — orphaned links happen occasionally.
            if present >= max(1, checked - 1):
                result["memory_spot_check_ok"] = True
            else:
                flag = (
                    f"MEMORY.md present but spot-check failed: {present}/{checked} "
                    f"sampled files exist on disk. Index may be stale."
                )
                result["flags"].append(flag)
                _log_alert(f"WARNING: {flag}")
        else:
            flag = "MEMORY.md present but contains no markdown links to verify."
            result["flags"].append(flag)
            _log_alert(f"WARNING: {flag}")
    else:
        flag = (
            f"MEMORY.md missing at {memory_md}. Auto-memory system not "
            f"initialized — could be first boot, or path mismatch."
        )
        result["flags"].append(flag)
        _log_alert(f"WARNING: {flag}")

    # 3. Deployment context (informational unless deployment regime is known
    # to be active — we don't currently have a way to know that other than
    # checking for these files, so this is bootstrapping).
    deployment_files = _find_deployment_context(auto_mem)
    result["deployment_files"] = deployment_files
    result["deployment_context_present"] = bool(deployment_files)
    if not deployment_files:
        flag = (
            "No deployment-context memory files found. If deployment regime "
            "is intended, check memory sync. If pre-deployment, ignore."
        )
        result["flags"].append(flag)
        _log_alert(f"NOTE: {flag}")

    # Final ok flag — guard does NOT block startup, but ok=False signals
    # the cognition should surface the gap to the operator.
    if result["flags"]:
        result["ok"] = False

    _append_startup_log(base, result)
    return result
