"""
Time-bound prospective memory (commitments, upcoming events) for natural follow-ups.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from .shared import atomic_json_save, json_load, now_iso

DEFAULT_PROMPT_TEMPLATE = (
    "Hey, didn't you say {event_text} was {due_description}? How did it go?"
)


def _store_path(host: dict | None) -> Path:
    if host and host.get("PROSPECTIVE_MEMORY_PATH"):
        return Path(host["PROSPECTIVE_MEMORY_PATH"])
    return Path(__file__).resolve().parent.parent / "state" / "prospective_memory.json"


def _ensure_store(host: dict | None) -> None:
    p = _store_path(host)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        atomic_json_save(str(p), {"events": []})


def _load_doc(host: dict | None) -> dict[str, Any]:
    _ensure_store(host)
    return json_load(str(_store_path(host)), {"events": []})


def _save_doc(host: dict | None, doc: dict[str, Any]) -> None:
    atomic_json_save(str(_store_path(host)), doc)


def load_all_events(host: dict | None = None) -> list[dict]:
    return list((_load_doc(host).get("events") or []))


def save_prospective_event(event: dict, host: dict | None = None) -> None:
    doc = _load_doc(host)
    events = list(doc.get("events") or [])
    eid = event.get("id")
    replaced = False
    for i, ex in enumerate(events):
        if ex.get("id") == eid:
            events[i] = event
            replaced = True
            break
    if not replaced:
        events.append(event)
    doc["events"] = events
    _save_doc(host, doc)


def _parse_due_to_date(due: str | None) -> date | None:
    if not due:
        return None
    s = str(due).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "")[:10]).date()
    except Exception:
        return None


def _due_start_datetime(due_raw: str | None) -> datetime | None:
    """Start of the due calendar day (local), or parsed datetime if ISO includes time."""
    if not due_raw:
        return None
    s = str(due_raw).strip()
    if "T" in s or (len(s) > 10 and s[10] in " T"):
        try:
            return datetime.fromisoformat(s.replace("Z", "")[:19])
        except ValueError:
            pass
    d = _parse_due_to_date(due_raw)
    if d is None:
        return None
    return datetime.combine(d, time(0, 0, 0))


def _event_timing_triple(event: dict) -> tuple[int, int, str]:
    c = int(event.get("cooldown_before_hours", 12))
    ex = int(event.get("expires_after_hours", 72))
    win = str(event.get("mention_window") or "any").lower().strip()
    if win not in ("before", "same_day", "after", "any"):
        win = "any"
    return c, ex, win


def _mention_time_bounds(due_start: datetime, cooldown_h: int, expires_after_h: int) -> tuple[datetime, datetime]:
    earliest = due_start - timedelta(hours=cooldown_h)
    expire_at = due_start + timedelta(hours=24) + timedelta(hours=expires_after_h)
    return earliest, expire_at


def _within_mention_window(mention_window: str, now: datetime, due_day: date) -> bool:
    nd = now.date()
    if mention_window == "any":
        return True
    if mention_window == "same_day":
        return nd == due_day
    if mention_window == "before":
        return nd < due_day
    if mention_window == "after":
        return nd > due_day
    return True


def _retire_expired_prospective_events(now: datetime, host: dict | None) -> None:
    doc = _load_doc(host)
    events = list(doc.get("events") or [])
    changed = False
    for i, ex in enumerate(events):
        if ex.get("status") != "pending":
            continue
        due_start = _due_start_datetime(ex.get("due_date"))
        if due_start is None:
            continue
        c_h, ex_h, _ = _event_timing_triple(ex)
        _, expire_at = _mention_time_bounds(due_start, c_h, ex_h)
        if now > expire_at:
            ex["status"] = "expired"
            events[i] = ex
            changed = True
    if changed:
        doc["events"] = events
        _save_doc(host, doc)


def load_pending_events(person_id: str, host: dict | None = None) -> list[dict]:
    expire_old_events(days=7, host=host)
    return [
        e
        for e in load_all_events(host)
        if e.get("person_id") == person_id and e.get("status") == "pending"
    ]


def get_due_events(
    person_id: str,
    now: datetime | None = None,
    host: dict | None = None,
) -> list[dict]:
    now = now or datetime.now()
    _retire_expired_prospective_events(now, host)
    out: list[dict] = []
    for e in load_pending_events(person_id, host=host):
        due_start = _due_start_datetime(e.get("due_date"))
        if due_start is None:
            continue
        due_day = due_start.date()
        c_h, ex_h, win = _event_timing_triple(e)
        earliest, expire_at = _mention_time_bounds(due_start, c_h, ex_h)
        if not (earliest <= now <= expire_at):
            continue
        if not _within_mention_window(win, now, due_day):
            continue
        out.append(e)
    return out


def mark_triggered(event_id: str, host: dict | None = None) -> None:
    doc = _load_doc(host)
    events = list(doc.get("events") or [])
    for i, ex in enumerate(events):
        if str(ex.get("id")) == str(event_id):
            ex["status"] = "triggered"
            ex["triggered_at"] = now_iso()
            events[i] = ex
            break
    doc["events"] = events
    _save_doc(host, doc)


def expire_old_events(days: int = 7, host: dict | None = None) -> None:
    doc = _load_doc(host)
    events = list(doc.get("events") or [])
    cutoff = datetime.now() - timedelta(days=days)
    changed = False
    for i, ex in enumerate(events):
        if ex.get("status") != "pending":
            continue
        raw = ex.get("created_at") or ""
        try:
            cr = datetime.fromisoformat(str(raw).replace("Z", "")[:19])
        except Exception:
            continue
        if cr < cutoff:
            ex["status"] = "expired"
            events[i] = ex
            changed = True
    if changed:
        doc["events"] = events
        _save_doc(host, doc)
