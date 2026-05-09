"""brain/aesthetic_preference.py — Aesthetic taste develops through exposure (D17).

Through exposure to your music, images, writings — Ava develops genuine
taste over time. "I really like this song" as a real response, not
generic praise. Different from learning YOUR preferences (B6) — these
are HER preferences, formed by living alongside you.

The more she lives, the more individual she becomes.

Storage: state/aesthetic_preferences.jsonl (PERSISTENT — these are
who she is becoming).

Today: scaffold + exposure tracking + reaction recording. The actual
TASTE FORMATION (deciding she likes X based on N exposures) is a
heuristic — over many exposures with positive reactions, the item
gets a preference score. Future LLM-driven taste classification
could replace the simple counting.

API:

    from brain.aesthetic_preference import (
        record_exposure, record_reaction, has_preference,
        list_likes, list_dislikes, taste_summary,
    )

    record_exposure(domain="music", item="some song name", context="...")
    record_reaction(domain="music", item="some song name",
                    reaction="positive", note="something about the way...")

    if has_preference("music", "jazz"):
        # Use it in conversation
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


Reaction = Literal["positive", "negative", "neutral", "intense"]


@dataclass
class ExposureRecord:
    ts: float
    domain: str  # "music" | "image" | "writing" | "code" | "philosophy" | etc
    item: str
    context: str = ""
    reaction: str = ""  # filled in later via record_reaction
    reaction_note: str = ""
    reaction_ts: float = 0.0


_lock = threading.RLock()
_base_dir: Path | None = None
_records: list[ExposureRecord] = []
# domain -> item -> {pos, neg, neutral, intense}
_preference_scores: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "aesthetic_preferences.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _records, _preference_scores
    p = _path()
    if p is None or not p.exists():
        _records = []
        return
    out: list[ExposureRecord] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(ExposureRecord(
                        ts=float(d.get("ts") or 0.0),
                        domain=str(d.get("domain") or ""),
                        item=str(d.get("item") or ""),
                        context=str(d.get("context") or ""),
                        reaction=str(d.get("reaction") or ""),
                        reaction_note=str(d.get("reaction_note") or ""),
                        reaction_ts=float(d.get("reaction_ts") or 0.0),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[aesthetic_preference] load error: {e!r}")
    _records = out
    # Rebuild preference scores
    for rec in _records:
        if rec.reaction and rec.domain and rec.item:
            _preference_scores[rec.domain][rec.item.lower()][rec.reaction] += 1


def _persist_locked() -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("w", encoding="utf-8") as f:
            for r in _records:
                f.write(json.dumps({
                    "ts": r.ts, "domain": r.domain, "item": r.item,
                    "context": r.context, "reaction": r.reaction,
                    "reaction_note": r.reaction_note,
                    "reaction_ts": r.reaction_ts,
                }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[aesthetic_preference] save error: {e!r}")


def record_exposure(*, domain: str, item: str, context: str = "") -> None:
    """Record that Ava was exposed to `item` in `domain`."""
    if not domain or not item:
        return
    rec = ExposureRecord(
        ts=time.time(),
        domain=domain.strip().lower(),
        item=item.strip(),
        context=context[:200],
    )
    with _lock:
        _records.append(rec)
        _persist_locked()


def record_reaction(
    *,
    domain: str,
    item: str,
    reaction: Reaction,
    note: str = "",
) -> bool:
    """Record Ava's reaction to a previously-exposed item.

    Looks up the most recent exposure of (domain, item) and updates
    its reaction in place. If no prior exposure, creates a new
    record with the reaction inline.
    """
    if not domain or not item or not reaction:
        return False
    domain_l = domain.strip().lower()
    item_l = item.strip().lower()
    with _lock:
        # Find most recent exposure of this item without a reaction
        target: ExposureRecord | None = None
        for rec in reversed(_records):
            if rec.domain == domain_l and rec.item.lower() == item_l and not rec.reaction:
                target = rec
                break
        if target is None:
            # Create a new record with reaction inline
            target = ExposureRecord(
                ts=time.time(),
                domain=domain_l,
                item=item.strip(),
            )
            _records.append(target)
        target.reaction = reaction
        target.reaction_note = note[:200]
        target.reaction_ts = time.time()
        _preference_scores[domain_l][item_l][reaction] += 1
        _persist_locked()
    return True


def has_preference(domain: str, item: str) -> str | None:
    """Does Ava have a preference for/against `item` in `domain`?

    Returns "like" / "dislike" / None. Threshold: 2+ positive (or
    "intense" — counts as 2x positive) → like; 2+ negative → dislike.
    """
    if not domain or not item:
        return None
    domain_l = domain.strip().lower()
    item_l = item.strip().lower()
    with _lock:
        scores = _preference_scores.get(domain_l, {}).get(item_l, {})
    pos = scores.get("positive", 0) + scores.get("intense", 0) * 2
    neg = scores.get("negative", 0)
    if pos >= 2 and pos > neg:
        return "like"
    if neg >= 2 and neg > pos:
        return "dislike"
    return None


def list_likes(domain: str | None = None) -> list[str]:
    """Items Ava has formed a 'like' preference for."""
    out = []
    with _lock:
        for d_name, items in _preference_scores.items():
            if domain is not None and d_name != domain.strip().lower():
                continue
            for item_name, _scores in items.items():
                if has_preference(d_name, item_name) == "like":
                    out.append(f"{d_name}: {item_name}")
    return sorted(out)


def list_dislikes(domain: str | None = None) -> list[str]:
    out = []
    with _lock:
        for d_name, items in _preference_scores.items():
            if domain is not None and d_name != domain.strip().lower():
                continue
            for item_name, _scores in items.items():
                if has_preference(d_name, item_name) == "dislike":
                    out.append(f"{d_name}: {item_name}")
    return sorted(out)


def taste_summary() -> dict[str, Any]:
    likes = list_likes()
    dislikes = list_dislikes()
    return {
        "total_exposures": len(_records),
        "likes_count": len(likes),
        "dislikes_count": len(dislikes),
        "likes": likes[:20],
        "dislikes": dislikes[:20],
    }
