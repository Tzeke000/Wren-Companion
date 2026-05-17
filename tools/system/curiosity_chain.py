# SELF_ASSESSMENT: I run a recursive-why curiosity chain on a topic and return the Q+A sequence, OR read a previously-saved chain by id.
"""
Recursive-why curiosity tool — Tier 2 (mutating: writes to concept_graph,
journal, and state/curiosity_chains/).

Zeke's framing 2026-05-17: ask "why" of the answer, take that answer, ask
"why" of it, recurse to depth-N or grounding hit. Each level's Q+A stored
as connected concept_graph subgraph plus a chain artifact at
state/curiosity_chains/<chain_id>.json.

Tool surface (one entrypoint, two modes):
- mode="pursue" topic="..."  -> runs a fresh chain on the topic (must
  not be mid-conversation; the underlying engine refuses to fire then,
  same gate as pursue_curiosity).
- mode="read" chain_id="..."  -> reads a saved chain back, returns the
  Q+A sequence.
- mode="list"                 -> lists recent chains with chain_id,
  topic, depth_reached, terminated_by.

Defensive-fallback: never raises. Reports failure shape in the response.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool


def _chains_dir(g: dict[str, Any]) -> Path:
    base = Path(g.get("BASE_DIR") or Path.cwd())
    p = base / "state" / "curiosity_chains"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _run_pursue(topic: str, max_depth: int, g: dict[str, Any]) -> dict[str, Any]:
    """Run a recursive-why chain. Topic is the seed string.

    Force-reloads brain.curiosity_topics so iterative edits to the recursive
    engine land without restart_self. The running iris_runtime caches brain/*
    modules in sys.modules at first import; importlib.reload() picks up
    on-disk changes mid-session, same spirit as the tools/* hot-reload
    registry.
    """
    try:
        import importlib
        import sys
        if "brain.curiosity_topics" in sys.modules:
            importlib.reload(sys.modules["brain.curiosity_topics"])
        from brain.curiosity_topics import (
            add_topic,
            get_current_curiosity,
            pursue_curiosity_recursive,
        )
    except Exception as e:
        return {"ok": False, "error": f"brain.curiosity_topics not importable: {e!r}"}

    # Seed: if topic provided, add it (idempotent via _topic_similarity)
    # and pursue it directly. If not, pursue the current top topic.
    topic = (topic or "").strip()
    if topic:
        try:
            add_topic(topic, sparked_by="curiosity_chain_tool", g=g)
        except Exception:
            pass
        topic_row = {"topic": topic}
    else:
        topic_row = get_current_curiosity(g) or {}
        if not topic_row.get("topic"):
            return {"ok": False, "error": "no topic provided and no current topic in state"}

    try:
        chain = pursue_curiosity_recursive(topic_row, g, max_depth=max_depth)
    except Exception as e:
        return {"ok": False, "error": f"pursue_curiosity_recursive raised: {e!r}"}

    return {
        "ok": True,
        "mode": "pursue",
        "chain_id": chain.get("chain_id", ""),
        "topic": chain.get("topic", ""),
        "depth_reached": chain.get("depth_reached", 0),
        "terminated_by": chain.get("terminated_by", ""),
        "chain": chain.get("chain", []),
        "started_ts": chain.get("started_ts", 0.0),
        "finished_ts": chain.get("finished_ts", 0.0),
    }


def _read_chain(chain_id: str, g: dict[str, Any]) -> dict[str, Any]:
    """Read a saved chain by id."""
    cid = (chain_id or "").strip()
    if not cid:
        return {"ok": False, "error": "chain_id required for mode='read'"}
    path = _chains_dir(g) / f"{cid}.json"
    if not path.is_file():
        return {"ok": False, "error": f"chain not found: {cid}"}
    try:
        chain = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"chain unreadable: {e!r}"}
    return {"ok": True, "mode": "read", **chain}


def _list_chains(limit: int, g: dict[str, Any]) -> dict[str, Any]:
    """List recent chains by mtime."""
    d = _chains_dir(g)
    try:
        files = sorted(
            d.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:max(1, int(limit))]
    except Exception as e:
        return {"ok": False, "error": f"list failed: {e!r}"}
    out = []
    for f in files:
        try:
            row = json.loads(f.read_text(encoding="utf-8"))
            out.append({
                "chain_id": row.get("chain_id", f.stem),
                "topic": row.get("topic", ""),
                "depth_reached": row.get("depth_reached", 0),
                "terminated_by": row.get("terminated_by", ""),
                "started_ts": row.get("started_ts", 0.0),
                "finished_ts": row.get("finished_ts", 0.0),
            })
        except Exception:
            continue
    return {"ok": True, "mode": "list", "count": len(out), "chains": out}


def _handle(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Single entrypoint — dispatch on mode.

    Params:
        mode: "pursue" (default) | "read" | "list"
        topic: str (mode=pursue)
        chain_id: str (mode=read)
        max_depth: int (mode=pursue, default 5)
        limit: int (mode=list, default 10)
    """
    t0 = time.time()
    mode = str(params.get("mode") or "pursue").strip().lower()
    try:
        if mode == "pursue":
            topic = str(params.get("topic") or "").strip()
            max_depth = int(params.get("max_depth") or 5)
            res = _run_pursue(topic, max_depth, g)
        elif mode == "read":
            res = _read_chain(str(params.get("chain_id") or ""), g)
        elif mode == "list":
            res = _list_chains(int(params.get("limit") or 10), g)
        else:
            res = {"ok": False, "error": f"unknown mode: {mode!r} (use pursue|read|list)"}
    except Exception as e:
        res = {"ok": False, "error": f"handler exception: {e!r}"}
    res["duration_ms"] = int((time.time() - t0) * 1000)
    return res


register_tool(
    name="curiosity_chain",
    description=(
        "Recursive-why curiosity engine. Takes a seed topic, asks 'why' of "
        "the answer, then 'why' of THAT answer, recursing to depth (default 5) "
        "or until grounding (physics axiom, math primitive, honest 'I don't "
        "know'). Stores chain as concept_graph subgraph + journal entry + "
        "artifact at state/curiosity_chains/<id>.json. Modes: 'pursue' (run "
        "a new chain — params: topic, max_depth=5), 'read' (params: chain_id), "
        "'list' (params: limit=10). Refuses to run during active conversation "
        "(same gate as pursue_curiosity)."
    ),
    tier=2,
    handler=_handle,
)
