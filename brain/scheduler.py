"""brain/scheduler.py — Reminders + scheduled actions (Hermes-pattern).

Voice command "remind me to X in 5 minutes" creates a scheduled entry.
A background poll loop checks pending entries every 5s and fires them
through the existing TTS path when their `when_iso` time arrives.

Persisted at state/scheduled_tasks.json so reminders survive restart.
On startup, pending entries are reloaded and re-scheduled.

Schema (one entry):
{
  "id": "rmd-1777993200-3a2b",
  "when_iso": "2026-05-05T18:30:00",
  "what": "call the client",
  "source_user": "zeke",
  "created": 1777983200.0,
  "fired": false
}

Action: when fired, Ava verbally announces "{user}, you asked me to
remind you: {what}." via the TTS worker. The orb pulses to flag the
proactive interjection.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import uuid

_LOCK = threading.RLock()
_TICK_SECONDS = 5.0
_WATCHER_THREAD: threading.Thread | None = None
_WATCHER_STOP = threading.Event()


def _state_path(base_dir: Path) -> Path:
    p = base_dir / "state" / "scheduled_tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load(base_dir: Path) -> list[dict[str, Any]]:
    p = _state_path(base_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save(base_dir: Path, entries: list[dict[str, Any]]) -> None:
    p = _state_path(base_dir)
    p.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _new_id() -> str:
    return f"rmd-{int(time.time())}-{uuid.uuid4().hex[:6]}"


# ── Time parsing ──────────────────────────────────────────────────────────


_DURATION_RE = re.compile(
    r"in\s+(?P<n>\d+)\s+(?P<unit>second|seconds|sec|secs|"
    r"minute|minutes|min|mins|"
    r"hour|hours|hr|hrs|"
    r"day|days)",
    re.IGNORECASE,
)
_AT_TIME_RE = re.compile(
    r"\bat\s+(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>am|pm|AM|PM)?\b",
    re.IGNORECASE,
)
_TOMORROW_AT_RE = re.compile(
    r"tomorrow(?:\s+at\s+(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>am|pm)?)?",
    re.IGNORECASE,
)


def parse_when(text: str, *, now: datetime | None = None) -> datetime | None:
    """Return the absolute `when` datetime parsed from `text`, or None.

    Handles: "in N min/hour/day", "at HH:MM(am|pm)", "tomorrow at HH:MM".
    """
    now = now or datetime.now()

    # Tomorrow takes precedence (otherwise "tomorrow at 3pm" matches "at 3pm" first).
    m = _TOMORROW_AT_RE.search(text)
    if m:
        tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        h = m.group("h")
        if h is not None:
            hh = int(h)
            mm = int(m.group("m") or 0)
            ampm = (m.group("ampm") or "").lower()
            if ampm == "pm" and hh < 12:
                hh += 12
            elif ampm == "am" and hh == 12:
                hh = 0
            tomorrow = tomorrow.replace(hour=hh, minute=mm)
        return tomorrow

    m = _DURATION_RE.search(text)
    if m:
        n = int(m.group("n"))
        unit = m.group("unit").lower()
        if unit.startswith("sec"):
            return now + timedelta(seconds=n)
        if unit.startswith("min"):
            return now + timedelta(minutes=n)
        if unit.startswith("hr") or unit.startswith("hour"):
            return now + timedelta(hours=n)
        if unit.startswith("day"):
            return now + timedelta(days=n)

    m = _AT_TIME_RE.search(text)
    if m:
        hh = int(m.group("h"))
        mm = int(m.group("m") or 0)
        ampm = (m.group("ampm") or "").lower()
        if ampm == "pm" and hh < 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    return None


def extract_action(text: str) -> str:
    """Pull the 'what' out of "remind me to <X> [time clause]".

    Keeps it simple: strip trigger words, strip time-clause from the end.
    """
    t = text.strip()
    # Drop the trigger.
    t = re.sub(
        r"^(?:hey\s+ava[,\s]+)?(?:please\s+)?remind\s+me\s+(?:to\s+)?",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # Strip trailing time clause(s).
    t = _DURATION_RE.sub("", t)
    t = _AT_TIME_RE.sub("", t)
    t = _TOMORROW_AT_RE.sub("", t)
    return t.strip(" ,.;:?!")


# ── Public API ────────────────────────────────────────────────────────────


def add_reminder(
    base_dir: Path,
    text_request: str,
    *,
    source_user: str = "zeke",
) -> dict[str, Any] | None:
    """Parse + persist a reminder from a natural-language request.

    Returns the new entry on success, None if no time clause was parsed.
    """
    when = parse_when(text_request)
    if when is None:
        return None
    what = extract_action(text_request)
    if not what:
        return None
    entry = {
        "id": _new_id(),
        "when_iso": when.isoformat(timespec="seconds"),
        "what": what[:200],
        "source_user": source_user,
        "created": time.time(),
        "fired": False,
    }
    with _LOCK:
        entries = _load(base_dir)
        entries.append(entry)
        _save(base_dir, entries)
    return entry


def list_pending(base_dir: Path) -> list[dict[str, Any]]:
    with _LOCK:
        return [e for e in _load(base_dir) if not e.get("fired")]


def mark_fired(base_dir: Path, entry_id: str) -> None:
    with _LOCK:
        entries = _load(base_dir)
        for e in entries:
            if e.get("id") == entry_id:
                e["fired"] = True
                e["fired_at"] = time.time()
                break
        _save(base_dir, entries)


# ── Background watcher ────────────────────────────────────────────────────


def _fire(g: dict[str, Any], entry: dict[str, Any]) -> None:
    """Speak the reminder via TTS worker."""
    user = entry.get("source_user") or "you"
    what = entry.get("what") or ""
    line = f"Hey {user.title()}, you asked me to remind you: {what}."
    print(f"[scheduler] firing reminder id={entry.get('id')} when={entry.get('when_iso')} what={what!r}")
    worker = g.get("_tts_worker")
    if worker is not None and getattr(worker, "available", False):
        try:
            worker.speak(line, emotion="curiosity", intensity=0.55, blocking=False)
        except Exception as e:
            print(f"[scheduler] TTS speak error: {e!r}")
    # Also append to chat_history so the UI sees it.
    try:
        from pathlib import Path as _P
        import json as _json
        base = _P(g.get("BASE_DIR") or ".")
        hp = base / "state" / "chat_history.jsonl"
        hp.parent.mkdir(parents=True, exist_ok=True)
        with hp.open("a", encoding="utf-8") as f:
            f.write(_json.dumps({
                "ts": time.time(),
                "role": "assistant",
                "source": "ava_initiated",
                "content": line,
                "person_id": user,
                "model": "scheduler",
                "emotion": "curiosity",
                "turn_route": "scheduled_reminder",
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[scheduler] chat_history append error: {e!r}")


def _watcher_loop(g: dict[str, Any]) -> None:
    base = Path(g.get("BASE_DIR") or ".")
    while not _WATCHER_STOP.wait(_TICK_SECONDS):
        try:
            now = datetime.now()
            with _LOCK:
                entries = _load(base)
                fired_any = False
                for e in entries:
                    if e.get("fired"):
                        continue
                    try:
                        when = datetime.fromisoformat(e.get("when_iso") or "")
                    except Exception:
                        continue
                    if when <= now:
                        _fire(g, e)
                        e["fired"] = True
                        e["fired_at"] = time.time()
                        fired_any = True
                if fired_any:
                    _save(base, entries)
        except Exception as e:
            print(f"[scheduler] watcher error: {e!r}")


def start_watcher(g: dict[str, Any]) -> None:
    """Start the background tick loop. Idempotent."""
    global _WATCHER_THREAD
    if _WATCHER_THREAD is not None and _WATCHER_THREAD.is_alive():
        return
    _WATCHER_STOP.clear()
    _WATCHER_THREAD = threading.Thread(
        target=_watcher_loop, args=(g,), daemon=True, name="scheduler-watcher"
    )
    _WATCHER_THREAD.start()
    print("[scheduler] watcher started")


def stop_watcher() -> None:
    _WATCHER_STOP.set()
