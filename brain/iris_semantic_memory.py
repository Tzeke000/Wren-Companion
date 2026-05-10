"""
brain/iris_semantic_memory.py — semantic search layer over iris_memory.

iris_memory.jsonl is the durable canonical log. This module mirrors entries
into a ChromaDB collection so I can do similarity search ("what did I learn
about Zeke's sleep schedule?") rather than just substring match.

Embeddings: Chroma's bundled all-MiniLM-L6-v2 ONNX (CPU). 80MB cached at
~/.cache/chroma/. No Ollama, no API keys, no extra pip installs.

Sync model: write-through. Every iris_memory.add() that includes a hook
into this module will also upsert into Chroma. On boot, we reconcile —
any JSONL entries missing from Chroma get backfilled.

Public API:
  configure(base_dir)              — bind paths, idempotent
  upsert(entry)                    — add one memory (called by hook)
  search(query, k=5) -> list[dict] — semantic search, returns iris_memory rows
  reconcile_from_jsonl()           — rebuild collection from JSONL on boot
  delete(memory_id)                — remove from collection (mirrors iris_memory.delete)
  count()                          — collection size

Why not mem0: mem0 wants either Ollama or OpenAI for both LLM extraction
AND embedder config. We don't have Ollama on this machine and we don't
want a paid API. Chroma's bundled embedder gives us semantic search on
day one with zero extra deps. The "extraction" half (deciding what to
remember) is me — through inner_monologue and explicit memory_remember
MCP calls.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional


_BASE: Path | None = None
_CLIENT: Any = None  # chromadb.PersistentClient
_COLLECTION: Any = None  # chromadb.Collection
_LOCK = threading.Lock()
_COLLECTION_NAME = "iris_memories"


def _chroma_dir() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "memory" / "chroma"


def configure(base_dir: Path | str) -> None:
    """Bind paths. Call once from iris_bootstrap. Idempotent."""
    global _BASE
    _BASE = Path(base_dir)
    _chroma_dir().mkdir(parents=True, exist_ok=True)


def _ensure_client() -> bool:
    """Lazy-init the chroma client + collection. Returns True if ready."""
    global _CLIENT, _COLLECTION
    if _COLLECTION is not None:
        return True
    if _BASE is None:
        return False
    try:
        import chromadb  # type: ignore
    except ImportError:
        print("[iris_semantic_memory] chromadb not installed — semantic search disabled")
        return False
    try:
        with _LOCK:
            if _COLLECTION is not None:
                return True
            _CLIENT = chromadb.PersistentClient(path=str(_chroma_dir()))
            _COLLECTION = _CLIENT.get_or_create_collection(_COLLECTION_NAME)
            return True
    except Exception as e:
        print(f"[iris_semantic_memory] init error: {e!r}")
        return False


def upsert(entry: dict[str, Any]) -> bool:
    """Add a memory entry to the semantic index. Idempotent — calling twice
    with the same id replaces."""
    if not _ensure_client():
        return False
    try:
        eid = str(entry.get("id") or "")
        text = str(entry.get("text") or "")
        if not eid or not text:
            return False
        meta = {
            "ts": float(entry.get("ts") or 0.0),
            "iso": str(entry.get("iso") or ""),
            "person_id": str(entry.get("person_id") or ""),
            "category": str(entry.get("category") or ""),
            "importance": float(entry.get("importance") or 0.0),
            "source": str(entry.get("source") or ""),
            "tags": ",".join(entry.get("tags") or []),
        }
        with _LOCK:
            _COLLECTION.upsert(
                ids=[eid],
                documents=[text],
                metadatas=[meta],
            )
        return True
    except Exception as e:
        print(f"[iris_semantic_memory] upsert error: {e!r}")
        return False


def search(query: str, k: int = 5,
           person_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Semantic similarity search. Returns iris_memory-shaped rows
    (id, text, ts, etc.) ordered by relevance."""
    if not _ensure_client():
        return []
    if not (query or "").strip():
        return []
    try:
        where = {"person_id": person_id} if person_id else None
        with _LOCK:
            res = _COLLECTION.query(
                query_texts=[str(query)],
                n_results=int(max(1, k)),
                where=where,
            )
        # Chroma returns dict of lists; we get the first row.
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[dict[str, Any]] = []
        for i in range(len(ids)):
            meta = metas[i] if i < len(metas) and metas[i] else {}
            tags_str = str(meta.get("tags") or "")
            tags = [t for t in tags_str.split(",") if t]
            out.append({
                "id": ids[i],
                "text": docs[i] if i < len(docs) else "",
                "ts": float(meta.get("ts") or 0.0),
                "iso": str(meta.get("iso") or ""),
                "person_id": str(meta.get("person_id") or ""),
                "category": str(meta.get("category") or ""),
                "importance": float(meta.get("importance") or 0.0),
                "source": str(meta.get("source") or ""),
                "tags": tags,
                "distance": float(dists[i]) if i < len(dists) else 0.0,
            })
        return out
    except Exception as e:
        print(f"[iris_semantic_memory] search error: {e!r}")
        return []


def delete(memory_id: str) -> bool:
    """Remove from collection. Returns True if anything was deleted."""
    if not _ensure_client():
        return False
    try:
        with _LOCK:
            _COLLECTION.delete(ids=[str(memory_id)])
        return True
    except Exception as e:
        print(f"[iris_semantic_memory] delete error: {e!r}")
        return False


def count() -> int:
    if not _ensure_client():
        return 0
    try:
        with _LOCK:
            return int(_COLLECTION.count())
    except Exception:
        return 0


def reconcile_from_jsonl() -> int:
    """Rebuild the collection from iris_memory.jsonl. Used at boot to
    catch any entries that landed before chroma was ready."""
    if not _ensure_client():
        return 0
    if _BASE is None:
        return 0
    jsonl = _BASE / "state" / "iris_memory.jsonl"
    if not jsonl.is_file():
        return 0
    try:
        import json
        # Pull existing IDs from chroma to avoid re-embedding everything.
        with _LOCK:
            existing = set()
            try:
                got = _COLLECTION.get(limit=10000)
                existing = set((got.get("ids") or []))
            except Exception:
                pass
        added = 0
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and entry.get("id") not in existing:
                    if upsert(entry):
                        added += 1
            except Exception:
                continue
        if added:
            print(f"[iris_semantic_memory] reconciled {added} entries from JSONL")
        return added
    except Exception as e:
        print(f"[iris_semantic_memory] reconcile error: {e!r}")
        return 0


def bootstrap_iris_semantic_memory(g: dict[str, Any]) -> None:
    """Wire into the global state dict. Idempotent."""
    base = Path(g.get("BASE_DIR") or ".")
    configure(base)
    if _ensure_client():
        n = reconcile_from_jsonl()
        g["_iris_semantic_memory_ready"] = True
        print(f"[iris_semantic_memory] ready (count={count()}, reconciled={n})")
    else:
        g["_iris_semantic_memory_ready"] = False
