"""brain/windows_use/event_subscriber.py — wrapper events into Ava.

Routes wrapper events into:
    - inner monologue ring (THOUGHT, ERROR)
    - audit log state/windows_use_log.jsonl (everything)
    - subscribers list at g["_windows_use_subscribers"]

Event shape:
    {"type": "TOOL_CALL"|"THOUGHT"|"TOOL_RESULT"|"ERROR",
     "ts": float, "operation": str, "payload": dict}
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


_LOG_LOCK = threading.Lock()
_AUDIT_FILENAME = "windows_use_log.jsonl"
_MAX_AUDIT_BYTES = 10 * 1024 * 1024  # 10 MB rotation


def _audit_path(g: dict[str, Any]) -> Path:
    base = Path(g.get("BASE_DIR") or ".")
    p = base / "state" / _AUDIT_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _rotate_if_huge(p: Path) -> None:
    try:
        if p.is_file() and p.stat().st_size > _MAX_AUDIT_BYTES:
            backup = p.with_suffix(".prev.jsonl")
            if backup.exists():
                backup.unlink()
            p.rename(backup)
    except Exception:
        pass


def _write_audit(g: dict[str, Any], event: dict[str, Any]) -> None:
    p = _audit_path(g)
    with _LOG_LOCK:
        _rotate_if_huge(p)
        try:
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[windows_use.event] audit write error: {e!r}")


def _surface_to_inner_monologue(g: dict[str, Any], event: dict[str, Any]) -> None:
    if event.get("type") not in ("THOUGHT", "ERROR"):
        return
    base = Path(g.get("BASE_DIR") or ".")
    payload = event.get("payload") or {}
    text = str(payload.get("thought") or payload.get("error") or "").strip()
    if not text:
        return
    try:
        from brain.inner_monologue import _append_thought
        mood = "concerned" if event.get("type") == "ERROR" else "focused"
        _append_thought(base, text, "windows_use", mood)
    except Exception as e:
        print(f"[windows_use.event] inner-monologue surface error: {e!r}")


def emit(g: dict[str, Any], event_type: str, operation: str, payload: dict[str, Any] | None = None) -> None:
    """Single emission point. Subscribers are called best-effort; one
    failing subscriber doesn't stop the others."""
    event = {
        "type": str(event_type),
        "ts": time.time(),
        "operation": str(operation),
        "payload": dict(payload or {}),
    }
    _write_audit(g, event)
    _surface_to_inner_monologue(g, event)
    subs = g.get("_windows_use_subscribers") or []
    if isinstance(subs, list):
        for cb in subs:
            try:
                cb(event)
            except Exception as e:
                print(f"[windows_use.event] subscriber error: {e!r}")


def add_subscriber(g: dict[str, Any], cb: Callable[[dict[str, Any]], None]) -> None:
    subs = g.get("_windows_use_subscribers")
    if not isinstance(subs, list):
        subs = []
        g["_windows_use_subscribers"] = subs
    subs.append(cb)
