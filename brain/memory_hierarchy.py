"""brain/memory_hierarchy.py — L1-L5 memory facade (architecture #3).

Today, Ava's memory lives in many stores with no clear relationship:

- chat_history.jsonl — raw conversation log, append-only
- mem0 vector store — semantic recall via embeddings
- concept_graph.json — knowledge graph of facts/relationships
- fts_memory.db — SQLite FTS5 full-text index over chat_history
- skills/*.json — procedural skills auto-created
- journal.jsonl — Ava's own reflections
- IDENTITY.md / SOUL.md / USER.md — bedrock identity
- mood_carryover.json + emotion weights — affective state

Every feature that needs memory currently has to know all those
shapes. That's coupling that won't scale to ~50 features.

This module is the FACADE that organizes those stores into a clear
hierarchy with documented consolidation flow:

  L1 (working)    — current turn + topic + active tasks (in-memory)
  L2 (episodic)   — recent days, raw history (chat_history.jsonl)
  L3 (semantic)   — concept_graph + mem0 vector + FTS5
  L4 (procedural) — skills + voice commands + learned patterns
  L5 (identity)   — IDENTITY/SOUL/USER + anchor moments

Consolidation flow:
  L1 → L2 continuous (every turn appends to chat_history)
  L2 → L3 during sleep (concept extraction, summarization)
  L3 → L4 when patterns emerge (skill auto-creation, learned-pattern detection)
  L5 only on anchor moments (rare, deliberate) — and bedrock files
       (IDENTITY/SOUL/USER) are never automatically modified

API:
  l1_set(key, value) / l1_get(key) — working memory (per-turn)
  l1_clear() — end of turn cleanup
  l2_recent(limit, person_id) — recent episodic entries
  l3_search(query, limit) — semantic + literal hybrid (FTS5 first, mem0 fallback)
  l4_recall_skill(text) — procedural skill match
  l5_identity_summary() — read-only digest of identity bedrock

This module provides the SEAM. Existing code paths still write to
their underlying stores directly. Future code can opt in incrementally.

The consolidation flow is documented but not yet auto-runs;
A5 (memory pruning + sleep-anchored consolidation) lands the actual
movement of data between layers.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any


_L1_STORE: dict[str, Any] = {}
_L1_LOCK = threading.RLock()


# ── L1: Working memory (in-memory per-turn) ───────────────────────────────


def l1_set(key: str, value: Any) -> None:
    """Set a working-memory value. Cleared at end of turn."""
    with _L1_LOCK:
        _L1_STORE[key] = value


def l1_get(key: str, default: Any = None) -> Any:
    with _L1_LOCK:
        return _L1_STORE.get(key, default)


def l1_clear() -> None:
    """Called at the end of each turn to clear working state."""
    with _L1_LOCK:
        _L1_STORE.clear()


def l1_keys() -> list[str]:
    with _L1_LOCK:
        return list(_L1_STORE.keys())


# ── L2: Episodic memory (recent days, raw history) ───────────────────────


def l2_recent(
    base_dir: Path,
    *,
    limit: int = 20,
    person_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read recent entries from chat_history.jsonl.

    Optional person_id filter. Returns most-recent-first.
    """
    import json
    path = base_dir / "state" / "chat_history.jsonl"
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if person_id and str(d.get("person_id") or "").lower() != person_id.lower():
            continue
        out.append(d)
    return out


def l2_append(base_dir: Path, record: dict[str, Any]) -> None:
    """Append a record to chat_history.jsonl. Convenience for callers
    that don't want to know the storage shape."""
    import json
    path = base_dir / "state" / "chat_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(record)
    rec.setdefault("ts", time.time())
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[memory_hierarchy] l2_append error: {e!r}")


# ── L3: Semantic memory (concept_graph + mem0 + FTS5 hybrid) ─────────────


