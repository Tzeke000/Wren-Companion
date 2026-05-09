"""
Detect future-oriented commitments in user text (regex fast path + optional LLM).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from .model_routing import resolve_model_for_execution_path
from .prospective import DEFAULT_PROMPT_TEMPLATE, load_pending_events, save_prospective_event


def classify_social_timing(event_line: str, due_desc: str) -> dict[str, int | str]:
    """Heuristic timing: birthdays same-day; games span due day + a bit after; etc."""
    t = f"{event_line} {due_desc}".lower()
    if "birthday" in t:
        return {"cooldown_before_hours": 0, "expires_after_hours": 96, "mention_window": "same_day"}
    if any(
        x in t
        for x in (
            "football",
            "soccer",
            "basketball",
            "hockey",
            "baseball",
            "game",
            "match",
            "tournament",
            "playoff",
        )
    ):
        return {"cooldown_before_hours": 8, "expires_after_hours": 40, "mention_window": "any"}
    if any(x in t for x in ("interview", "exam", "surgery", "appointment", "deadline")):
        return {"cooldown_before_hours": 10, "expires_after_hours": 72, "mention_window": "any"}
    return {"cooldown_before_hours": 12, "expires_after_hours": 72, "mention_window": "same_day"}

_WEEK = (
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend"
)
_MONTHS = (
    r"january|february|march|april|may|june|july|august|september|october|november|december"
)

TEMPORAL_PATTERNS = [
    r"\btomorrow\b",
    r"\btonight\b",
    r"\btoday\b",
    r"\bnext\s+(week|" + _WEEK + r")\b",
    r"\bthis\s+(" + _WEEK + r")\b",
    r"\bon\s+(" + _WEEK + r")\b",
    r"\b(" + _MONTHS + r")\s+\d{1,2}(st|nd|rd|th)?\b",
    r"\b(" + _MONTHS + r")\s+\d{1,2}\b",
    r"\bin\s+\d+\s+(days?|weeks?|hours?)\b",
    r"\b\d{1,2}/\d{1,2}(/\d{4})?\b",
    r"\b(game|match|appointment|meeting|birthday|interview|deadline|exam|surgery|trip)\b",
]


def _layer1_hit(text: str) -> bool:
    if not (text or "").strip():
        return False
    for p in TEMPORAL_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


def _resolve_due_date(phrase: str, ref: datetime | None = None) -> tuple[str, str]:
    ref = ref or datetime.now()
    raw = (phrase or "").strip()
    p = raw.lower()
    if not p:
        d = ref.date()
        return d.strftime("%Y-%m-%d"), "soon"

    iso = re.search(r"(\d{4}-\d{1,2}-\d{1,2})", raw)
    if iso:
        return iso.group(1), raw

    mdy = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", raw)
    if mdy:
        mo, dy, yr = int(mdy.group(1)), int(mdy.group(2)), mdy.group(3)
        y = int(yr) if yr else ref.year
        try:
            dt = datetime(y, mo, dy)
            return dt.strftime("%Y-%m-%d"), raw
        except ValueError:
            pass

    if "tonight" in p or re.search(r"\btoday\b", p):
        return ref.strftime("%Y-%m-%d"), raw or "today"

    if "tomorrow" in p:
        return (ref + timedelta(days=1)).strftime("%Y-%m-%d"), raw or "tomorrow"

    m = re.search(r"in\s+(\d+)\s+days?", p)
    if m:
        n = int(m.group(1))
        return (ref + timedelta(days=n)).strftime("%Y-%m-%d"), raw

    m = re.search(r"in\s+(\d+)\s+weeks?", p)
    if m:
        n = int(m.group(1))
        return (ref + timedelta(weeks=n)).strftime("%Y-%m-%d"), raw

    m = re.search(r"in\s+(\d+)\s+hours?", p)
    if m:
        # same calendar day for simplicity
        return ref.strftime("%Y-%m-%d"), raw

    if "next week" in p:
        return (ref + timedelta(days=7)).strftime("%Y-%m-%d"), raw

    weekdays = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    for i, name in enumerate(weekdays):
        if re.search(rf"\b{name}\b", p):
            target = i
            today = ref.weekday()
            delta = (target - today) % 7
            if delta == 0:
                delta = 7
            if "next" in p:
                delta += 7
            return (ref + timedelta(days=delta)).strftime("%Y-%m-%d"), raw

    d = ref.date()
    return d.strftime("%Y-%m-%d"), raw or "upcoming"


def _llm_extract(host: dict, user_text: str) -> dict[str, Any] | None:
    tag, _, _ = resolve_model_for_execution_path(
        "prospective_events",
        host if isinstance(host, dict) else None,
        user_text=user_text[:1200],
        commit_to_globals=False,
    )
    llm = ChatOllama(model=tag, temperature=0.35)
    sys = SystemMessage(
        content=(
            "You extract structured data about FUTURE events only. "
            "If the user message does not imply a future commitment, event, or dated plan, respond with exactly: null\n"
            "Otherwise respond with one JSON object only, no markdown, keys: "
            "has_event (bool), event_description (short string), time_reference (string), "
            "person_involved (string or empty), confidence (0-1 float)."
        )
    )
    hum = HumanMessage(
        content=f'User message:\n"""{user_text[:1200]}"""\n\nJSON or null:'
    )
    try:
        res = llm.invoke([sys, hum])
        text = (getattr(res, "content", None) or str(res)).strip()
        if not text or text.lower().startswith("null"):
            return None
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
        if not isinstance(data, dict) or not data.get("has_event"):
            return None
        return data
    except Exception:
        return None


def _dedupe_exists(
    person_id: str,
    event_text: str,
    due_date: str,
    host: dict | None,
) -> bool:
    key = (event_text or "")[:120].lower().strip()
    for ex in load_pending_events(person_id, host=host):
        if ex.get("due_date") == due_date and (ex.get("event_text") or "")[:120].lower().strip() == key:
            return True
    return False


def maybe_extract_prospective_events(
    user_text: str,
    person_id: str,
    host: dict | None,
    source_turn: int = 0,
) -> None:
    text = (user_text or "").strip()
    if not text or not person_id:
        return
    if not _layer1_hit(text):
        return

    data = _llm_extract(host or {}, text)
    if not data:
        return

    desc = str(data.get("event_description") or "").strip()
    if not desc:
        return
    person_inv = str(data.get("person_involved") or "").strip()
    time_ref = str(data.get("time_reference") or data.get("time") or "").strip() or "soon"
    conf = float(data.get("confidence", 0.75) or 0.75)
    conf = max(0.0, min(1.0, conf))

    event_line = desc
    if person_inv:
        event_line = f"{desc} ({person_inv})"

    due_date, due_desc = _resolve_due_date(time_ref)
    if _dedupe_exists(person_id, event_line, due_date, host):
        return

    timing = classify_social_timing(event_line, due_desc)
    evt = {
        "id": str(uuid.uuid4()),
        "person_id": person_id,
        "event_text": event_line,
        "due_date": due_date,
        "due_description": due_desc,
        "trigger": "person_returns",
        "prompt_template": DEFAULT_PROMPT_TEMPLATE,
        "status": "pending",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "triggered_at": None,
        "source_turn": int(source_turn),
        "confidence": round(conf, 3),
        **timing,
    }
    save_prospective_event(evt, host=host)
