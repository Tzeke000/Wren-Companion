"""brain/comparative_memory.py — Comparative-state memory (D14).

"Yesterday you were calmer." / "This is the first time we've talked
about photography." / "You laughed more than usual today."

Computes deltas vs baseline. A person notices these things; an
assistant doesn't. The difference is care.

Today: mood snapshots accumulate via existing mood_carryover. This
module is the QUERY layer that compares current vs past. Future
work could add behavioral deltas (typing speed, response latency,
question pattern shifts).

API:

    from brain.comparative_memory import (
        snapshot_mood, mood_delta_vs_yesterday,
        first_time_for_topic, observation_for_user,
    )

    delta = mood_delta_vs_yesterday(g)
    if delta and delta["primary_change"] == "calmer":
        # Ava can say "you seem calmer today than yesterday"
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MoodSnapshot:
    ts: float
    person_id: str
    primary_mood: str
    weights: dict[str, float] = field(default_factory=dict)


_lock = threading.RLock()
_base_dir: Path | None = None
_snapshots: list[MoodSnapshot] = []
_MAX_SNAPSHOTS = 1000


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "mood_snapshots.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _snapshots
    p = _path()
    if p is None or not p.exists():
        _snapshots = []
        return
    out: list[MoodSnapshot] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(MoodSnapshot(
                        ts=float(d.get("ts") or 0.0),
                        person_id=str(d.get("person_id") or ""),
                        primary_mood=str(d.get("primary_mood") or ""),
                        weights=dict(d.get("weights") or {}),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[comparative_memory] load error: {e!r}")
    _snapshots = out[-_MAX_SNAPSHOTS:]


def _append(s: MoodSnapshot) -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": s.ts, "person_id": s.person_id,
                "primary_mood": s.primary_mood,
                "weights": s.weights,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[comparative_memory] append error: {e!r}")


def snapshot_mood(g: dict[str, Any], *, person_id: str | None = None) -> bool:
    """Capture current mood as a snapshot."""
    try:
        load_mood = g.get("load_mood")
        if not callable(load_mood):
            return False
        mood = load_mood() or {}
        primary = str(mood.get("current_mood") or mood.get("primary_emotion") or "")
        weights = dict(mood.get("emotion_weights") or {})
        if not primary:
            return False
        s = MoodSnapshot(
            ts=time.time(),
            person_id=person_id or str(g.get("_active_person_id") or ""),
            primary_mood=primary,
            weights=weights,
        )
        with _lock:
            _snapshots.append(s)
            _snapshots[:] = _snapshots[-_MAX_SNAPSHOTS:]
            _append(s)
        return True
    except Exception as e:
        print(f"[comparative_memory] snapshot error: {e!r}")
        return False


def _avg_weights(snapshots: list[MoodSnapshot]) -> dict[str, float]:
    if not snapshots:
        return {}
    keys: set[str] = set()
    for s in snapshots:
        keys.update(s.weights.keys())
    out: dict[str, float] = {}
    for k in keys:
        vals = [float(s.weights.get(k) or 0.0) for s in snapshots]
        out[k] = sum(vals) / len(vals)
    return out


def mood_delta_vs_yesterday(g: dict[str, Any]) -> dict[str, Any] | None:
    """Compare current mood vs the last 24 hours' average."""
    person_id = str(g.get("_active_person_id") or "")
    now = time.time()
    yesterday_start = now - 48 * 3600
    yesterday_end = now - 12 * 3600
    today_start = now - 8 * 3600
    with _lock:
        yesterday = [s for s in _snapshots
                     if yesterday_start <= s.ts <= yesterday_end
                     and (not person_id or s.person_id == person_id)]
        today = [s for s in _snapshots
                 if s.ts >= today_start
                 and (not person_id or s.person_id == person_id)]
    if not yesterday or not today:
        return None
    y_avg = _avg_weights(yesterday)
    t_avg = _avg_weights(today)
    deltas: dict[str, float] = {}
    for k in set(y_avg) | set(t_avg):
        d = t_avg.get(k, 0.0) - y_avg.get(k, 0.0)
        if abs(d) > 0.05:
            deltas[k] = d
    primary_change = ""
    if deltas:
        biggest = sorted(deltas.items(), key=lambda kv: abs(kv[1]), reverse=True)[0]
        emotion, delta_val = biggest
        if emotion in ("calmness", "peace") and delta_val > 0:
            primary_change = "calmer"
        elif emotion in ("calmness", "peace") and delta_val < 0:
            primary_change = "more agitated"
        elif emotion in ("anger", "frustration") and delta_val > 0:
            primary_change = "more frustrated"
        elif emotion in ("joy", "happiness", "excitement") and delta_val > 0:
            primary_change = "happier"
        elif emotion in ("sadness", "loneliness") and delta_val > 0:
            primary_change = "lower"
        elif emotion == "boredom" and delta_val > 0:
            primary_change = "more restless"
    return {
        "today_count": len(today),
        "yesterday_count": len(yesterday),
        "deltas": deltas,
        "primary_change": primary_change,
    }


def first_time_for_topic(person_id: str, topic: str) -> bool:
    try:
        from brain.theory_of_mind import has_topic_been_told
        return not has_topic_been_told(person_id, topic)
    except Exception:
        return False


def observation_for_user(g: dict[str, Any]) -> str:
    """Produce a comparative observation Ava might say, or "" if nothing notable."""
    delta = mood_delta_vs_yesterday(g)
    if delta and delta.get("primary_change"):
        change = delta["primary_change"]
        if change == "calmer":
            return "You seem calmer today than yesterday."
        if change == "more agitated":
            return "Yesterday you were calmer than today."
        if change == "more frustrated":
            return "More frustrated than yesterday — what's up?"
        if change == "happier":
            return "Higher energy than yesterday — nice."
        if change == "lower":
            return "Lower than yesterday. What's going on?"
        if change == "more restless":
            return "Restless today. Want to talk?"
    return ""