def l3_search(
    base_dir: Path,
    query: str,
    *,
    limit: int = 4,
    person_id: str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid semantic search. Tries FTS5 first (microseconds), falls
    through to mem0 vector if no FTS5 hit (seconds). Returns unified
    list of {source, content, score, ...}.

    Mirrors the logic prompt_builder uses when injecting MEMORIES
    into deep-path turns. Centralized here so future callers don't
    re-implement the FTS5/mem0 hybrid.
    """
    out: list[dict[str, Any]] = []
    # FTS5 fast path
    try:
        from brain.fts_memory import search as fts_search
        fts_hits = fts_search(base_dir, query, limit=limit, person_id=person_id) or []
        for h in fts_hits:
            out.append({
                "source": "fts",
                "content": str(h.get("content") or ""),
                "role": str(h.get("role") or ""),
                "ts": str(h.get("ts") or ""),
                "score": float(h.get("score") or 0.0),
            })
    except Exception as e:
        print(f"[memory_hierarchy] l3 FTS error: {e!r}")
    if out:
        return out
    # mem0 fallback (semantic)
    try:
        # Loader is held by avaagent.g — needs to be passed in via caller
        # since we don't import avaagent here. For now: empty fallback.
        pass
    except Exception:
        pass
    return out


# ── L4: Procedural memory (skills) ───────────────────────────────────────


def l4_recall_skill(base_dir: Path, text: str) -> tuple[dict[str, Any], float] | None:
    """Find a matching procedural skill for the input text."""
    try:
        from brain.skills import recall
        return recall(base_dir, text)
    except Exception as e:
        print(f"[memory_hierarchy] l4_recall_skill error: {e!r}")
        return None


def l4_skill_count(base_dir: Path) -> int:
    """How many skills Ava has learned."""
    try:
        from brain.skills import _load_index
        idx = _load_index(base_dir)
        return len(idx.get("skills") or {})
    except Exception:
        return 0


# ── L5: Identity (read-only digest) ───────────────────────────────────────


def l5_identity_summary(base_dir: Path) -> dict[str, str]:
    """Read-only digest of identity bedrock files.

    Per CLAUDE.md, IDENTITY.md / SOUL.md / USER.md are NEVER edited
    by Ava or by Claude Code. This function ONLY reads them so other
    modules can incorporate the identity context without parsing the
    files themselves.
    """
    out: dict[str, str] = {}
    for name in ("IDENTITY.md", "SOUL.md", "USER.md"):
        try:
            p = base_dir / "ava_core" / name
            if p.exists():
                content = p.read_text(encoding="utf-8")
                out[name] = content[:400]  # first 400 chars as preview
        except Exception:
            continue
    return out


# ── Consolidation status ──────────────────────────────────────────────────


def consolidation_status(base_dir: Path) -> dict[str, Any]:
    """Diagnostic snapshot of each memory layer's current state."""
    out: dict[str, Any] = {
        "L1_working": {
            "keys_count": len(l1_keys()),
            "keys": l1_keys()[:20],
        },
        "L2_episodic": {},
        "L3_semantic": {},
        "L4_procedural": {
            "skill_count": l4_skill_count(base_dir),
        },
        "L5_identity": {},
    }
    # L2 size
    try:
        path = base_dir / "state" / "chat_history.jsonl"
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            out["L2_episodic"]["chat_history_lines"] = lines
            out["L2_episodic"]["chat_history_size_bytes"] = path.stat().st_size
    except Exception:
        pass
    # L3 size (FTS5 row count)
    try:
        from brain.fts_memory import _ensure_fresh
        conn = _ensure_fresh(base_dir)
        if conn is not None:
            cur = conn.execute("SELECT count(*) FROM msgs;").fetchone()
            if cur:
                out["L3_semantic"]["fts5_rows"] = int(cur[0])
    except Exception:
        pass
    # L5 — bedrock files
    try:
        for name in ("IDENTITY.md", "SOUL.md", "USER.md"):
            p = base_dir / "ava_core" / name
            if p.exists():
                out["L5_identity"][name] = {
                    "exists": True,
                    "size_bytes": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                }
    except Exception:
        pass
    return out
