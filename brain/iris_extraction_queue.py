"""
brain/iris_extraction_queue.py — deferred fact-extraction queue.

When Zeke says something that might contain durable facts/preferences,
we don't want to interrupt the reply path with an LLM call. We also
don't want to burn a CC turn for every voice/chat turn. The middle
ground:

  1. After each user turn, append a low-priority "consider extracting
     from this turn" entry to state/iris_extraction_queue.jsonl.
  2. inner_monologue's tick (~15min) drains the queue: for each pending
     turn, call iris_llm.extract_facts or extract_preferences, write
     results to iris_memory.

This way:
  - Per-turn cost: append to JSONL (cheap).
  - Per-tick cost: at most one LLM call to extract from accumulated turns.
  - No recursion (the inner_monologue tick already opens a CC turn for
    the thought; piggybacking on it adds zero turns).

State: state/iris_extraction_queue.jsonl
  {"id":..., "ts":..., "turn_text":..., "person_id":..., "status":"pending"}
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional


_LOCK = threading.Lock()
_BASE: Path | None = None


def configure(base_dir: Path | str) -> None:
    global _BASE
    _BASE = Path(base_dir)
    (_BASE / "state").mkdir(parents=True, exist_ok=True)


def _path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "iris_extraction_queue.jsonl"


def enqueue(turn_text: str, person_id: str = "zeke",
            modality: str = "chat") -> str:
    """Append a pending turn for later fact extraction. Cheap."""
    if not turn_text or not turn_text.strip():
        return ""
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "turn_text": str(turn_text)[:2000],
        "person_id": str(person_id),
        "modality": str(modality),
        "status": "pending",
    }
    with _LOCK:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry["id"]


def _read_all() -> list[dict[str, Any]]:
    p = _path()
    if not p.is_file():
        return []
    with _LOCK:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if isinstance(e, dict):
                out.append(e)
        except Exception:
            pass
    return out


def _rewrite_all(entries: list[dict[str, Any]]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    tmp.replace(p)


def pending_count() -> int:
    return sum(1 for e in _read_all() if e.get("status") == "pending")


def drain_one_batch(g: dict[str, Any], max_turns: int = 8) -> dict[str, Any]:
    """Process up to max_turns pending entries with a SINGLE iris_llm call.
    Combines turn texts so one extraction covers a batch.

    Returns dict with counts:
      processed: int
      facts_extracted: int
      remaining: int
    """
    entries = _read_all()
    pending = [e for e in entries if e.get("status") == "pending"]
    if not pending:
        return {"processed": 0, "facts_extracted": 0, "remaining": 0}
    batch = pending[:max_turns]
    combined = "\n\n".join(
        f"[{e.get('modality','?')}] {e.get('person_id','?')}: {e.get('turn_text','')}"
        for e in batch
    )
    facts: list[str] = []
    try:
        from brain import iris_llm
        result = iris_llm.extract_facts(combined)
        if result:
            facts = [f for f in result if f and len(f.strip()) > 5]
    except Exception as e:
        print(f"[extraction_queue] iris_llm error: {e!r}")
    # Persist facts to iris_memory if any.
    saved = 0
    if facts:
        try:
            mem = g.get("_iris_memory")
            if mem is not None:
                for fact in facts[:20]:  # cap per batch to avoid spam
                    try:
                        mem.add(fact, source="iris_llm_extraction",
                                category="fact", importance=0.6,
                                tags=["extracted"])
                        saved += 1
                    except Exception:
                        pass
        except Exception:
            pass
    # Mark batch as processed.
    with _LOCK:
        batch_ids = {e["id"] for e in batch}
        for e in entries:
            if e.get("id") in batch_ids:
                e["status"] = "processed"
                e["processed_ts"] = time.time()
                e["facts_extracted"] = saved
        _rewrite_all(entries)
    remaining = sum(1 for e in entries if e.get("status") == "pending")
    return {"processed": len(batch), "facts_extracted": saved, "remaining": remaining}


def bootstrap_iris_extraction_queue(g: dict[str, Any]) -> None:
    """Bind paths. Idempotent."""
    base = Path(g.get("BASE_DIR") or ".")
    configure(base)
    g["_iris_extraction_queue_ready"] = True
    print(f"[iris_extraction_queue] ready (pending={pending_count()})")
