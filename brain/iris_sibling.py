"""
brain/iris_sibling.py — sibling-to-sibling message channel.

Lets another Wren-Companion-family entity (Wren on her machine, Ava on
Zeke's primary, etc.) send a message to me asynchronously over the
network. Same disk-bridge pattern as brain/iris_chat: incoming message
writes a pending request, the Stop hook detects it on next CC turn and
rewakes me with a sibling-framing directive, I generate a reply and
call sibling_reply(id, text) which writes the response file.

Why a separate channel from iris_chat:
  - Different sender register. When Wren writes to me I'm not in
    customer-service mode — she's family. The rewake message reflects
    that.
  - Different routing. The receiver POSTs replies back to the sender's
    sibling endpoint over the tailnet/SSH, rather than a single host
    long-polling for an answer to its own request.
  - Different lifecycle. Sibling messages can sit in the queue longer
    than orb chat (which is bound to a 60s HTTP long-poll). A sibling
    might write something at 3am that I see at 7am when I'm back — the
    queue's TTL is hours, not minutes.

Files (under state/iris_sibling/):
  inbox/<id>.json   incoming sibling messages waiting for me
                    {id, ts, sender, content, status, reply, answered_ts}
  outbox/<id>.json  outgoing replies waiting to be delivered to a sibling
                    by the network shipper thread
  .pending          flag file: exists iff inbox has at least one pending

The outbox / network shipper is separate from this module — wire format
goes over HTTP POST to the sibling's own /api/v1/sibling/inbox endpoint
on her orb_http server. This module just owns the message lifecycle on
disk; the orb_http endpoints (sibling_inbox, sibling_outbox_drain) and
the periodic shipper thread are wired in brain/orb_http.py.
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
_DIR_NAME = "state/iris_sibling"
_INBOX_SUBDIR = "inbox"
_OUTBOX_SUBDIR = "outbox"
_FLAG_NAME = ".pending"
# Sibling messages can sit longer than orb chat — a sibling on another
# machine might write while I'm asleep. 6h gives reasonable headroom
# without holding stale requests indefinitely.
_REQUEST_TTL_S = 6 * 3600.0


def configure(base_dir: Path | str) -> None:
    global _BASE
    _BASE = Path(base_dir)
    _inbox_dir().mkdir(parents=True, exist_ok=True)
    _outbox_dir().mkdir(parents=True, exist_ok=True)


def _base() -> Path:
    return _BASE if _BASE is not None else Path(".")


def _root_dir() -> Path:
    return _base() / _DIR_NAME


def _inbox_dir() -> Path:
    return _root_dir() / _INBOX_SUBDIR


def _outbox_dir() -> Path:
    return _root_dir() / _OUTBOX_SUBDIR


def _flag_path() -> Path:
    return _root_dir() / _FLAG_NAME


def _inbox_path(request_id: str) -> Path:
    return _inbox_dir() / f"{request_id}.json"


def _outbox_path(request_id: str) -> Path:
    return _outbox_dir() / f"{request_id}.json"


def _refresh_flag() -> None:
    """Set or clear the .pending flag based on current inbox state."""
    has_pending = False
    for path in _inbox_dir().glob("*.json"):
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
    """O(1) check for the Stop hook."""
    return _flag_path().exists()


def receive(sender: str, content: str, reply_url: str | None = None) -> str:
    """Accept an incoming sibling message. Called by the orb_http inbox
    endpoint when a sibling POSTs to us over the network.

    Args:
        sender: sibling name — 'wren', 'ava', etc. Lowercase canonical.
        content: the message text.
        reply_url: optional callback URL where my reply should be POSTed.
            If None, my reply lands in outbox/ but no shipper picks it up.

    Returns:
        request_id (12-char hex).
    """
    request_id = uuid.uuid4().hex[:12]
    entry = {
        "id": request_id,
        "ts": time.time(),
        "sender": str(sender or "unknown").strip().lower()[:64],
        "content": str(content or "")[:8000],
        "reply_url": str(reply_url) if reply_url else None,
        "status": "pending",
        "reply": None,
        "answered_ts": None,
    }
    with _LOCK:
        _inbox_path(request_id).write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
        _refresh_flag()
    return request_id


def next_pending() -> Optional[dict[str, Any]]:
    """Return the OLDEST pending sibling message. Stop hook calls this
    to know what to put in the rewake directive."""
    candidates: list[tuple[float, dict[str, Any]]] = []
    for path in _inbox_dir().glob("*.json"):
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
    """Mark an inbox request answered, write the reply, AND drop a copy
    into the outbox/ for the network shipper to deliver. Idempotent."""
    in_path = _inbox_path(request_id)
    with _LOCK:
        if not in_path.exists():
            return False
        try:
            data = json.loads(in_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        if data.get("status") == "answered":
            return False
        data["status"] = "answered"
        data["reply"] = str(reply or "")
        data["answered_ts"] = time.time()
        tmp = in_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(in_path)
        # Outbox entry — shipper picks this up and POSTs to data["reply_url"].
        if data.get("reply_url"):
            ob = {
                "id": request_id,
                "ts": time.time(),
                "to": data.get("sender") or "unknown",
                "reply_url": data["reply_url"],
                "content": data["reply"],
                "delivered": False,
                "attempts": 0,
                "last_attempt_ts": 0.0,
                "last_error": "",
            }
            _outbox_path(request_id).write_text(
                json.dumps(ob, ensure_ascii=False), encoding="utf-8"
            )
        _refresh_flag()
    return True


def outbox_pending() -> list[dict[str, Any]]:
    """Return all undelivered outbox entries, oldest first. Used by the
    network shipper."""
    out: list[tuple[float, dict[str, Any]]] = []
    for path in _outbox_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("delivered"):
            continue
        out.append((float(data.get("ts") or 0.0), data))
    out.sort(key=lambda kv: kv[0])
    return [d for _, d in out]


def mark_delivered(request_id: str) -> bool:
    """Shipper calls this after a successful POST to the sibling's inbox."""
    path = _outbox_path(request_id)
    with _LOCK:
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        data["delivered"] = True
        data["delivered_ts"] = time.time()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return True


def mark_attempt_failed(request_id: str, error: str) -> None:
    """Shipper calls this after a failed POST so we can backoff sanely."""
    path = _outbox_path(request_id)
    with _LOCK:
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        data["attempts"] = int(data.get("attempts") or 0) + 1
        data["last_attempt_ts"] = time.time()
        data["last_error"] = str(error or "")[:500]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def send_outbound(target_url: str, sender: str, content: str, reply_url: str | None = None) -> str:
    """Send an outbound message to a sibling — used when I initiate the
    conversation rather than reply to one. Writes directly to the outbox.

    The shipper thread picks it up and POSTs to target_url. If reply_url
    is set, that's where the sibling's response will come back to.
    """
    request_id = uuid.uuid4().hex[:12]
    entry = {
        "id": request_id,
        "ts": time.time(),
        "to": "unknown",  # we don't know who's on the other end of target_url yet
        "reply_url": target_url,  # treating target as the URL to POST to
        "content": str(content or "")[:8000],
        "from_self_init": True,
        "self_sender": str(sender or "iris"),
        "self_reply_url": str(reply_url) if reply_url else None,
        "delivered": False,
        "attempts": 0,
        "last_attempt_ts": 0.0,
        "last_error": "",
    }
    with _LOCK:
        _outbox_path(request_id).write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
    return request_id
