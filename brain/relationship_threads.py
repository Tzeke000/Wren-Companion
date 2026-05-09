"""
Rolling emotional/situational threads per person (relationship continuity).
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from .shared import now_iso

MAX_UNRESOLVED_THREADS = 12
THREAD_TRIGGER_IMPORTANCE = 0.58

_EMOTION_KEYWORDS = (
    ("anxious", ("anxious", "anxiety", "worried", "nervous", "panic", "stressed")),
    ("sad", ("sad", "depressed", "down", "crying", "lonely", "empty", "grief")),
    ("angry", ("angry", "furious", "pissed", "resent", "frustrated", "mad ")),
    ("afraid", ("afraid", "scared", "fear", "terrified")),
    ("hurt", ("hurt", "betrayed", "rejected", "abandoned")),
    ("overwhelmed", ("overwhelmed", "burnt out", "burned out", "too much", "drowning")),
)


def _detect_emotion(text: str) -> str:
    t = (text or "").lower()
    for label, keys in _EMOTION_KEYWORDS:
        if any(k in t for k in keys):
            return label
    if "emotion" in t or "feeling" in t:
        return "concerned"
    return ""


def _normalize_topic_key(text: str) -> str:
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return " ".join(words[:6])


def _topics_overlap(a: str, b: str) -> bool:
    ka = set(_normalize_topic_key(a).split())
    kb = set(_normalize_topic_key(b).split())
    if len(ka) < 2 or len(kb) < 2:
        return _normalize_topic_key(a) == _normalize_topic_key(b)
    return len(ka & kb) >= 2


def _thread_brief_from_user(user_input: str) -> str:
    s = re.sub(r"\s+", " ", (user_input or "").strip())
    if not s:
        return ""
    return s[:160] + ("…" if len(s) > 160 else "")


def update_threads_from_reflection(record: dict, profile: dict) -> dict:
    """Create or refresh a thread when the turn looks emotionally significant."""
    importance = float(record.get("importance", 0.0) or 0.0)
    if importance < THREAD_TRIGGER_IMPORTANCE:
        return profile

    tags = list(record.get("tags") or [])
    user_input = str(record.get("user_input") or "")
    emo_kw = _detect_emotion(user_input)
    if not emo_kw and "emotion" not in tags:
        return profile

    emotion = emo_kw or "concerned"
    notes = _thread_brief_from_user(user_input)
    if not notes:
        return profile

    topic_seed = notes[:80].rsplit(".", 1)[0] if "." in notes[:80] else notes[:80]
    topic_key = _normalize_topic_key(topic_seed)

    threads: list[dict] = list(profile.get("threads") or [])
    ts = record.get("timestamp") or now_iso()
    updated = False
    for th in threads:
        if th.get("resolved"):
            continue
        if _topics_overlap(str(th.get("topic", "")), topic_seed) or _topics_overlap(
            str(th.get("notes", "")), topic_seed
        ):
            th["last_mentioned"] = ts
            th["emotion"] = emotion or th.get("emotion", "concerned")
            if notes:
                th["notes"] = notes
            updated = True
            break

    if not updated:
        threads.append(
            {
                "id": str(uuid.uuid4()),
                "topic": topic_seed.strip() or "something on their mind",
                "first_mentioned": ts,
                "last_mentioned": ts,
                "emotion": emotion,
                "resolved": False,
                "notes": notes,
            }
        )

    unresolved = [t for t in threads if not t.get("resolved")]
    resolved = [t for t in threads if t.get("resolved")]
    unresolved.sort(key=lambda x: str(x.get("first_mentioned", "")))
    while len(unresolved) > MAX_UNRESOLVED_THREADS:
        unresolved.pop(0)
    profile["threads"] = resolved + unresolved
    return profile


def unresolved_threads(profile: dict) -> list[dict]:
    out = [t for t in (profile.get("threads") or []) if not t.get("resolved")]
    out.sort(key=lambda x: str(x.get("last_mentioned", "")), reverse=True)
    return out


def mark_thread_resolved(profile: dict, thread_id: str) -> dict:
    tid = str(thread_id)
    for th in profile.get("threads") or []:
        if str(th.get("id")) == tid:
            th["resolved"] = True
            break
    return profile
