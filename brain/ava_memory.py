"""
brain/ava_memory.py — mem0 wrapper for Ava.

Uses mem0ai 2.0+ with:
  - LLM:        Ollama (`ava-personal:latest` — Llama 3.1 8B Q4_K_M, fine-tuned)
  - Embedder:   Ollama (`nomic-embed-text:latest`)
  - Vector DB:  ChromaDB at `memory/mem0_chroma/`

ChromaDB is chosen deliberately: Qdrant pulls in gRPC + protobuf 6.x,
which conflicts with the protobuf 3.20.x pin MediaPipe requires. ChromaDB
has no protobuf path.

The wrapper exposes a small, stable surface:
  add_conversation_turn(user_text, ava_text, user_id="zeke")
  search(query, user_id="zeke", limit=5)
  get_all(user_id="zeke", limit=200)
  delete(memory_id)
  delete_all_for(user_id="zeke")

mem0 internally extracts memories from each conversation via the LLM —
"Zeke likes red" / "Zeke is building Ava" / etc. We don't try to do
extraction ourselves.

Bootstrap-friendly: this module records facts but doesn't decide what
matters. mem0's extraction prompt does the filtering.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Optional


_DEFAULT_USER_ID = "zeke"
_DEFAULT_COLLECTION = "ava_memories"

# Pre-create the chroma directory at module load so mem0's first init call
# never trips on a missing path. Safe to call repeatedly.
try:
    os.makedirs("D:/AvaAgentv2/memory/mem0_chroma", exist_ok=True)
except Exception:
    pass


class AvaMemory:
    """Thin singleton wrapper around mem0.Memory."""

    def __init__(self, base_dir: Path) -> None:
        self._base = Path(base_dir)
        self._lock = threading.Lock()
        self._memory: Any = None
        self._available = False
        self._init_error: str = ""

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Lazy-init the mem0 stack. Returns True if ready."""
        if self._available:
            return True
        # If init already failed once, don't retry — we'd just print the same
        # error again and clutter the log. Caller should treat available=False
        # as terminal for this process.
        if self._init_error:
            return False
        try:
            from mem0 import Memory  # type: ignore
        except Exception as e:
            self._init_error = f"import: {e!r}"
            print(f"[ava_memory] disabled (init failed: import: {e!r}) — falling back to ChromaDB only")
            return False

        # ava-personal:latest is the cleanest fit (4.9 GB Q4_K_M).
        # Prefer it over the stock llama3.1:8b base for persona consistency
        # in extracted facts.
        llm_model = self._pick_llm_model()
        chroma_path = self._base / "memory" / "mem0_chroma"
        chroma_path.mkdir(parents=True, exist_ok=True)

        config = {
            "llm": {
                "provider": "ollama",
                "config": {
                    "model": llm_model,
                    "ollama_base_url": "http://localhost:11434",
                    "temperature": 0.1,
                },
            },
            "embedder": {
                "provider": "ollama",
                "config": {
                    "model": "nomic-embed-text:latest",
                    "ollama_base_url": "http://localhost:11434",
                },
            },
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "collection_name": _DEFAULT_COLLECTION,
                    "path": str(chroma_path),
                },
            },
            "version": "v1.1",
        }
        try:
            self._memory = Memory.from_config(config)
            self._available = True
            print(f"[ava_memory] mem0 ready (llm={llm_model}, chroma={chroma_path})")
            return True
        except Exception as e:
            self._init_error = f"init: {e!r}"
            print(f"[ava_memory] disabled (init failed: {e!r}) — falling back to ChromaDB only")
            self._available = False
            return False

    @staticmethod
    def _pick_llm_model() -> str:
        # ava-gemma4 dropped 2026-05-02 — 9.6 GB doesn't fit in the 8 GB
        # VRAM ceiling and forces Ollama paging on every mem0 extraction
        # call. ava-personal (4.9 GB Llama 3.1 8B Q4) is the cleanest fit.
        try:
            import requests
            r = requests.get("http://localhost:11434/api/tags", timeout=2)
            names = [m.get("name", "") for m in (r.json().get("models") or [])]
            for candidate in ("ava-personal:latest", "ava-personal", "llama3.1:8b"):
                if candidate in names:
                    return candidate
        except Exception:
            pass
        return "ava-personal:latest"

    @property
    def available(self) -> bool:
        return self._available

    # ── add ────────────────────────────────────────────────────────────────────

    def add_conversation_turn(
        self,
        user_text: str,
        ava_text: str,
        user_id: str = _DEFAULT_USER_ID,
    ) -> Optional[dict[str, Any]]:
        """Pass a single turn into mem0 for fact extraction.

        Combines the user input + Ava's reply so the extractor can pick up
        facts mentioned by either side ("I like X", Ava confirms X).
        """
        if not self._available:
            return None
        user_text = (user_text or "").strip()
        ava_text = (ava_text or "").strip()
        if not user_text and not ava_text:
            return None
        # Format as a short transcript snippet for mem0's extractor.
        transcript = ""
        if user_text:
            transcript += f"User: {user_text}\n"
        if ava_text:
            transcript += f"Ava: {ava_text}"
        try:
            with self._lock:
                return self._memory.add(transcript, user_id=user_id)
        except Exception as e:
            print(f"[ava_memory] add error: {e!r}")
            return None

    def add_fact(self, text: str, user_id: str = _DEFAULT_USER_ID) -> Optional[dict[str, Any]]:
        """Manually add a single fact (used by the 'remember this' voice command)."""
        if not self._available or not text.strip():
            return None
        try:
            with self._lock:
                return self._memory.add(text.strip(), user_id=user_id)
        except Exception as e:
            print(f"[ava_memory] add_fact error: {e!r}")
            return None

    # ── search / read ─────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        user_id: str = _DEFAULT_USER_ID,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Semantic search over Ava's memories. Returns up to `limit` results."""
        if not self._available or not (query or "").strip():
            return []
        try:
            with self._lock:
                # mem0 2.0+ uses filters={'user_id': ...}
                res = self._memory.search(query.strip(), filters={"user_id": user_id}, limit=limit)
            items = res.get("results") if isinstance(res, dict) else res
            return list(items or [])
        except Exception as e:
            print(f"[ava_memory] search error: {e!r}")
            return []

    def get_all(self, user_id: str = _DEFAULT_USER_ID, limit: int = 200) -> list[dict[str, Any]]:
        if not self._available:
            return []
        try:
            with self._lock:
                res = self._memory.get_all(filters={"user_id": user_id}, limit=limit)
            items = res.get("results") if isinstance(res, dict) else res
            return list(items or [])
        except Exception as e:
            print(f"[ava_memory] get_all error: {e!r}")
            return []

    # ── delete ────────────────────────────────────────────────────────────────

    def delete(self, memory_id: str) -> bool:
        if not self._available or not memory_id:
            return False
        try:
            with self._lock:
                self._memory.delete(memory_id=memory_id)
            return True
        except Exception as e:
            print(f"[ava_memory] delete error: {e!r}")
            return False

    def delete_matching(self, query: str, user_id: str = _DEFAULT_USER_ID) -> int:
        """Search + delete all matching memories. Used by 'forget everything about X'."""
        results = self.search(query, user_id=user_id, limit=50)
        n = 0
        for r in results:
            mid = r.get("id")
            if mid and self.delete(mid):
                n += 1
        return n

    def delete_all_for(self, user_id: str = _DEFAULT_USER_ID) -> int:
        """Delete every memory for a user. Used by 'forget everything you know about me'."""
        if not self._available:
            return 0
        try:
            with self._lock:
                self._memory.delete_all(user_id=user_id)
            return 1
        except Exception as e:
            print(f"[ava_memory] delete_all error: {e!r}")
            return 0


# ── singleton + bootstrap ─────────────────────────────────────────────────────

_SINGLETON: Optional[AvaMemory] = None
_LOCK = threading.Lock()


def get_ava_memory() -> Optional[AvaMemory]:
    return _SINGLETON


def bootstrap_ava_memory(g: dict[str, Any]) -> Optional[AvaMemory]:
    """Init mem0 in a background thread (LLM warmup can take a few seconds)."""
    global _SINGLETON
    base = Path(g.get("BASE_DIR") or ".")
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = AvaMemory(base)
    g["_ava_memory"] = _SINGLETON

    def _bg_init():
        try:
            _SINGLETON.initialize()
        except Exception as e:
            print(f"[ava_memory] background init error: {e!r}")

    threading.Thread(target=_bg_init, daemon=True, name="ava-memory-init").start()
    return _SINGLETON
