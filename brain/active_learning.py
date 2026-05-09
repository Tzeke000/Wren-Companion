"""brain/active_learning.py — Active learning from corrections (B1).

When Zeke corrects something Ava SAID (factually wrong), DID (action
mistake), or how she APPROACHED something (process miss), the learning
shouldn't just be "be shorter next time" — that's B6. It should
ALSO be "what specifically did you get wrong, and what's right?"

This module is the FACTUAL/PROCESS correction layer. Complements B6
(style preferences):

  B6: "you're too long" → store reply_length=short
  B1: "no, the meeting is Tuesday not Wednesday" → store correction
       + retrieve when Ava reasons about that meeting again

Distinction is essential: B6 corrections shape register; B1 corrections
shape KNOWLEDGE.

Detection heuristics (initial; LLM can supplant later):

  - "no, [X]" / "actually [X]" / "that's wrong" / "incorrect"
  - "I never said [X]" / "I didn't [X]"
  - "the [X] is/was [Y]" framed as a correction (with "actually",
    "no", contradictory verb to Ava's last claim)
  - explicit: "let me correct you" / "to correct that" / "I should
    clarify"

When a correction is detected, the module:
  1. Captures the prior assistant reply (last turn) for context
  2. Captures the user's correction text
  3. Optional: extracts the corrected fact (LLM-assisted later)
  4. Stores in state/active_corrections.jsonl
  5. Promotes to memory (mem0 / concept_graph) if substantive

At reply time, recent corrections relevant to the current query are
surfaced as a hint: "User previously corrected you on [topic]:
[correction summary]. Don't repeat that mistake."

Bootstrap-friendly: empty by default. First weeks: nothing. Every
correction adds one entry; relevance retrieval improves over time.

Storage: state/active_corrections.jsonl

API:

    from brain.active_learning import (
        detect_correction, capture_correction, relevant_corrections,
        corrections_summary,
    )

    if detect_correction(user_input, last_ava_reply):
        capture_correction(person_id, user_input, last_ava_reply)
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CorrectionRecord:
    id: str
    ts: float
    person_id: str
    user_correction: str
    prior_assistant_reply: str
    extracted_fact: str = ""  # filled by LLM-extraction step (future)
    topic_keywords: list[str] = field(default_factory=list)
    surfaced_count: int = 0


_lock = threading.RLock()
_base_dir: Path | None = None
_records: list[CorrectionRecord] = []
_MAX_RECORDS = 1000

# Stronger correction phrases — high-confidence triggers
_STRONG_CORRECTION_PATTERNS = [
    re.compile(r"\bthat(?:'?s| is| was)\s+(wrong|incorrect|not right|not correct)\b", re.IGNORECASE),
    re.compile(r"\b(?:that|this|it|you)\s+(?:are|is|was|were)\s+(?:wrong|incorrect)\b", re.IGNORECASE),
    re.compile(r"\bi (never|didn'?t|did not) (said|told|asked|do|mean)\b", re.IGNORECASE),
    re.compile(r"\blet me correct\b", re.IGNORECASE),
    re.compile(r"\bto correct that\b", re.IGNORECASE),
    re.compile(r"\bnot (true|accurate|right)\b", re.IGNORECASE),
    re.compile(r"\bget(s|ting)? that wrong\b", re.IGNORECASE),
]

# Weaker patterns — need additional signal (prior reply being a claim)
_WEAK_CORRECTION_PATTERNS = [
    re.compile(r"^(no|nope|actually)\b[,.]?\s+", re.IGNORECASE),
    re.compile(r"\bactually,?\s+(it'?s|the|that'?s)\b", re.IGNORECASE),
]


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "active_corrections.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _records
    p = _path()
    if p is None or not p.exists():
        _records = []
        return
    out: list[CorrectionRecord] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(CorrectionRecord(
                        id=str(d.get("id") or ""),
                        ts=float(d.get("ts") or 0.0),
                        person_id=str(d.get("person_id") or ""),
                        user_correction=str(d.get("user_correction") or ""),
                        prior_assistant_reply=str(d.get("prior_assistant_reply") or ""),
                        extracted_fact=str(d.get("extracted_fact") or ""),
                        topic_keywords=list(d.get("topic_keywords") or []),
                        surfaced_count=int(d.get("surfaced_count") or 0),
                    ))
                except Exception:
                    continue
    except Exception as e:
        print(f"[active_learning] load error: {e!r}")
    _records = out[-_MAX_RECORDS:]


def _persist_locked() -> None:
    p = _path()
    if p is None:
        return
    try:
        with p.open("w", encoding="utf-8") as f:
            for r in _records:
                f.write(json.dumps({
                    "id": r.id, "ts": r.ts, "person_id": r.person_id,
                    "user_correction": r.user_correction[:400],
                    "prior_assistant_reply": r.prior_assistant_reply[:600],
                    "extracted_fact": r.extracted_fact[:300],
                    "topic_keywords": r.topic_keywords[:10],
                    "surfaced_count": r.surfaced_count,
                }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[active_learning] save error: {e!r}")


def _gen_id() -> str:
    return f"corr_{int(time.time())}_{abs(hash(time.time())) % 10000:04d}"


def detect_correction(user_input: str, prior_assistant_reply: str = "") -> bool:
    """Heuristic correction detector.

    Strong patterns fire on their own. Weak patterns require a prior
    assistant reply that contained a substantive claim (more than just
    'OK' or 'I'm here').
    """
    if not user_input:
        return False
    for pat in _STRONG_CORRECTION_PATTERNS:
        if pat.search(user_input):
            return True
    for pat in _WEAK_CORRECTION_PATTERNS:
        if pat.search(user_input):
            if prior_assistant_reply and len(prior_assistant_reply.split()) >= 5:
                return True
    return False


def _extract_topic_keywords(text: str) -> list[str]:
    """Pull content words from the correction for relevance retrieval."""
    if not text:
        return []
    words = re.findall(r"\b[a-zA-Z][a-zA-Z\-]{3,}\b", text)
    stoplist = {
        "actually", "really", "never", "didnt", "didn", "isnt", "isn",
        "thats", "that", "this", "these", "those", "with", "from",
        "what", "when", "where", "which", "would", "could", "should",
        "about", "your", "yours", "mine", "ours", "their", "they",
        "them", "have", "haven", "havent", "wasnt", "wasn", "wont",
        "will", "wouldn", "couldn", "shouldn", "told", "said", "say",
        "saying", "want", "wants", "need", "needs",
    }
    return [w.lower() for w in words if w.lower() not in stoplist][:10]


def capture_correction(
    person_id: str,
    user_correction: str,
    prior_assistant_reply: str,
    *,
    extracted_fact: str = "",
) -> str | None:
    """Persist a correction. Returns the new record id."""
    if not user_correction:
        return None
    rec = CorrectionRecord(
        id=_gen_id(),
        ts=time.time(),
        person_id=str(person_id or "zeke"),
        user_correction=user_correction[:500],
        prior_assistant_reply=prior_assistant_reply[:800],
        extracted_fact=(extracted_fact or "")[:400],
        topic_keywords=_extract_topic_keywords(user_correction),
    )
    with _lock:
        _records.append(rec)
        _records[:] = _records[-_MAX_RECORDS:]
        _persist_locked()
    print(f"[active_learning] captured correction (id={rec.id}, kw={rec.topic_keywords[:3]})")
    return rec.id


def relevant_corrections(
    user_input: str,
    *,
    person_id: str | None = None,
    limit: int = 3,
    days: float = 30.0,
) -> list[dict[str, Any]]:
    """Find prior corrections topically relevant to this input.

    Returns up to `limit` records sorted by recency + keyword overlap.
    """
    if not user_input:
        return []
    cutoff = time.time() - days * 86400
    input_words = set(_extract_topic_keywords(user_input))
    if not input_words:
        return []

    scored: list[tuple[float, CorrectionRecord]] = []
    with _lock:
        for r in _records:
            if r.ts < cutoff:
                continue
            if person_id is not None and r.person_id != person_id:
                continue
            overlap = len(input_words & set(r.topic_keywords))
            if overlap == 0:
                continue
            recency_bonus = 1.0 / max(1.0, (time.time() - r.ts) / 86400.0)
            score = overlap + recency_bonus
            scored.append((score, r))

    scored.sort(key=lambda kv: kv[0], reverse=True)
    out: list[dict[str, Any]] = []
    for _, r in scored[:limit]:
        out.append({
            "id": r.id, "ts": r.ts,
            "user_correction": r.user_correction,
            "prior_assistant_reply": r.prior_assistant_reply,
            "extracted_fact": r.extracted_fact,
            "topic_keywords": r.topic_keywords,
        })
        # Bump surfaced count to track repeat-relevance
        with _lock:
            r.surfaced_count += 1
    return out


def correction_hint(user_input: str, *, person_id: str | None = None) -> str:
    """Build a system-prompt fragment surfacing relevant prior corrections.

    Returns "" when nothing relevant.
    """
    relevant = relevant_corrections(user_input, person_id=person_id, limit=2)
    if not relevant:
        return ""
    lines: list[str] = []
    for r in relevant:
        snippet = r.get("user_correction") or ""
        if snippet:
            lines.append(f"- {snippet[:160]}")
    if not lines:
        return ""
    return "PRIOR CORRECTIONS RELEVANT TO THIS TOPIC — don't repeat the mistakes:\n" + "\n".join(lines)


def auto_capture_from_turn(
    g: dict[str, Any],
    person_id: str,
    user_input: str,
) -> str | None:
    """Run detection on a turn and capture if it's a correction.

    Caller passes the global state dict so we can pull the prior
    assistant reply from canonical history.
    """
    prior_reply = ""
    try:
        import avaagent as _av
        canon = _av._get_canonical_history()
        if canon:
            for entry in reversed(canon):
                if isinstance(entry, dict) and entry.get("role") == "assistant":
                    prior_reply = str(entry.get("content") or "")
                    break
    except Exception:
        pass

    if not detect_correction(user_input, prior_reply):
        return None
    return capture_correction(person_id, user_input, prior_reply)


def corrections_summary() -> dict[str, Any]:
    with _lock:
        records = list(_records)
    by_person: dict[str, int] = {}
    last_7d = 0
    cutoff = time.time() - 7 * 86400
    for r in records:
        by_person[r.person_id] = by_person.get(r.person_id, 0) + 1
        if r.ts >= cutoff:
            last_7d += 1
    return {
        "total": len(records),
        "last_7d": last_7d,
        "by_person": by_person,
    }
