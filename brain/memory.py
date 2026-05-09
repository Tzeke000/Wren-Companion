from __future__ import annotations

from .perception import PerceptionState
from .shared import extract_text, iso_to_ts


def remember_with_context(host: dict, text: str, person_id: str, perception: PerceptionState) -> str | None:
    """
    Store a memory with visual and emotional context attached.
    Wraps the existing remember_memory() in avaagent.py (no extra= kwarg there).
    """
    remember_fn = host.get("remember_memory")
    if not callable(remember_fn):
        return None

    positive = {"happy", "surprise"}
    negative = {"angry", "disgust", "fear", "sad"}
    emotion = (perception.face_emotion or "neutral").lower()
    if emotion in positive:
        valence = "positive"
    elif emotion in negative:
        valence = "negative"
    else:
        valence = "neutral"

    visual_context = f"face={'yes' if perception.face_detected else 'no'}, emotion={emotion}"
    extra_tags = ["visual_context"] if perception.face_detected else []
    tags = ["perception", valence] + extra_tags
    enriched = f"[{visual_context} | valence={valence}] {text}".strip()

    try:
        return remember_fn(
            enriched,
            person_id=person_id,
            category="episodic",
            importance=0.6,
            source="ava_perception",
            tags=tags,
        )
    except TypeError:
        try:
            return remember_fn(text, person_id=person_id, category="episodic", importance=0.6)
        except Exception:
            return None
    except Exception:
        try:
            return remember_fn(text, person_id=person_id, category="episodic", importance=0.6)
        except Exception:
            return None


def recall_for_person(host: dict, person_id: str | None, limit: int = 5) -> list[str]:
    """
    Surface recent vector memories for a recognized person (face-triggered recall).
    """
    if not person_id:
        return []

    list_fn = host.get("list_recent_memories")
    if callable(list_fn):
        try:
            rows = list_fn(person_id, limit) or []
        except Exception:
            rows = []
        out: list[str] = []
        for item in rows[:limit]:
            if not isinstance(item, dict):
                t = extract_text(item)
                if t and str(t).strip():
                    out.append(str(t).strip()[:300])
                continue
            meta = item.get("metadata") or {}
            raw = (meta.get("raw_text") or item.get("text") or "").strip()
            if not raw:
                raw = str(item.get("text", "") or "").strip()
            if raw:
                out.append(raw[:300])
        return out

    search_fn = host.get("search_memories")
    if callable(search_fn):
        try:
            q = str(person_id)
            rows = search_fn(q, person_id=person_id, k=limit) or []
        except Exception:
            return []
        out = []
        for item in rows[:limit]:
            if isinstance(item, dict):
                t = (item.get("text") or "").strip()
            else:
                t = str(item).strip()
            if t:
                out.append(t[:300])
        return out

    return []


def decay_tick(host: dict) -> None:
    """
    Lightly reduce importance of memories not accessed in 30+ days (by created_at).
    Never deletes — only adjusts override importance downward.
    """
    import time

    thirty_days = 30 * 24 * 3600
    now = time.time()

    list_fn = host.get("list_memories") or host.get("get_all_memories")
    update_fn = host.get("set_memory_importance")
    if not callable(list_fn) or not callable(update_fn):
        return

    try:
        memories = list_fn()
        for m in memories or []:
            if not isinstance(m, dict):
                continue
            last_accessed = float(m.get("last_accessed_ts") or 0.0)
            created = float(m.get("created_ts") or 0.0)
            ref_ts = last_accessed or created
            if ref_ts <= 0.0:
                ref_ts = iso_to_ts(str(m.get("created_at", "") or ""))
            if ref_ts <= 0.0:
                continue
            if (now - ref_ts) <= thirty_days:
                continue
            try:
                current_importance = float(m.get("importance", m.get("importance_score", 0.5)))
            except (TypeError, ValueError):
                current_importance = 0.5
            new_importance = max(0.1, current_importance - 0.05)
            mem_id = m.get("memory_id") or m.get("id")
            if not mem_id:
                continue
            try:
                update_fn(mem_id, new_importance, reason="decay_tick")
            except Exception:
                pass
    except Exception:
        pass


def describe_memory_integrity(host: dict) -> str:
    fn = host.get("get_memory_status")
    if callable(fn):
        try:
            return str(fn())
        except Exception as e:
            return f"error: {e}"
    return "memory status unavailable"


def search_reflections(host, query, *args, **kwargs):
    """Legacy bridge — delegates to host reflection search if present."""
    orig = host.get("_BRAIN_ORIG_SEARCH_REFLECTIONS")
    if callable(orig):
        return orig(query, *args, **kwargs)
    return []
