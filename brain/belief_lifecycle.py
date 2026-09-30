"""belief_lifecycle — ONE lifecycle for evidence → confidence → belief → new evidence →
reconsideration → revision history (Iris_fixes #8 + addition B, 2026-09-30).

Zeke (Iris_fixes.docx): "Iris has opinions, confidence, provenance/evidence tracking,
disagreement handling and self-revision mechanisms. They don't yet appear to form one clean
lifecycle. Connect them so new evidence can challenge an existing opinion rather than an old
opinion simply remaining frozen. Preserve why an opinion changed and what evidence caused the
revision. Remove or tighten generic fallback opinions."  Addition B: "Avoid automatically
changing beliefs from one contradictory observation ... keep previous belief, confidence,
conflicting evidence, revised belief and reason ... unify several systems Iris already has."

What the audit found: brain/opinions.py, provenance.py, confidence.py, honest_disagreement.py
and self_revision.py all exist and NONE has ever written a byte on this machine — no callers in
the Iris process, no state files. This module does not replace them; it is the lifecycle that
USES them: confidence.py's source-kind defaults seed confidence, provenance.py records every
source, self_revision.py keeps the "I changed my mind" log, and opinions/honest_disagreement
are pointed at this store instead of at files that never existed.

Beliefs are about the world, a person, or myself (kind). The rules that keep this honest:
  * ONE contradiction never revises. It lowers confidence and is kept as evidence_against.
    Revision needs MIN_INDEPENDENT_CONTRA independent contradictions (different source_ref or
    source_kind, spaced ≥ MIN_CONTRA_GAP_S) → status under_review → an explicit revise().
  * The DOMAIN SPLIT (Zeke 08-30, memory CORE): about HIS life/body/self, HIS word wins — a
    challenge marked decisive=True with a proposed statement revises immediately, with the
    reason recorded. Technical domains: my judgment carries → never decisive by default.
  * Every revision keeps previous statement + previous confidence + the challenges that caused
    it + the reason. Nothing is silently overwritten.
  * Confidence decays with age since last confirmation (half-life HALF_LIFE_DAYS) on READ;
    reconsider() lists what is under review or stale so cognition looks again.
  * Corrupt store → reads return [] with an error flag and writes REFUSE (never overwrite).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Optional

MIN_INDEPENDENT_CONTRA = 2
MIN_CONTRA_GAP_S = 600.0
CHALLENGE_PENALTY = 0.15
CONFIRM_GAIN = 0.2
HALF_LIFE_DAYS = 180.0
STALE_DAYS = 90.0
MAX_BELIEFS = 500
KINDS = ("world", "person", "self", "opinion")
STATUSES = ("active", "under_review", "revised", "retired")

# confidence.py's levels, made numeric (same thresholds as its infer_from_provenance)
_LEVEL_NUM = {"high": 0.85, "medium-high": 0.7, "medium": 0.55, "medium-low": 0.4, "low": 0.25}


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _base_dir() -> str:
    return os.environ.get("BELIEF_STORE_BASE", "").strip() or _repo_root()


def store_path() -> str:
    return os.path.join(_base_dir(), "state", "beliefs", "beliefs.json")


def events_path() -> str:
    return os.path.join(_base_dir(), "state", "beliefs", "events.jsonl")


def default_confidence(source_kind: str) -> float:
    try:
        from brain import confidence as _c
        return _LEVEL_NUM.get(_c.source_kind_to_default_confidence(source_kind), 0.55)
    except Exception:
        return {"user_told": 0.85, "chat": 0.85, "web": 0.85, "observation": 0.7,
                "email": 0.7, "training": 0.55, "memory": 0.55, "skill": 0.55,
                "derived": 0.4}.get(source_kind, 0.55)


# ---------------------------------------------------------------- the sibling systems
def _ensure_siblings() -> None:
    """Configure provenance + self_revision if the Iris bootstrap never did (it never did)."""
    base = _base_dir()
    try:
        from pathlib import Path
        from brain import provenance as _p
        if getattr(_p.provenance, "_base_dir", None) is None:
            _p.configure_provenance(Path(base))
    except Exception:
        pass
    try:
        from pathlib import Path
        from brain import self_revision as _sr
        if getattr(_sr, "_base_dir", None) is None:
            _sr.configure(Path(base))
    except Exception:
        pass


def _prov(claim: str, source_kind: str, source_ref: str, conf: float, ctx: dict[str, Any]) -> str:
    try:
        _ensure_siblings()
        from brain import provenance as _p
        return _p.provenance.record_claim(claim, source_kind, source_ref=source_ref,
                                          confidence=conf, context=ctx)
    except Exception:
        return ""


def _log_revision(subject: str, previous: str, new: str, reason: str) -> str:
    try:
        _ensure_siblings()
        from brain import self_revision as _sr
        return _sr.record_revision(topic=subject, previous=previous, new=new, reason=reason, marked_by="iris")
    except Exception:
        return ""


# ---------------------------------------------------------------- store
def _load() -> tuple[dict[str, Any], Optional[str]]:
    p = store_path()
    if not os.path.exists(p):
        return {"beliefs": {}, "updated_at": 0.0}, None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("beliefs"), dict):
            return {"beliefs": {}, "updated_at": 0.0}, "store has the wrong shape"
        return d, None
    except Exception as e:
        return {"beliefs": {}, "updated_at": 0.0}, "store unreadable: %r" % (e,)


def _save(d: dict[str, Any]) -> None:
    p = store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d["updated_at"] = time.time()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


def _event(event_name: str, **fields: Any) -> None:
    try:
        p = events_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        row = {"ts": time.time(), "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event_name}
        row.update(fields)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", _norm(s)))


def _find(d: dict[str, Any], subject: str) -> Optional[dict[str, Any]]:
    n = _norm(subject)
    for b in d["beliefs"].values():
        if b.get("status") in ("active", "under_review") and _norm(b.get("subject", "")) == n:
            return b
    return None


def effective_confidence(b: dict[str, Any], now: Optional[float] = None) -> float:
    t = float(now) if now is not None else time.time()
    conf = float(b.get("confidence") or 0.0)
    last = float(b.get("last_confirmed_ts") or b.get("formed_ts") or t)
    age_days = max(0.0, (t - last) / 86400.0)
    return round(max(0.0, min(1.0, conf * (0.5 ** (age_days / HALF_LIFE_DAYS)))), 3)


# ---------------------------------------------------------------- lifecycle
def hold(subject: str, statement: str, *, kind: str = "world", source_kind: str = "observation",
         source_ref: str = "", confidence: Optional[float] = None, note: str = "",
         now: Optional[float] = None) -> dict[str, Any]:
    """Assert a belief. New subject → created with the source's default confidence.
    Existing subject with the same statement → CONFIRMED (confidence up, bounded).
    Existing subject with a DIFFERENT statement → treated as a challenge (see challenge())."""
    if not (subject or "").strip() or not (statement or "").strip():
        return {"ok": False, "error": "subject and statement are required"}
    if kind not in KINDS:
        return {"ok": False, "error": "kind must be one of %s" % (KINDS,)}
    t = float(now) if now is not None else time.time()
    d, err = _load()
    if err:
        return {"ok": False, "error": "refusing to write over a corrupt store: " + err}
    b = _find(d, subject)
    conf = float(confidence) if confidence is not None else default_confidence(source_kind)
    conf = max(0.0, min(1.0, conf))
    pid = _prov(statement, source_kind, source_ref, conf, {"subject": subject, "kind": kind, "note": note})
    if b is None:
        if len(d["beliefs"]) >= MAX_BELIEFS:
            return {"ok": False, "error": "belief store full (%d)" % MAX_BELIEFS}
        bid = uuid.uuid4().hex[:10]
        b = {"id": bid, "subject": subject.strip(), "statement": statement.strip(), "kind": kind,
             "confidence": conf, "status": "active", "formed_ts": t, "last_confirmed_ts": t,
             "evidence_for": [{"ts": t, "source_kind": source_kind, "source_ref": source_ref,
                               "note": note[:200], "prov": pid}],
             "evidence_against": [], "history": []}
        d["beliefs"][bid] = b
        _save(d)
        _event("hold", id=bid, subject=subject, statement=statement, kind=kind, source_kind=source_kind, confidence=conf)
        return {"ok": True, "action": "created", "belief": b}
    if _norm(b["statement"]) == _norm(statement):
        old = float(b["confidence"])
        b["confidence"] = round(min(1.0, old + (1.0 - old) * CONFIRM_GAIN), 3)
        b["last_confirmed_ts"] = t
        b["evidence_for"].append({"ts": t, "source_kind": source_kind, "source_ref": source_ref,
                                  "note": note[:200], "prov": pid})
        b["evidence_for"] = b["evidence_for"][-50:]
        _save(d)
        _event("confirm", id=b["id"], subject=subject, old=old, new=b["confidence"], source_kind=source_kind)
        return {"ok": True, "action": "confirmed", "belief": b}
    return challenge(subject, statement, source_kind=source_kind, source_ref=source_ref,
                     note=note or "hold() with a different statement", proposed=statement, now=t)


def challenge(subject: str, evidence: str, *, source_kind: str = "observation", source_ref: str = "",
              note: str = "", proposed: Optional[str] = None, decisive: bool = False,
              now: Optional[float] = None) -> dict[str, Any]:
    """Record evidence AGAINST the active belief on `subject`.
    One challenge lowers confidence and is kept. Independent challenges ≥ MIN_INDEPENDENT_CONTRA
    put the belief under_review. decisive=True (Zeke's word about HIS OWN life/body/self — the
    08-30 domain split) revises immediately when `proposed` is given."""
    t = float(now) if now is not None else time.time()
    d, err = _load()
    if err:
        return {"ok": False, "error": "refusing to write over a corrupt store: " + err}
    b = _find(d, subject)
    if b is None:
        return {"ok": False, "error": "no active belief on subject %r (hold() it first)" % subject}
    pid = _prov(evidence, source_kind, source_ref, default_confidence(source_kind),
                {"subject": subject, "against": b["id"], "note": note})
    ch = {"id": uuid.uuid4().hex[:8], "ts": t, "evidence": evidence.strip()[:400], "source_kind": source_kind,
          "source_ref": source_ref, "note": note[:200], "proposed": (proposed or "").strip()[:400],
          "decisive": bool(decisive), "prov": pid}
    b["evidence_against"].append(ch)
    b["evidence_against"] = b["evidence_against"][-50:]
    old_conf = float(b["confidence"])
    b["confidence"] = round(max(0.05, old_conf - CHALLENGE_PENALTY), 3)
    independent = _independent_challenges(b["evidence_against"])
    outcome = "noted"
    if decisive and proposed:
        _save(d)
        _event("challenge", id=b["id"], subject=subject, challenge=ch["id"], decisive=True)
        r = revise(subject, proposed, reason=note or "decisive: Zeke's word on his own domain",
                   source_kind=source_kind, source_ref=source_ref, evidence_ids=[ch["id"]], now=t)
        r["action"] = "revised_decisive"
        return r
    if independent >= MIN_INDEPENDENT_CONTRA and b["status"] == "active":
        b["status"] = "under_review"
        outcome = "under_review"
    elif decisive:
        b["status"] = "under_review"
        outcome = "under_review (decisive but no proposed statement)"
    _save(d)
    _event("challenge", id=b["id"], subject=subject, challenge=ch["id"], independent=independent,
           old=old_conf, new=b["confidence"], outcome=outcome)
    return {"ok": True, "action": outcome, "independent_challenges": independent, "belief": b, "challenge": ch}


def _independent_challenges(chs: list[dict[str, Any]]) -> int:
    """Count challenges that are independent of each other: different (source_kind, source_ref)
    or the same source repeated ≥ MIN_CONTRA_GAP_S apart."""
    seen: dict[tuple[str, str], float] = {}
    n = 0
    for c in sorted(chs, key=lambda x: float(x.get("ts") or 0.0)):
        key = (str(c.get("source_kind") or ""), str(c.get("source_ref") or ""))
        ts = float(c.get("ts") or 0.0)
        last = seen.get(key)
        if last is None or (ts - last) >= MIN_CONTRA_GAP_S:
            n += 1
            seen[key] = ts
    return n


def revise(subject: str, new_statement: str, *, reason: str, source_kind: str = "observation",
           source_ref: str = "", evidence_ids: Optional[list[str]] = None,
           confidence: Optional[float] = None, now: Optional[float] = None) -> dict[str, Any]:
    """Replace the statement, keeping previous + confidence + the challenges + reason in history,
    and log it to self_revision (the 'I changed my mind' record)."""
    if not (new_statement or "").strip() or not (reason or "").strip():
        return {"ok": False, "error": "new_statement and reason are required"}
    t = float(now) if now is not None else time.time()
    d, err = _load()
    if err:
        return {"ok": False, "error": "refusing to write over a corrupt store: " + err}
    b = _find(d, subject)
    if b is None:
        return {"ok": False, "error": "no active belief on subject %r" % subject}
    prev, prev_conf = b["statement"], float(b["confidence"])
    ev_ids = list(evidence_ids or [c["id"] for c in b["evidence_against"]])
    entry = {"ts": t, "previous": prev, "previous_confidence": prev_conf, "new": new_statement.strip(),
             "reason": reason.strip()[:400], "evidence": ev_ids,
             "challenges": [c for c in b["evidence_against"] if c["id"] in ev_ids][-10:]}
    rid = _log_revision(subject, prev, new_statement, reason)
    entry["self_revision_id"] = rid
    b["history"].append(entry)
    b["history"] = b["history"][-20:]
    b["statement"] = new_statement.strip()
    b["confidence"] = max(0.0, min(1.0, float(confidence) if confidence is not None else default_confidence(source_kind)))
    b["status"] = "active"
    b["last_confirmed_ts"] = t
    b["evidence_for"] = [{"ts": t, "source_kind": source_kind, "source_ref": source_ref,
                          "note": "revision: " + reason[:150],
                          "prov": _prov(new_statement, source_kind, source_ref, b["confidence"], {"subject": subject, "revision": True})}]
    b["evidence_against"] = []
    _save(d)
    _event("revise", id=b["id"], subject=subject, previous=prev, new=new_statement, reason=reason, self_revision_id=rid)
    return {"ok": True, "action": "revised", "belief": b, "revision": entry}


def retire(subject: str, *, reason: str, now: Optional[float] = None) -> dict[str, Any]:
    t = float(now) if now is not None else time.time()
    d, err = _load()
    if err:
        return {"ok": False, "error": "refusing to write over a corrupt store: " + err}
    b = _find(d, subject)
    if b is None:
        return {"ok": False, "error": "no active belief on subject %r" % subject}
    b["status"] = "retired"
    b["history"].append({"ts": t, "previous": b["statement"], "previous_confidence": b["confidence"],
                         "new": "", "reason": "retired: " + reason[:300], "evidence": []})
    _save(d)
    _event("retire", id=b["id"], subject=subject, reason=reason)
    return {"ok": True, "action": "retired", "belief": b}


# ---------------------------------------------------------------- reads
def _view(b: dict[str, Any], now: float) -> dict[str, Any]:
    return {"id": b["id"], "subject": b["subject"], "statement": b["statement"], "kind": b["kind"],
            "status": b["status"], "confidence": float(b["confidence"]),
            "effective_confidence": effective_confidence(b, now),
            "sources": sorted({e.get("source_kind", "") for e in b.get("evidence_for", [])}),
            "confirmations": len(b.get("evidence_for", [])), "challenges": len(b.get("evidence_against", [])),
            "independent_challenges": _independent_challenges(b.get("evidence_against", [])),
            "last_confirmed_iso": time.strftime("%Y-%m-%d", time.localtime(float(b.get("last_confirmed_ts") or 0))),
            "revisions": len(b.get("history", [])),
            "stale": ((now - float(b.get("last_confirmed_ts") or now)) / 86400.0) > STALE_DAYS}


def recall(query: str = "", *, kind: Optional[str] = None, include_retired: bool = False,
           limit: int = 10, now: Optional[float] = None) -> dict[str, Any]:
    """What do I currently believe about `query`? Subject match, substring, or token overlap.
    Flags under_review + stale so the reader knows to look again."""
    t = float(now) if now is not None else time.time()
    d, err = _load()
    q = _norm(query)
    qt = _tokens(query)
    scored = []
    for b in d["beliefs"].values():
        if not include_retired and b.get("status") == "retired":
            continue
        if kind and b.get("kind") != kind:
            continue
        s = _norm(b.get("subject", ""))
        if not q:
            score = 0.5
        elif s == q:
            score = 1.0
        elif min(len(q), len(s)) >= 4 and (q in s or s in q):
            score = 0.8            # substring only when the shorter side is a real word
        else:
            bt = _tokens(b.get("subject", "") + " " + b.get("statement", ""))
            score = (len(qt & bt) / float(len(qt | bt))) if (qt and bt) else 0.0
            if score < 0.2:
                continue
        scored.append((score, b))
    scored.sort(key=lambda x: (-x[0], -float(x[1].get("confidence") or 0.0)))
    out = {"ok": err is None, "beliefs": [_view(b, t) for _, b in scored[:max(1, int(limit))]],
           "total": len(d["beliefs"])}
    if err:
        out["error"] = err
    return out


def reconsider(*, now: Optional[float] = None) -> dict[str, Any]:
    """The reconsideration sweep: what is under review, what is stale, what a single
    contradiction is sitting on. A readout for cognition — it changes nothing by itself."""
    t = float(now) if now is not None else time.time()
    d, err = _load()
    under, stale, contested = [], [], []
    for b in d["beliefs"].values():
        if b.get("status") == "under_review":
            under.append(_view(b, t))
        elif b.get("status") == "active":
            v = _view(b, t)
            if v["stale"]:
                stale.append(v)
            elif v["challenges"] > 0:
                contested.append(v)
    out = {"ok": err is None, "under_review": under, "stale": stale, "contested_but_active": contested,
           "counts": {"total": len(d["beliefs"]),
                      "by_status": {s: sum(1 for b in d["beliefs"].values() if b.get("status") == s) for s in STATUSES}}}
    if err:
        out["error"] = err
    return out


def history(subject: str) -> dict[str, Any]:
    d, err = _load()
    n = _norm(subject)
    rows = [b for b in d["beliefs"].values() if _norm(b.get("subject", "")) == n]
    return {"ok": err is None, "beliefs": rows, "error": err} if err else {"ok": True, "beliefs": rows}


def contradictions_for(text: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Beliefs topically connected to `text` — what honest_disagreement's Track A should compare
    a strong claim against (it used to read a file with a shape nobody ever wrote)."""
    r = recall(text, now=now, limit=5)
    return [b for b in r.get("beliefs", []) if b["effective_confidence"] >= 0.5]


def explain() -> dict[str, Any]:
    return {
        "lifecycle": "hold (evidence → confidence from source kind) → confirm/challenge → under_review "
                     "after %d independent contradictions → revise (previous+new+reason+evidence kept; "
                     "logged to self_revision) or retire; recall/reconsider are the consumers" % MIN_INDEPENDENT_CONTRA,
        "rules": {"one_contradiction_never_revises": True, "challenge_penalty": CHALLENGE_PENALTY,
                  "confirm_gain": CONFIRM_GAIN, "min_independent_contra": MIN_INDEPENDENT_CONTRA,
                  "min_contra_gap_s": MIN_CONTRA_GAP_S, "half_life_days": HALF_LIFE_DAYS, "stale_days": STALE_DAYS,
                  "decisive": "Zeke's word about HIS OWN life/body/self revises at once (08-30 domain split); "
                              "technical/world claims never auto-revise"},
        "uses": {"confidence.py": "source-kind → starting confidence", "provenance.py": "every source recorded",
                 "self_revision.py": "every revision logged", "opinions.py": "opinions register here as kind=opinion",
                 "honest_disagreement.py": "Track A reads contradictions_for()"},
        "store": store_path(), "events": events_path(),
    }
