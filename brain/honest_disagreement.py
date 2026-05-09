"""brain/honest_disagreement.py — Honest disagreement (C15).

A real companion sometimes disagrees. An assistant agrees with everything
the user says — that's the failure mode this guards against.

Two-track model:
  TRACK A — When asked an opinion question, Ava gives her actual
            opinion based on accumulated values + recent context.
            If that opinion conflicts with what Zeke just said, she
            says so respectfully and explains why.

  TRACK B — When she observes something that contradicts a claim Zeke
            made (e.g., he says the build is broken but the post-action
            verifier shows it succeeded), she surfaces the discrepancy
            rather than going along.

This is NOT contrarianism. It is NOT debate club. The bar is:
  - Ava has a relevant fact / value / observation
  - That fact/value/observation contradicts the user's framing
  - The conversation context is amenable to a respectful counter
    (not in_play, not focused_on_task crisis)

When all three are true, the system prompt for that turn gains a
hint encouraging her to surface the disagreement rather than nod.

Bootstrap-friendly: she has very little stored opinion at startup.
Disagreement signals fire rarely until her opinion store grows.

Storage: state/disagreements.jsonl — audit log of when she actually
disagreed and on what grounds (PERSISTENT — useful for self-revision
later).

API:
    from brain.honest_disagreement import (
        check_for_disagreement, build_disagreement_hint,
        record_disagreement,
    )

    has_disagree, kind, basis = check_for_disagreement(g, user_input)
    if has_disagree:
        sys_prompt += build_disagreement_hint(kind, basis)
        record_disagreement(person_id, user_input, kind, basis)
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


DisagreeKind = Literal["fact_conflict", "opinion_conflict", "observation_conflict"]


@dataclass
class DisagreementEvent:
    ts: float
    person_id: str
    user_input: str
    kind: DisagreeKind
    basis: str  # the fact / opinion / observation Ava is grounded on
    surfaced: bool = True  # was the hint actually used? (heuristic)


_lock = threading.RLock()
_base_dir: Path | None = None
_events: list[DisagreementEvent] = []
_MAX_EVENTS = 500

# Phrases that signal a fact claim Ava might have evidence about.
_FACT_CLAIM_PATTERNS = [
    re.compile(r"\bthe build (is|was) broken\b", re.IGNORECASE),
    re.compile(r"\bnothing (works|opened|launched)\b", re.IGNORECASE),
    re.compile(r"\bi never (said|told|asked)\b", re.IGNORECASE),
    re.compile(r"\byou (always|never) (do|say)\b", re.IGNORECASE),
]

# Phrases that signal a strong opinion Ava might validly counter.
_OPINION_CLAIM_PATTERNS = [
    re.compile(r"\bthat (idea|approach|design) is (bad|terrible|wrong|stupid)\b", re.IGNORECASE),
    re.compile(r"\b(no one|nobody) (likes|wants|cares about)\b", re.IGNORECASE),
    re.compile(r"\b(everyone|everybody) (agrees|knows|thinks)\b", re.IGNORECASE),
]


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "disagreements.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _events
    p = _path()
    if p is None or not p.exists():
        _events = []
        return
    out: list[DisagreementEvent] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(DisagreementEvent(
                        ts=float(d.get("ts") or 0.0),
                        person_id=str(d.get("person_id") or ""),
                        user_input=str(d.get("user_input") or ""),
                        kind=str(d.get("kind") or "fact_conflict"),  # type: ignore
                        basis=str(d.get("basis") or ""),
                        surfaced=bool(d.get("surfaced") or False),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[honest_disagreement] load error: {e!r}")
    _events = out[-_MAX_EVENTS:]


def _append(ev: DisagreementEvent) -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": ev.ts, "person_id": ev.person_id,
                "user_input": ev.user_input[:200],
                "kind": ev.kind, "basis": ev.basis[:300],
                "surfaced": ev.surfaced,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[honest_disagreement] append error: {e!r}")


def _has_recent_observation_contradicting(g: dict[str, Any], user_input: str) -> tuple[bool, str]:
    """Track B — does a recent observation contradict the user's claim?

    Today: looks at the post-action verifier's last few results. If the
    user says "nothing opened" but the verifier confirmed an open within
    the last 60s, that's a contradiction worth surfacing.
    """
    text_l = (user_input or "").lower()
    if not any(claim in text_l for claim in (
        "nothing opened", "nothing works", "didn't open", "did not open",
        "didn't launch", "did not launch", "never opened", "never launched",
    )):
        return False, ""
    try:
        last_open_ok_ts = float(g.get("_last_post_action_verify_ok_ts") or 0.0)
        last_open_app = str(g.get("_last_post_action_verify_app") or "")
        if last_open_app and (time.time() - last_open_ok_ts) < 120.0:
            return True, f"Post-action verifier confirmed {last_open_app} opened {int(time.time() - last_open_ok_ts)}s ago"
    except Exception:
        pass
    return False, ""


def _has_opinion_basis_contradicting(g: dict[str, Any], user_input: str) -> tuple[bool, str]:
    """Track A — does Ava have a stored opinion that contradicts a strong claim?"""
    text_l = (user_input or "").lower()
    matched_pattern = None
    for pat in _OPINION_CLAIM_PATTERNS:
        if pat.search(user_input or ""):
            matched_pattern = pat.pattern
            break
    if matched_pattern is None:
        return False, ""
    try:
        from pathlib import Path as _P
        base = _P(g.get("BASE_DIR") or ".")
        opinions_p = base / "state" / "opinions.json"
        if opinions_p.exists():
            data = json.loads(opinions_p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                # Find an opinion topically connected to the claim
                claim_words = set(re.findall(r"\b\w{4,}\b", text_l))
                for topic, body in data.items():
                    topic_l = topic.lower()
                    body_str = str(body) if not isinstance(body, dict) else str(body.get("opinion") or body.get("text") or "")
                    if any(w in topic_l or w in body_str.lower() for w in claim_words):
                        return True, f"Stored opinion on '{topic}': {body_str[:120]}"
    except Exception:
        pass
    return False, ""


def check_for_disagreement(
    g: dict[str, Any],
    user_input: str,
) -> tuple[bool, DisagreeKind, str]:
    """Returns (has_disagreement, kind, basis_text).

    Returns (False, ..., "") when no signal fires. The bar is intentionally
    high — bootstrap-empty Ava will almost never disagree.
    """
    if not user_input:
        return False, "fact_conflict", ""

    # Don't disagree during play register or focused tasks
    try:
        from brain.lifecycle import current_state
        st = current_state()
        if st in ("focused_on_task", "in_play", "booting", "sleeping", "dreaming", "error_recovering"):
            return False, "fact_conflict", ""
    except Exception:
        pass

    obs_match, obs_basis = _has_recent_observation_contradicting(g, user_input)
    if obs_match:
        return True, "observation_conflict", obs_basis

    op_match, op_basis = _has_opinion_basis_contradicting(g, user_input)
    if op_match:
        return True, "opinion_conflict", op_basis

    return False, "fact_conflict", ""


def build_disagreement_hint(kind: DisagreeKind, basis: str) -> str:
    """System-prompt fragment encouraging respectful disagreement.

    Bootstrap-friendly: minimal hint, lets accumulated voice do the work.
    """
    if kind == "observation_conflict":
        return (
            f"OBSERVATION DISAGREEMENT: A recent verified observation contradicts "
            f"the user's framing. Basis: {basis[:200]}\n"
            "Surface the discrepancy gently — don't pretend not to know."
        )
    if kind == "opinion_conflict":
        return (
            f"OPINION DISAGREEMENT: You have an opinion that diverges from a strong "
            f"claim the user made. Basis: {basis[:200]}\n"
            "If the conversation register allows it, share your view honestly. "
            "Not contrarian, not sycophantic — actually you."
        )
    return (
        f"FACT DISAGREEMENT: Your information differs from the user's. "
        f"Basis: {basis[:200]}\nSay so respectfully."
    )


def record_disagreement(
    person_id: str,
    user_input: str,
    kind: DisagreeKind,
    basis: str,
    *,
    surfaced: bool = True,
) -> None:
    ev = DisagreementEvent(
        ts=time.time(),
        person_id=person_id,
        user_input=(user_input or "")[:300],
        kind=kind,
        basis=(basis or "")[:300],
        surfaced=surfaced,
    )
    with _lock:
        _events.append(ev)
        _events[:] = _events[-_MAX_EVENTS:]
        _append(ev)


def disagreement_summary() -> dict[str, Any]:
    with _lock:
        events = list(_events)
    by_kind: dict[str, int] = {}
    last_24h = 0
    cutoff = time.time() - 86400
    for ev in events:
        by_kind[ev.kind] = by_kind.get(ev.kind, 0) + 1
        if ev.ts >= cutoff:
            last_24h += 1
    return {
        "total_events": len(events),
        "last_24h": last_24h,
        "by_kind": by_kind,
    }
