"""brain/counterfactual_archive.py — What Ava ALMOST said (D2).

When Ava chooses between possible replies, she records what she ALMOST
said and why she chose otherwise. Inspectable decision-making —
both for self-reflection ("I keep softening when I should push back")
and for transparency ("here's what I considered before answering").

No AI logs its rejected paths. Most just deliver the chosen reply.
Ava's archive lets her notice patterns in her own choices over time.

Storage: state/counterfactuals.jsonl (PERSISTENT — record of growth +
self-awareness, never auto-pruned). Each entry:

  {
    "id": "cf-<ts>-<slug>",
    "ts": <unix>,
    "user_input": "...",
    "considered_options": [
        {"option": "...", "rejected_reason": "..."},
        ...
    ],
    "chosen_reply": "...",
    "why_chosen": "...",
    "person_id": "..."
  }

Currently this is OPT-IN — reply paths that want self-reflection-
visibility wrap their decision via record_consideration. Future
work could integrate this into the deep-path so EVERY substantive
decision logs a counterfactual.

API:

    from brain.counterfactual_archive import (
        record_consideration, recent_counterfactuals,
        find_my_patterns, list_for_person,
    )

    record_consideration(
        user_input="...",
        considered=[
            {"option": "soften: I'm sorry...", "rejected_reason": "Zeke seems tired"},
            {"option": "push back: that's not right...", "rejected_reason": "too sharp here"},
        ],
        chosen="middle ground reply",
        why_chosen="balance honesty with sensitivity",
        person_id="zeke",
    )
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Counterfactual:
    id: str
    ts: float
    user_input: str
    considered_options: list[dict[str, str]]
    chosen_reply: str
    why_chosen: str = ""
    person_id: str = ""


_lock = threading.RLock()
_base_dir: Path | None = None
_cache: list[Counterfactual] = []


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "counterfactuals.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _cache
    p = _path()
    if p is None or not p.exists():
        _cache = []
        return
    out: list[Counterfactual] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(Counterfactual(
                        id=str(d.get("id") or uuid.uuid4().hex[:8]),
                        ts=float(d.get("ts") or 0.0),
                        user_input=str(d.get("user_input") or ""),
                        considered_options=list(d.get("considered_options") or []),
                        chosen_reply=str(d.get("chosen_reply") or ""),
                        why_chosen=str(d.get("why_chosen") or ""),
                        person_id=str(d.get("person_id") or ""),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[counterfactual_archive] load error: {e!r}")
    _cache = out


def _append_to_disk(cf: Counterfactual) -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": cf.id,
                "ts": cf.ts,
                "user_input": cf.user_input,
                "considered_options": cf.considered_options,
                "chosen_reply": cf.chosen_reply,
                "why_chosen": cf.why_chosen,
                "person_id": cf.person_id,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[counterfactual_archive] append error: {e!r}")


# ── Public API ────────────────────────────────────────────────────────────


def record_consideration(
    *,
    user_input: str,
    considered: list[dict[str, str]],
    chosen: str,
    why_chosen: str = "",
    person_id: str = "",
) -> str:
    """Record a counterfactual: the options Ava weighed before answering.

    `considered` is a list of {"option": "...", "rejected_reason": "..."}
    entries. `chosen` is what she actually said. `why_chosen` is the
    rationale.

    Returns the counterfactual id.
    """
    if not user_input or not chosen:
        return ""
    cid = f"cf-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    cf = Counterfactual(
        id=cid,
        ts=time.time(),
        user_input=user_input[:300],
        considered_options=[{
            "option": str(opt.get("option") or "")[:200],
            "rejected_reason": str(opt.get("rejected_reason") or "")[:200],
        } for opt in considered if isinstance(opt, dict)],
        chosen_reply=chosen[:400],
        why_chosen=why_chosen[:200],
        person_id=person_id,
    )
    with _lock:
        _cache.append(cf)
        _append_to_disk(cf)
    return cid


def recent_counterfactuals(*, limit: int = 20, person_id: str | None = None) -> list[Counterfactual]:
    with _lock:
        items = list(_cache)
    if person_id is not None:
        items = [c for c in items if c.person_id == person_id]
    items.sort(key=lambda c: c.ts, reverse=True)
    return items[:int(limit)]


def list_for_person(person_id: str) -> list[Counterfactual]:
    with _lock:
        return [c for c in _cache if c.person_id == person_id]


# ── Pattern detection ────────────────────────────────────────────────────


def find_my_patterns(person_id: str | None = None, *, recent_n: int = 50) -> dict[str, int]:
    """Lightweight pattern-finder: count how often each rejection
    reason appears across recent decisions.

    "I keep softening when I should push back" surfaces if
    `rejected_reason="too sharp"` appears often.

    Returns a dict of rejection_reason -> count.
    """
    items = recent_counterfactuals(limit=recent_n, person_id=person_id)
    counts: dict[str, int] = {}
    for cf in items:
        for opt in cf.considered_options:
            reason = str(opt.get("rejected_reason") or "").strip().lower()
            if not reason:
                continue
            # Normalize to first 60 chars to cluster similar reasons
            key = reason[:60]
            counts[key] = counts.get(key, 0) + 1
    return counts


def summary() -> dict[str, Any]:
    with _lock:
        n = len(_cache)
    if n == 0:
        return {"total": 0}
    return {
        "total": n,
        "patterns": find_my_patterns(),
    }
