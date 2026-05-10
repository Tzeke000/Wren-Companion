"""
brain/iris_chat.py — pending-chat-request store.

Cross-process bridge between the orb's POST /api/v1/chat (HTTP, blocking
long-poll) and Iris's CC session (MCP, async). The orb writes a pending
request to disk; the Stop hook detects it on the next CC turn and rewakes
the model with a handle-this-chat directive; Iris generates a reply and
calls chat_reply(id, text) which writes the response file; the orb's
long-poll picks it up.

Files:
  state/iris_chat/<id>.json   per-request, status=pending|answered|expired
  state/iris_chat/.pending    flag file: exists iff at least one pending
                              request. Cheap for the Stop hook to test.

Why a flag file *and* status fields: the Stop hook fires after every CC
turn — even when voice mode is off, even when no chat is pending. We
need an O(1) "is there work to do" test that doesn't require listing
the chat directory. The flag is updated whenever submit/mark_answered
run.
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
_DIR_NAME = "state/iris_chat"
_FLAG_NAME = ".pending"
_REQUEST_TTL_S = 600.0  # requests older than this are considered abandoned (10x HTTP timeout)


def configure(base_dir: Path | str) -> None:
    global _BASE
    _BASE = Path(base_dir)
    # Also configure brain/iris_paths so other modules see the same root.
    try:
        from brain.iris_paths import paths as _paths
        _paths.configure(_BASE)
    except Exception:
        pass
    _chat_dir().mkdir(parents=True, exist_ok=True)


def _chat_dir() -> Path:
    """Canonical chat dir. Prefer brain/iris_paths.paths.chat_dir if configured."""
    try:
        from brain.iris_paths import paths as _paths
        if _paths._root is not None:
            return _paths.chat_dir
    except Exception:
        pass
    base = _BASE if _BASE is not None else Path(".")
    return base / _DIR_NAME


def _flag_path() -> Path:
    try:
        from brain.iris_paths import paths as _paths
        if _paths._root is not None:
            return _paths.chat_pending_flag
    except Exception:
        pass
    return _chat_dir() / _FLAG_NAME


def _request_path(request_id: str) -> Path:
    return _chat_dir() / f"{request_id}.json"


def _refresh_flag() -> None:
    """Set or clear the .pending flag file based on current state on disk."""
    has_pending = False
    for path in _chat_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("status") == "pending":
                ts = float(data.get("ts") or 0.0)
                if (time.time() - ts) <= _REQUEST_TTL_S:
                    has_pending = True
                    break
        except Exception:
            continue
    flag = _flag_path()
    if has_pending and not flag.exists():
        flag.write_text("1", encoding="utf-8")
    elif (not has_pending) and flag.exists():
        try:
            flag.unlink()
        except Exception:
            pass


def has_pending() -> bool:
    """O(1) check used by Stop hook."""
    return _flag_path().exists()


def submit(user_text: str) -> str:
    """Write a new pending request, return request_id."""
    request_id = uuid.uuid4().hex[:12]
    entry = {
        "id": request_id,
        "ts": time.time(),
        "user_text": str(user_text or "")[:8000],
        "status": "pending",
        "reply": None,
        "answered_ts": None,
    }
    with _LOCK:
        _request_path(request_id).write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
        _refresh_flag()
    return request_id


def next_pending() -> Optional[dict[str, Any]]:
    """Return the OLDEST pending request (FIFO across all modalities)."""
    candidates: list[tuple[float, dict[str, Any]]] = []
    for path in _chat_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "pending":
            continue
        ts = float(data.get("ts") or 0.0)
        if (time.time() - ts) > _REQUEST_TTL_S:
            # mark as expired so we don't keep trying
            data["status"] = "expired"
            try:
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            continue
        candidates.append((ts, data))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0])
    return candidates[0][1]


def mark_answered(request_id: str, reply: str) -> bool:
    """Flip the request to answered and write the reply. Atomic via tmp+rename.

    Phase 41: existence check is now INSIDE the lock to prevent the race where
    two concurrent mark_answered calls on the same id both succeed.
    """
    path = _request_path(request_id)
    with _LOCK:
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        # Idempotency — if already answered, return False so caller knows.
        if data.get("status") == "answered":
            return False
        data["status"] = "answered"
        data["reply"] = str(reply or "")
        data["answered_ts"] = time.time()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        _refresh_flag()
    return True


def wait_for_reply(request_id: str, timeout_s: float = 60.0) -> Optional[str]:
    """Block up to timeout_s for the request to flip to answered. Returns
    the reply string on success, None on timeout or expired.

    Phase 41: also returns None immediately if the request status flips
    to expired (e.g. iris_runtime down, or stale-cleanup ran). Avoids
    the waiter blocking the full timeout when the answer will never come.
    """
    deadline = time.time() + max(0.5, float(timeout_s))
    path = _request_path(request_id)
    while time.time() < deadline:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    status = data.get("status")
                    if status == "answered":
                        return str(data.get("reply") or "")
                    if status == "expired":
                        return None
            except Exception:
                pass
        time.sleep(0.2)
    return None


def get(request_id: str) -> Optional[dict[str, Any]]:
    path = _request_path(request_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
