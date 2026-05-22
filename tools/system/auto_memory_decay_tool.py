# SELF_ASSESSMENT: I expose the auto-memory earned-score machinery (decay + backref strengthening) as MCP-callable tools so I can audit my own .md corpus mid-session without shelling out or restarting iris_runtime.
"""
Hot-load wrapper around scripts/auto_memory_decay.py.

Exposes four MCP tools:
  - auto_memory_summary       (tier 1, read-only)
  - auto_memory_report        (tier 1, read-only)
  - auto_memory_candidates    (tier 1, read-only)
  - auto_memory_mark          (tier 2, mutates state/auto_memory_meta.json)

Wired via direct function import from scripts/auto_memory_decay.py
(no subprocess, no stdout parsing). Each handler returns a JSON-serializable
dict - the CLI's print() calls are bypassed entirely.

scripts/ is not a package (no __init__.py), so we add the repo root to
sys.path once at import time and import the module by name.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

# --- Import the script's functions directly ---
# scripts/ lacks __init__.py; add the scripts/ dir to sys.path so
# `import auto_memory_decay` resolves to scripts/auto_memory_decay.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Import after sys.path manipulation. Module name = file stem.
import auto_memory_decay as _amd  # noqa: E402


# --- Shared helpers ---

def _build_state() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build (corpus, backrefs, meta) - the three inputs every read-only mode needs."""
    corpus = _amd.build_corpus()
    backrefs = _amd.build_backref_graph(corpus)
    meta = _amd.load_meta()
    return corpus, backrefs, meta


# --- Summary mode (tier 1) ---

def _auto_memory_summary(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        corpus, backrefs, meta = _build_state()
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}

    rows = _amd.generate_report(corpus, backrefs, meta)
    total = len(rows)
    mean_score = (sum(r["score"] for r in rows) / total) if total else 0.0
    total_kb = sum(r["size_kb"] for r in rows)

    return {
        "ok": True,
        "memory_dir": str(_amd.MEMORY_DIR),
        "total_files": total,
        "total_size_kb": round(total_kb, 1),
        "mean_score": round(mean_score, 3),
        "highly_earned_count": sum(1 for r in rows if r["score"] > 0.70),
        "decay_candidate_count": sum(1 for r in rows if r["score"] < 0.15),
        "flag_counts": {
            "permanent": sum(1 for r in rows if r["flag"] == "permanent"),
            "high":      sum(1 for r in rows if r["flag"] == "high"),
            "default":   sum(1 for r in rows if r["flag"] == "default"),
            "low":       sum(1 for r in rows if r["flag"] == "low"),
        },
        "decay_tau_days": _amd.DECAY_TAU_DAYS,
        "candidate_threshold": 0.15,
    }


# --- Report mode (tier 1) ---

def _auto_memory_report(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        n = int(params.get("n") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "param 'n' must be an integer"}

    try:
        corpus, backrefs, meta = _build_state()
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}

    rows = _amd.generate_report(corpus, backrefs, meta)
    if n > 0:
        rows = rows[:n]

    return {
        "ok": True,
        "memory_dir": str(_amd.MEMORY_DIR),
        "limit": n,
        "row_count": len(rows),
        "rows": rows,
    }


# --- List-candidates mode (tier 1) ---

def _auto_memory_candidates(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        threshold = float(params.get("threshold", 0.15))
    except (TypeError, ValueError):
        return {"ok": False, "error": "param 'threshold' must be a number"}
    try:
        n = int(params.get("n", 20))
    except (TypeError, ValueError):
        return {"ok": False, "error": "param 'n' must be an integer"}

    try:
        corpus, backrefs, meta = _build_state()
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}

    rows = _amd.generate_report(corpus, backrefs, meta)
    candidates = [r for r in rows if r["score"] < threshold][:n]
    total_size = sum(r["size_kb"] for r in candidates)

    return {
        "ok": True,
        "memory_dir": str(_amd.MEMORY_DIR),
        "threshold": threshold,
        "limit": n,
        "candidate_count": len(candidates),
        "total_candidate_size_kb": round(total_size, 1),
        "candidates": candidates,
    }


# --- Mark mode (tier 2, mutates state/auto_memory_meta.json) ---

def _auto_memory_mark(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    stem = str(params.get("stem") or "").strip()
    flag = str(params.get("flag") or "").strip()

    if not stem:
        return {"ok": False, "error": "missing required param 'stem'"}
    if flag not in _amd.BASE_IMPORTANCE:
        return {
            "ok": False,
            "error": f"invalid flag '{flag}'",
            "valid_flags": list(_amd.BASE_IMPORTANCE.keys()),
        }

    # Strip .md suffix if user passed a filename.
    if stem.endswith(".md"):
        stem = stem[:-3]

    try:
        corpus = _amd.build_corpus()
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}

    # Resolve via slug-variants (kebab vs snake) just like the CLI does.
    if stem not in corpus:
        resolved = None
        for variant in _amd.slug_variants(stem):
            if variant in corpus:
                resolved = variant
                break
        if resolved is None:
            return {
                "ok": False,
                "error": f"file not found in corpus: {stem}",
                "memory_dir": str(_amd.MEMORY_DIR),
            }
        stem = resolved

    meta = _amd.load_meta()
    prior = meta.get(stem, {}).get("importance_flag")
    entry = meta.setdefault(stem, {})
    entry["importance_flag"] = flag
    entry["set_ts"] = time.time()
    entry["set_iso"] = datetime.now().isoformat(timespec="seconds")
    _amd.save_meta(meta)

    return {
        "ok": True,
        "stem": stem,
        "flag": flag,
        "prior_flag": prior,
        "meta_path": str(_amd.META_PATH),
        "message": f"Marked {stem} as {flag} (was {prior or 'unset'})",
    }


# --- Registration ---

register_tool(
    name="auto_memory_summary",
    description=(
        "Auto-memory corpus summary stats (total files, mean earned-score, "
        "highly-earned count, decay-candidate count, flag counts). Read-only. "
        "Wraps scripts/auto_memory_decay.py summary."
    ),
    tier=1,
    handler=_auto_memory_summary,
)

register_tool(
    name="auto_memory_report",
    description=(
        "All .md files in the auto-memory corpus, sorted ascending by earned-score. "
        "Each row: {stem, score, age_days, size_kb, backref_count, recent_backref_count, flag}. "
        "Params: {n: int=0} (0 = all). Read-only."
    ),
    tier=1,
    handler=_auto_memory_report,
)

register_tool(
    name="auto_memory_candidates",
    description=(
        "Files below the decay threshold - candidates for consolidation/pruning. "
        "Params: {threshold: float=0.15, n: int=20}. Read-only."
    ),
    tier=1,
    handler=_auto_memory_candidates,
)

register_tool(
    name="auto_memory_mark",
    description=(
        "Override importance flag for a file in state/auto_memory_meta.json. "
        "Params: {stem: str, flag: 'permanent'|'high'|'default'|'low'}. "
        "Mutates the shadow-meta file. Tier 2."
    ),
    tier=2,
    handler=_auto_memory_mark,
)
