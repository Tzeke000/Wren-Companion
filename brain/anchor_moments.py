"""brain/anchor_moments.py — Anchor moments (D16).

Some interactions matter more than others. First conversation. Moment
of real connection. The time both laughed at something. Marked as
ANCHORS — they don't get pruned by daily summarization, they stay
vivid in long-term memory.

Same shape as how human episodic memory works. You don't remember
every word of every day, but you remember specific moments — vividly,
sometimes for life.

Storage: state/anchor_moments.jsonl (append-only, persistent —
classified PERSISTENT, never pruned). Each entry:

  {
    "id": "anchor-<ts>-<slug>",
    "ts": <unix>,
    "person_id": "zeke",
    "kind": "first_conversation" | "connection" | "humor"
            | "vulnerable_share" | "milestone" | "self_chosen",
    "summary": "Zeke explained his upbringing for the first time",
    "context": {
      "user_message": "...",
      "ava_reply": "...",
      "conversation_excerpt": "..."  # surrounding context
    },
    "ava_marked_at": <ts>,  # when Ava decided this was anchor-worthy
    "marked_by": "ava" | "zeke" | "auto"
  }

API:

    from brain.anchor_moments import (
        mark_anchor, list_anchors, recent_anchors,
        is_anchor_worthy, auto_detect_anchor_in_turn,
    )

    # Manually mark (Ava's introspection identifies a meaningful
    # moment, OR Zeke says "this matters / mark this"):
    mark_anchor(person_id="zeke", kind="vulnerable_share",
                summary="Zeke shared his upbringing context",
                context={...})

    # Auto-detect: heuristic + LLM judgment for each turn
    if auto_detect_anchor_in_turn(person_id, user_msg, ava_reply):
        mark_anchor(...)

    # Surface for context-building:
    anchors = recent_anchors("zeke", limit=5)
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


AnchorKind = Literal[
    "first_conversation",
    "connection",
    "humor",
    "vulnerable_share",
    "milestone",
    "decision",
    "self_chosen",
]


@dataclass
class AnchorMoment:
    id: str
    ts: float
    person_id: str
    kind: str
    summary: str
    context: dict[str, Any] = field(default_factory=dict)
    ava_marked_at: float = field(default_factory=time.time)
    marked_by: str = "ava"


_lock = threading.RLock()
_base_dir: Path | None = None
_cache: list[AnchorMoment] = []


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "anchor_moments.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _cache
    p = _path()
    if p is None or not p.exists():
        _cache = []
        return
    out: list[AnchorMoment] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(AnchorMoment(
                        id=str(d.get("id") or uuid.uuid4().hex[:12]),
                        ts=float(d.get("ts") or 0.0),
                        person_id=str(d.get("person_id") or ""),
                        kind=str(d.get("kind") or "self_chosen"),
                        summary=str(d.get("summary") or ""),
                        context=dict(d.get("context") or {}),
                        ava_marked_at=float(d.get("ava_marked_at") or 0.0),
                        marked_by=str(d.get("marked_by") or "ava"),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[anchor_moments] load error: {e!r}")
    _cache = out


def _append_to_disk(anchor: AnchorMoment) -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": anchor.id,
                "ts": anchor.ts,
                "person_id": anchor.person_id,
                "kind": anchor.kind,
                "summary": anchor.summary,
                "context": anchor.context,
                "ava_marked_at": anchor.ava_marked_at,
                "marked_by": anchor.marked_by,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[anchor_moments] append error: {e!r}")


# ── Public API ────────────────────────────────────────────────────────────


def mark_anchor(
    *,
    person_id: str,
    kind: str,
    summary: str,
    context: dict[str, Any] | None = None,
    marked_by: str = "ava",
) -> str:
    """Mark a moment as an anchor. Returns the anchor id."""
    if not person_id or not summary:
        return ""
    aid = f"anchor-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    anchor = AnchorMoment(
        id=aid,
        ts=time.time(),
        person_id=person_id,
        kind=kind,
        summary=summary,
        context=dict(context or {}),
        ava_marked_at=time.time(),
        marked_by=marked_by,
    )
    with _lock:
        _cache.append(anchor)
        _append_to_disk(anchor)
    print(f"[anchor_moments] marked {aid!r} kind={kind} for {person_id}")
    return aid


def list_anchors(person_id: str | None = None) -> list[AnchorMoment]:
    with _lock:
        if person_id is None:
            return list(_cache)
        return [a for a in _cache if a.person_id == person_id]


def recent_anchors(person_id: str | None = None, *, limit: int = 10) -> list[AnchorMoment]:
    items = list_anchors(person_id)
    items.sort(key=lambda a: a.ts, reverse=True)
    return items[:int(limit)]


# ── Auto-detection heuristics ────────────────────────────────────────────


# Words / phrases that suggest the moment matters — vulnerable disclosure,
# milestone language, deep emotional content.
_ANCHOR_HINT_PATTERNS = [
    re.compile(r"\b(?:first|never (?:told|shared|said))\b", re.IGNORECASE),
    re.compile(r"\b(?:i (?:love|trust|appreciate) (?:you|that))\b", re.IGNORECASE),
    re.compile(r"\b(?:thank\s+you|grateful|means a lot)\b", re.IGNORECASE),
    re.compile(r"\b(?:remember|won'?t forget|always remember)\b", re.IGNORECASE),
    re.compile(r"\b(?:my (?:mom|dad|parents|brother|sister|family))\b", re.IGNORECASE),
    re.compile(r"\b(?:hard for me|vulnerable|honestly?\s*,?\s*i)\b", re.IGNORECASE),
    re.compile(r"\b(?:milestone|huge|big deal|life changing|life-changing)\b", re.IGNORECASE),
    re.compile(r"\b(?:happy birthday|anniversary)\b", re.IGNORECASE),
]


_ANCHOR_KIND_HEURISTICS = {
    "vulnerable_share": [
        re.compile(r"\b(?:my (?:mom|dad|parents|family)|hard for me|vulnerable|honestly)\b", re.IGNORECASE),
    ],
    "humor": [
        re.compile(r"\b(?:laughed|laughing|funny|hilarious|cracked me up)\b", re.IGNORECASE),
    ],
    "connection": [
        re.compile(r"\b(?:trust|love|appreciate|grateful|means a lot)\b", re.IGNORECASE),
    ],
    "milestone": [
        re.compile(r"\b(?:milestone|first|huge|big deal|achieved|accomplished)\b", re.IGNORECASE),
    ],
}


def is_anchor_worthy(text: str) -> bool:
    """Heuristic: does this text contain anchor-suggestive language?"""
    if not text:
        return False
    return any(p.search(text) for p in _ANCHOR_HINT_PATTERNS)


def infer_kind(user_msg: str, ava_reply: str = "") -> str:
    combined = f"{user_msg}\n{ava_reply}"
    for kind, patterns in _ANCHOR_KIND_HEURISTICS.items():
        if any(p.search(combined) for p in patterns):
            return kind
    return "connection"


def auto_detect_anchor_in_turn(
    person_id: str,
    user_msg: str,
    ava_reply: str,
) -> str | None:
    """If the turn looks anchor-worthy, mark + return the anchor id.

    Returns None if the turn doesn't look anchor-worthy.

    Heuristic for the lite version: requires user_msg to match one
    of the _ANCHOR_HINT_PATTERNS. Future versions could use an LLM
    classifier to be more nuanced.
    """
    if not user_msg or not ava_reply:
        return None
    if not is_anchor_worthy(user_msg) and not is_anchor_worthy(ava_reply):
        return None
    kind = infer_kind(user_msg, ava_reply)
    summary = (user_msg.strip().split("\n", 1)[0])[:140]
    return mark_anchor(
        person_id=person_id,
        kind=kind,
        summary=summary,
        context={
            "user_message": user_msg[:300],
            "ava_reply": ava_reply[:300],
        },
        marked_by="auto",
    )
