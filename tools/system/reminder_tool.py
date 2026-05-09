# SELF_ASSESSMENT: I store and surface reminders Zeke asks me to set.
"""
Reminder tool.

Persists pending reminders to state/reminders.jsonl. The heartbeat tick
checks for due reminders every cycle and speaks them via the TTS worker.

Bootstrap-friendly: format and tone of how Ava delivers the reminder are
hers; this module only moves the data.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from tools.tool_registry import register_tool


_PATH = "state/reminders.jsonl"
_LOCK = threading.Lock()


def _path(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".") / _PATH


def _read_all(g: dict[str, Any]) -> list[dict[str, Any]]:
    p = _path(g)
    out: list[dict[str, Any]] = []
    if not p.is_file():
        return out
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _rewrite(g: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    p = _path(g)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[reminders] rewrite error: {e}")


def _append(g: dict[str, Any], row: dict[str, Any]) -> None:
    p = _path(g)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[reminders] append error: {e}")


# ── public API ────────────────────────────────────────────────────────────────

def set_reminder(text: str, minutes: float, g: dict[str, Any]) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty_text"}
    try:
        minutes = max(0.1, float(minutes))
    except Exception:
        return {"ok": False, "error": "bad_minutes"}
    row = {
        "id": f"r{int(time.time()*1000)}",
        "text": text,
        "due_ts": time.time() + minutes * 60.0,
        "spoken": False,
        "created_ts": time.time(),
        "minutes": minutes,
    }
    with _LOCK:
        _append(g, row)
    print(f"[reminders] set: {minutes:.1f}min — {text[:80]!r}")
    return {"ok": True, "id": row["id"], "due_in_minutes": minutes, "text": text}


def get_reminders(g: dict[str, Any]) -> list[dict[str, Any]]:
    """Return pending (unspoken, uncancelled) reminders."""
    with _LOCK:
        rows = _read_all(g)
    return [r for r in rows if not r.get("spoken") and not r.get("cancelled")]


def cancel_reminders(g: dict[str, Any]) -> int:
    """Cancel all pending reminders. Returns count cancelled."""
    with _LOCK:
        rows = _read_all(g)
        cancelled = 0
        for r in rows:
            if not r.get("spoken") and not r.get("cancelled"):
                r["cancelled"] = True
                cancelled += 1
        _rewrite(g, rows)
    print(f"[reminders] cancelled {cancelled}")
    return cancelled


def deliver_due_reminders(g: dict[str, Any]) -> int:
    """Called by heartbeat. Speaks any reminders whose due_ts has passed.
    Returns the number of reminders spoken this tick.

    Also fires SIGNAL_REMINDER_DUE (urgent) on the signal bus so any
    registered urgent handler runs in addition to the speak path. The
    speak here is the primary delivery — the signal exists for any other
    subsystem that wants to react (logging, UI flash, etc).
    """
    if not bool(g.get("tts_enabled", False)):
        return 0
    worker = g.get("_tts_worker")
    if worker is None or not getattr(worker, "available", False):
        return 0
    with _LOCK:
        rows = _read_all(g)
        now = time.time()
        due = [r for r in rows if not r.get("spoken") and not r.get("cancelled") and float(r.get("due_ts") or 0) <= now]
        if not due:
            return 0
        bus = g.get("_signal_bus")
        for r in due:
            try:
                spoken = f"Reminder: {r.get('text', '')}"
                worker.speak_with_emotion(spoken, emotion="curiosity", intensity=0.5, blocking=False)
                r["spoken"] = True
                r["spoken_at"] = time.time()
                print(f"[reminders] delivered: {r.get('text', '')[:80]!r}")
                if bus is not None:
                    try:
                        from brain.signal_bus import SIGNAL_REMINDER_DUE
                        bus.fire(
                            SIGNAL_REMINDER_DUE,
                            data={"text": str(r.get("text", "")), "id": r.get("id")},
                            priority="urgent",
                        )
                    except Exception:
                        pass
            except Exception as e:
                print(f"[reminders] deliver error: {e}")
        _rewrite(g, rows)
    return len(due)


# ── tool registry handlers ───────────────────────────────────────────────────

def _tool_set_reminder(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    text = str(params.get("text") or "")
    minutes = params.get("minutes")
    if minutes is None:
        return {"ok": False, "error": "minutes parameter required"}
    return set_reminder(text, float(minutes), g)


def _tool_list_reminders(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    pending = get_reminders(g)
    return {"ok": True, "pending": pending, "count": len(pending)}


def _tool_cancel_reminders(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    n = cancel_reminders(g)
    return {"ok": True, "cancelled": n}


register_tool(
    "set_reminder",
    "Set a reminder. Params: text (string), minutes (number). Ava will speak the reminder when due.",
    1,
    _tool_set_reminder,
)
register_tool(
    "list_reminders",
    "List all pending (undelivered) reminders.",
    1,
    _tool_list_reminders,
)
register_tool(
    "cancel_reminders",
    "Cancel all pending reminders.",
    1,
    _tool_cancel_reminders,
)
