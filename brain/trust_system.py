"""
Phase 98 — Progressive trust system.

Trust is earned over time, not assigned. Ava develops her own trust intuition.
She may weight events differently than we expect — that's her judgment.

Trust levels:
  0.0-0.2: Stranger   — minimal sharing, no personal topics
  0.2-0.4: Acquaintance — surface level
  0.4-0.6: Known      — normal conversation
  0.6-0.8: Trusted    — open conversation, opinions shared freely
  0.8-1.0: Deep Trust — genuine relationship, full openness

Wire into prompt_builder deep path (inject trust context).
Wire into reply_engine (trust level affects tool permissions).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

_TRUST_STATE_PATH = "state/trust_scores.json"

_INITIAL_TRUST = {
    "stranger": 0.3,
    "introduced_by_zeke": 0.5,
    "zeke": 0.95,
}

_TRUST_LABELS = [
    (0.8, "deep_trust"),
    (0.6, "trusted"),
    (0.4, "known"),
    (0.2, "acquaintance"),
    (0.0, "stranger"),
]


def _state_path(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".") / _TRUST_STATE_PATH


def _load_scores(g: dict[str, Any]) -> dict[str, Any]:
    path = _state_path(g)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_scores(g: dict[str, Any], scores: dict[str, Any]) -> None:
    path = _state_path(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scores, indent=2, ensure_ascii=False), encoding="utf-8")


def _initial_trust_for(person_id: str, g: dict[str, Any]) -> float:
    owner = str(g.get("OWNER_PERSON_ID") or "zeke")
    if person_id == owner:
        return _INITIAL_TRUST["zeke"]
    # Check if introduced by Zeke (in profile)
    try:
        base = Path(g.get("BASE_DIR") or ".")
        profile_path = base / "profiles" / f"{person_id}.json"
        if profile_path.is_file():
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            if str(profile.get("relationship_to_zeke") or "") in ("friend", "family", "partner", "colleague"):
                return _INITIAL_TRUST["introduced_by_zeke"]
    except Exception:
        pass
    return _INITIAL_TRUST["stranger"]


def get_trust_level(person_id: str, g: dict[str, Any]) -> float:
    scores = _load_scores(g)
    if person_id in scores:
        return float(scores[person_id].get("score") or 0.3)
    # Initialize
    init = _initial_trust_for(person_id, g)
    scores[person_id] = {"score": init, "events": [], "initialized_at": time.time()}
    _save_scores(g, scores)
    return init


def update_trust(person_id: str, delta: float, reason: str, g: dict[str, Any]) -> float:
    """
    Adjust trust score. delta: -1.0 to +1.0 (small values typical).
    Returns new trust level.
    """
    scores = _load_scores(g)
    if person_id not in scores:
        init = _initial_trust_for(person_id, g)
        scores[person_id] = {"score": init, "events": [], "initialized_at": time.time()}

    current = float(scores[person_id].get("score") or 0.3)
    new_score = max(0.0, min(1.0, current + float(delta)))
    scores[person_id]["score"] = round(new_score, 4)
    events = list(scores[person_id].get("events") or [])
    events.append({"ts": time.time(), "delta": round(float(delta), 4), "reason": str(reason)[:100]})
    scores[person_id]["events"] = events[-20:]  # keep last 20 events
    _save_scores(g, scores)
    print(f"[trust] {person_id}: {current:.3f} → {new_score:.3f} ({reason[:60]})")
    return new_score


def get_trust_label(person_id: str, g: dict[str, Any]) -> str:
    score = get_trust_level(person_id, g)
    for threshold, label in _TRUST_LABELS:
        if score >= threshold:
            return label
    return "stranger"


def get_trust_context(person_id: str, g: dict[str, Any]) -> str:
    """Returns trust-appropriate behavior guidance for prompt injection."""
    label = get_trust_label(person_id, g)
    score = get_trust_level(person_id, g)
    if label == "deep_trust":
        return f"TRUST LEVEL: {label} ({score:.2f}) — full openness; this is a genuine relationship."
    elif label == "trusted":
        return f"TRUST LEVEL: {label} ({score:.2f}) — open conversation, share opinions freely."
    elif label == "known":
        return f"TRUST LEVEL: {label} ({score:.2f}) — normal conversation; some personal topics okay."
    elif label == "acquaintance":
        return f"TRUST LEVEL: {label} ({score:.2f}) — surface level; be warm but measured."
    else:
        return f"TRUST LEVEL: {label} ({score:.2f}) — minimal sharing; don't disclose Zeke's private info."


def get_all_trust_scores(g: dict[str, Any]) -> dict[str, Any]:
    return _load_scores(g)
