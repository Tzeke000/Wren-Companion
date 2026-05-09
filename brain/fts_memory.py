"""brain/fts_memory.py — SQLite FTS5 fast-path memory.

Both Hermes and Jarvis use FTS5 for cross-session literal recall before
falling back to vector / semantic search. Ava already has mem0 (vector)
and concept_graph (knowledge graph), but mem0.search is 5-20s on warm
data — too slow for the build_prompt path. FTS5 is microseconds.

Index source: state/chat_history.jsonl (one JSON per line with
{role, content, ts, person_id, source, ...}).

Storage: state/fts_memory.db (single SQLite file with FTS5 virtual table).
Rebuilt automatically when chat_history.jsonl mtime > db mtime.

Query API: search(user_input, limit=4) -> list of {role, content, ts}
matching the input's important tokens. We strip stopwords + punctuation
and pass the remaining tokens to FTS5 with OR semantics so any matching
recent message returns. Bm25 ranks them.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DB_CONN: sqlite3.Connection | None = None
_DB_PATH: Path | None = None
_LAST_HISTORY_MTIME: float = 0.0

_STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "in", "on", "and", "or", "i",
    "you", "me", "we", "us", "they", "them", "is", "are", "be", "been",
    "have", "has", "had", "do", "does", "did", "this", "that", "these",
    "those", "what", "where", "when", "who", "how", "why", "tell", "ava",
}


def _strip_query(text: str) -> str:
    """Reduce query to FTS5-safe alphanumeric tokens with OR connectives."""
    if not text:
        return ""
    tokens = re.findall(r"[A-Za-z0-9]+", text.lower())
    keep = [t for t in tokens if t not in _STOPWORDS and len(t) >= 3]
    if not keep:
        return ""
    # Quote individual tokens to keep FTS5 happy with reserved chars.
    return " OR ".join(f'"{t}"' for t in keep[:8])


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS msgs USING fts5("
        "role, content, ts UNINDEXED, person_id UNINDEXED, source UNINDEXED, "
        "tokenize='unicode61'"
        ");"
    )
    return conn


def _rebuild_index(history_path: Path, conn: sqlite3.Connection) -> int:
    """Wipe + rebuild the FTS5 table from chat_history.jsonl. Returns row count."""
    conn.execute("DELETE FROM msgs;")
    rows = 0
    if not history_path.exists():
        conn.commit()
        return 0
    with history_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            content = str(rec.get("content") or "").strip()
            if not content:
                continue
            conn.execute(
                "INSERT INTO msgs (role, content, ts, person_id, source) VALUES (?, ?, ?, ?, ?);",
                (
                    str(rec.get("role") or ""),
                    content,
                    str(rec.get("ts") or ""),
                    str(rec.get("person_id") or ""),
                    str(rec.get("source") or ""),
                ),
            )
            rows += 1
    conn.commit()
    return rows


def _ensure_fresh(base_dir: Path) -> sqlite3.Connection | None:
    """Open the FTS5 db; rebuild it if chat_history.jsonl is newer than it."""
    global _DB_CONN, _DB_PATH, _LAST_HISTORY_MTIME
    history_path = base_dir / "state" / "chat_history.jsonl"
    db_path = base_dir / "state" / "fts_memory.db"
    with _LOCK:
        if _DB_CONN is None or _DB_PATH != db_path:
            try:
                _DB_CONN = _open_db(db_path)
                _DB_PATH = db_path
            except Exception as e:
                print(f"[fts_memory] open error: {e!r}")
                return None
        try:
            history_mtime = history_path.stat().st_mtime if history_path.exists() else 0.0
        except Exception:
            history_mtime = 0.0
        # Rebuild if first-time OR the chat history advanced past our last index.
        try:
            cur = _DB_CONN.execute("SELECT count(*) FROM msgs;").fetchone()
            current_rows = int(cur[0]) if cur else 0
        except Exception:
            current_rows = 0
        if current_rows == 0 or history_mtime > _LAST_HISTORY_MTIME:
            try:
                rebuilt = _rebuild_index(history_path, _DB_CONN)
                _LAST_HISTORY_MTIME = history_mtime
                print(f"[fts_memory] index rebuilt rows={rebuilt}")
            except Exception as e:
                print(f"[fts_memory] rebuild error: {e!r}")
                return _DB_CONN
        return _DB_CONN


def search(
    base_dir: Path,
    user_input: str,
    *,
    limit: int = 4,
    person_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return top-N FTS5-matched messages for the user_input.

    Each result: {role, content, ts, person_id, source, score}.
    """
    q = _strip_query(user_input)
    if not q:
        return []
    conn = _ensure_fresh(base_dir)
    if conn is None:
        return []
    try:
        sql = (
            "SELECT role, content, ts, person_id, source, bm25(msgs) AS score "
            "FROM msgs WHERE msgs MATCH ? "
        )
        params: list[Any] = [q]
        if person_id:
            sql += "AND person_id = ? "
            params.append(person_id)
        sql += "ORDER BY score LIMIT ?;"
        params.append(int(limit))
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [
            {
                "role": r[0],
                "content": r[1],
                "ts": r[2],
                "person_id": r[3],
                "source": r[4],
                "score": float(r[5] or 0.0),
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[fts_memory] search error: {e!r}")
        return []


def append(base_dir: Path, role: str, content: str, **meta: Any) -> None:
    """Append a single record to the FTS5 index without a full rebuild.

    Caller is responsible for matching the content to chat_history.jsonl.
    Useful for keeping the index in sync without a full reindex on every
    new turn.
    """
    if not content:
        return
    conn = _ensure_fresh(base_dir)
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO msgs (role, content, ts, person_id, source) VALUES (?, ?, ?, ?, ?);",
            (
                str(role or ""),
                str(content),
                str(meta.get("ts") or ""),
                str(meta.get("person_id") or ""),
                str(meta.get("source") or ""),
            ),
        )
        conn.commit()
    except Exception as e:
        print(f"[fts_memory] append error: {e!r}")
