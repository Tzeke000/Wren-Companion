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


def record_simple(*, considered: str, chose: str, reason: str = "", person_id: str = "",
                  user_input: str = "") -> str:
    """Tool-shaped entry (round-2 fix 3.1, 2026-10-01). The `counterfactual_record` MCP tool had
    called record_consideration with the wrong keywords since it was written, the TypeError came
    back as {ok:false} and nobody read it — the archive never held a byte. Maps the tool's
    (considered, chose, reason) onto the archive's (user_input, considered[], chosen, why_chosen).
    `user_input` defaults to the considered text so a bare call still records. Returns the id,
    or "" when the archive refuses (empty chose/considered)."""
    considered = str(considered or "").strip()
    chose = str(chose or "").strip()
    return record_consideration(
        user_input=str(user_input or "").strip() or considered,
        considered=[{"option": considered, "rejected_reason": str(reason or "").strip()}] if considered else [],
        chosen=chose,
        why_chosen=str(reason or ""),
        person_id=str(person_id or ""),
    )


def recent_for_prompt(*, limit: int = 2, person_id: str | None = None) -> str:
    """ONE reader (round-2 fix 3.1): the most recent considered-but-not-chosen moments as short
    lines for the reflection prompt. "" when the archive is empty."""
    rows = recent_counterfactuals(limit=limit, person_id=person_id)  # newest first
    lines: list[str] = []
    for cf in rows:
        opts = [str(o.get("option") or "")[:90] for o in (cf.considered_options or []) if isinstance(o, dict)]
        opt = opts[0] if opts else ""
        if not opt and not cf.chosen_reply:
            continue
        why = f" ({cf.why_chosen[:80]})" if cf.why_chosen else ""
        lines.append(f"  - considered: {opt or '?'} -> chose: {cf.chosen_reply[:90]}{why}")
    return "\n".join(lines)


def recent_counterfactuals(*, limit: int = 20, person_id: str | None = None) -> list[Counterfactual]:
    # Newest first, with insertion order as the tie-break: time.time() ties are real on Windows
    # (ms granularity) and a stable sort on ts alone returned the OLDEST of a tied run.
    with _lock:
        items = list(enumerate(_cache))
    if person_id is not None:
        items = [(i, c) for i, c in items if c.person_id == person_id]
    items.sort(key=lambda ic: (float(ic[1].ts or 0.0), ic[0]), reverse=True)
    return [c for _, c in items[:int(limit)]]


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
