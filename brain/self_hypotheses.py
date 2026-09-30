"""self_hypotheses — the self-model fed back into cognition as COMPETING HYPOTHESES, not identity
(Iris_fixes #9 + addition C, 2026-09-30).

Zeke (Iris_fixes.docx): "Feed relevant self-model information back as advisory context, not
immutable identity. Preserve uncertainty and contradictory evidence so a temporary behavior
doesn't become a permanent self-fulfilling trait. Keep approved identity extensions stronger
than automatically inferred self-observations."  Addition C: "Let Iris represent uncertainty
about her own traits instead of forcing every observation into a declaration. 'I may naturally
be reserved, but I may also simply speak less when Zeke is occupied. Current evidence doesn't
distinguish them strongly.' Track confidence and counterevidence over time. Strong, repeated
evidence can gradually stabilize a self-understanding."

What the audit found: brain/self_model.py keeps state/self_model.json (traits: {} — its weekly
LLM review has failed four times in a row and written "deferred" notes; the one real entry was
written by avaagent's reflection path on 09-24) and NOTHING in the Iris process reads it. The
inner monologue prompt carries IDENTITY.md (the approved identity) and nothing about what I have
observed of myself. So self-observations were both unconsumed and, where they existed, phrased
as verdicts.

The model here:
  * a TRAIT is a QUESTION with ≥ 2 rival HYPOTHESES. One hypothesis alone is refused — "a
    self-observation is not a trait until it has a rival".
  * an OBSERVATION says which hypotheses it supports. If it supports ALL of them it is kept but
    NON-DISCRIMINATING (it moves nothing). Only evidence that separates rivals counts.
  * a hypothesis STABILISES only when it has ≥ STABILISE_MIN_DISCRIMINATING discriminating
    observations, ≥ STABILISE_RATIO × the runner-up's, spanning ≥ STABILISE_MIN_DAYS — and it
    DESTABILISES again if the runner-up gathers DESTABILISE_MIN discriminating observations
    within DESTABILISE_WINDOW_DAYS. Stabilised is still advisory.
  * advisory() renders this for prompts and SAYS it is outranked by IDENTITY.md / Zeke's
    approved framing. Fail-open: any error → '' (the prompt just lacks the block).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Optional

STABILISE_MIN_DISCRIMINATING = 5
STABILISE_RATIO = 2.0
STABILISE_MIN_DAYS = 14.0
DESTABILISE_MIN = 3
DESTABILISE_WINDOW_DAYS = 30.0
MAX_OBS_PER_TRAIT = 200


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _base_dir() -> str:
    return os.environ.get("SELF_HYPOTHESES_BASE", "").strip() or _repo_root()


def store_path() -> str:
    return os.path.join(_base_dir(), "state", "self_model", "hypotheses.json")


def events_path() -> str:
    return os.path.join(_base_dir(), "state", "self_model", "events.jsonl")


def _load() -> tuple[dict[str, Any], Optional[str]]:
    p = store_path()
    if not os.path.exists(p):
        return {"traits": {}, "updated_at": 0.0}, None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("traits"), dict):
            return {"traits": {}, "updated_at": 0.0}, "store has the wrong shape"
        return d, None
    except Exception as e:
        return {"traits": {}, "updated_at": 0.0}, "store unreadable: %r" % (e,)


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


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")[:60]


# ---------------------------------------------------------------- write side
def propose(trait: str, question: str, hypotheses: dict[str, str], *, source_ref: str = "",
            now: Optional[float] = None) -> dict[str, Any]:
    """Define (or extend) a trait QUESTION with ≥ 2 rival hypotheses."""
    tk = _key(trait)
    hyps = {_key(k): str(v).strip() for k, v in (hypotheses or {}).items() if _key(k) and str(v).strip()}
    if not tk or not (question or "").strip():
        return {"ok": False, "error": "trait and question are required"}
    d, err = _load()
    if err:
        return {"ok": False, "error": "refusing to write over a corrupt store: " + err}
    t = float(now) if now is not None else time.time()
    tr = d["traits"].get(tk)
    if tr is None:
        if len(hyps) < 2:
            return {"ok": False, "error": "a trait needs at least two rival hypotheses — a single "
                                          "self-observation is not a trait until it has a rival"}
        tr = {"trait": tk, "question": question.strip()[:300], "hypotheses": {}, "observations": [],
              "stabilised": None, "stabilised_ts": None, "created_ts": t, "source_ref": source_ref[:200]}
        d["traits"][tk] = tr
        action = "created"
    else:
        action = "extended"
    for hk, st in hyps.items():
        if hk not in tr["hypotheses"]:
            tr["hypotheses"][hk] = {"statement": st[:300], "added_ts": t}
    _save(d)
    _event("propose", trait=tk, action=action, hypotheses=list(hyps.keys()))
    return {"ok": True, "action": action, "trait": tr}


def observe(trait: str, text: str, *, supports: list[str], source_ref: str = "",
            now: Optional[float] = None) -> dict[str, Any]:
    """Record a self-observation and which hypotheses it supports. Supporting ALL of a trait's
    hypotheses is allowed but NON-DISCRIMINATING — it is kept and moves nothing."""
    tk = _key(trait)
    d, err = _load()
    if err:
        return {"ok": False, "error": "refusing to write over a corrupt store: " + err}
    tr = d["traits"].get(tk)
    if tr is None:
        return {"ok": False, "error": "unknown trait %r — propose() it with rival hypotheses first" % trait}
    sup = [_key(s) for s in (supports or []) if _key(s) in tr["hypotheses"]]
    if not sup:
        return {"ok": False, "error": "supports must name at least one of: %s" % sorted(tr["hypotheses"].keys())}
    if not (text or "").strip():
        return {"ok": False, "error": "text is required"}
    t = float(now) if now is not None else time.time()
    discriminates = len(set(sup)) < len(tr["hypotheses"])
    ob = {"id": uuid.uuid4().hex[:8], "ts": t, "text": text.strip()[:400], "supports": sorted(set(sup)),
          "discriminates": discriminates, "source_ref": source_ref[:200]}
    tr["observations"].append(ob)
    tr["observations"] = tr["observations"][-MAX_OBS_PER_TRAIT:]
    before = tr.get("stabilised")
    _reassess(tr, t)
    _save(d)
    _event("observe", trait=tk, obs=ob["id"], supports=ob["supports"], discriminates=discriminates,
           stabilised_before=before, stabilised_after=tr.get("stabilised"))
    return {"ok": True, "observation": ob, "discriminates": discriminates, "status": _status(tr, t)}


def _counts(tr: dict[str, Any], now: float, *, since_days: Optional[float] = None) -> dict[str, dict[str, Any]]:
    out = {hk: {"support": 0, "discriminating": 0, "first_disc_ts": None, "last_disc_ts": None}
           for hk in tr["hypotheses"]}
    for ob in tr["observations"]:
        ts = float(ob.get("ts") or 0.0)
        if since_days is not None and (now - ts) > since_days * 86400.0:
            continue
        for hk in ob.get("supports", []):
            if hk not in out:
                continue
            out[hk]["support"] += 1
            if ob.get("discriminates"):
                out[hk]["discriminating"] += 1
                if out[hk]["first_disc_ts"] is None or ts < out[hk]["first_disc_ts"]:
                    out[hk]["first_disc_ts"] = ts
                if out[hk]["last_disc_ts"] is None or ts > out[hk]["last_disc_ts"]:
                    out[hk]["last_disc_ts"] = ts
    return out


def _reassess(tr: dict[str, Any], now: float) -> None:
    """Apply the stabilise / destabilise rules. Advisory either way."""
    c = _counts(tr, now)
    ranked = sorted(c.items(), key=lambda kv: -kv[1]["discriminating"])
    if not ranked:
        return
    lead, lead_c = ranked[0]
    runner_c = ranked[1][1]["discriminating"] if len(ranked) > 1 else 0
    if tr.get("stabilised"):
        # counter-evidence: any rival gathering DESTABILISE_MIN discriminating obs in the window
        recent = _counts(tr, now, since_days=DESTABILISE_WINDOW_DAYS)
        for hk, rc in recent.items():
            if hk != tr["stabilised"] and rc["discriminating"] >= DESTABILISE_MIN:
                _event("destabilise", trait=tr["trait"], was=tr["stabilised"], rival=hk,
                       rival_recent_discriminating=rc["discriminating"])
                tr["stabilised"] = None
                tr["stabilised_ts"] = None
                tr["destabilised_ts"] = now
                break
        if tr.get("stabilised"):
            return
    # cooling: a trait that was just destabilised must earn it again over a fresh window,
    # otherwise the old lead re-stabilises in the very same call (caught by the tests)
    dts = tr.get("destabilised_ts")
    if dts is not None and (now - float(dts)) < DESTABILISE_WINDOW_DAYS * 86400.0:
        return
    span_days = 0.0
    if lead_c["first_disc_ts"] is not None and lead_c["last_disc_ts"] is not None:
        span_days = (lead_c["last_disc_ts"] - lead_c["first_disc_ts"]) / 86400.0
    if (lead_c["discriminating"] >= STABILISE_MIN_DISCRIMINATING
            and lead_c["discriminating"] >= STABILISE_RATIO * max(1, runner_c)
            and span_days >= STABILISE_MIN_DAYS):
        tr["stabilised"] = lead
        tr["stabilised_ts"] = now
        _event("stabilise", trait=tr["trait"], hypothesis=lead, discriminating=lead_c["discriminating"],
               runner_up=runner_c, span_days=round(span_days, 1))


# ---------------------------------------------------------------- read side
def _status(tr: dict[str, Any], now: float) -> dict[str, Any]:
    c = _counts(tr, now)
    ranked = sorted(c.items(), key=lambda kv: -kv[1]["discriminating"])
    total_disc = sum(v["discriminating"] for v in c.values())
    lead, lead_c = (ranked[0] if ranked else (None, {"discriminating": 0, "first_disc_ts": None, "last_disc_ts": None}))
    runner_c = ranked[1][1]["discriminating"] if len(ranked) > 1 else 0
    if tr.get("stabilised"):
        verdict = "stabilised: " + tr["stabilised"]
    elif total_disc == 0:
        verdict = "undistinguished — no discriminating evidence yet"
    elif lead_c["discriminating"] >= STABILISE_RATIO * max(1, runner_c) and lead_c["discriminating"] >= 2:
        need = max(0, STABILISE_MIN_DISCRIMINATING - lead_c["discriminating"])
        span = 0.0
        if lead_c["first_disc_ts"] is not None and lead_c["last_disc_ts"] is not None:
            span = (lead_c["last_disc_ts"] - lead_c["first_disc_ts"]) / 86400.0
        verdict = "leaning %s (%d discriminating vs %d; needs %d more over ≥%d days, span so far %.0f d)" % (
            lead, lead_c["discriminating"], runner_c, need, int(STABILISE_MIN_DAYS), span)
    else:
        verdict = "undistinguished — evidence doesn't separate the rivals (%s)" % ", ".join(
            "%s %d" % (hk, v["discriminating"]) for hk, v in ranked)
    return {"trait": tr["trait"], "question": tr["question"], "verdict": verdict,
            "hypotheses": {hk: {"statement": tr["hypotheses"][hk]["statement"], **v} for hk, v in c.items()},
            "observations": len(tr["observations"]), "non_discriminating": sum(1 for o in tr["observations"] if not o.get("discriminates")),
            "stabilised": tr.get("stabilised")}


def status(trait: Optional[str] = None, *, now: Optional[float] = None) -> dict[str, Any]:
    t = float(now) if now is not None else time.time()
    d, err = _load()
    if err:
        return {"ok": False, "error": err, "traits": []}
    if trait:
        tr = d["traits"].get(_key(trait))
        if tr is None:
            return {"ok": False, "error": "unknown trait %r" % trait, "traits": []}
        return {"ok": True, "traits": [_status(tr, t)]}
    return {"ok": True, "traits": [_status(tr, t) for tr in d["traits"].values()]}


def advisory(*, max_traits: int = 3, now: Optional[float] = None) -> str:
    """The block that reaches cognition. Advisory by construction; says so; fail-open → ''."""
    try:
        t = float(now) if now is not None else time.time()
        d, err = _load()
        if err:
            return ""
        lines: list[str] = []
        traits = sorted(d["traits"].values(), key=lambda tr: -len(tr.get("observations", [])))[:max(1, int(max_traits))]
        for tr in traits:
            s = _status(tr, t)
            if s["stabilised"]:
                hk = s["stabilised"]
                lines.append("  - %s → stabilised over repeated evidence: %s (still advisory)" % (
                    tr["question"], tr["hypotheses"][hk]["statement"]))
            else:
                alts = " / ".join("\"%s\" (%d)" % (h["statement"], h["discriminating"]) for h in s["hypotheses"].values())
                lines.append("  - %s — may be %s; %s." % (tr["question"], alts,
                             "evidence doesn't distinguish them yet" if "undistinguished" in s["verdict"] else s["verdict"]))
        if not lines:
            return ""
        review = _last_self_review()
        if review:
            lines.append("  - last self-review (%s): %s" % review)
        return ("SELF-MODEL (advisory, NOT identity — IDENTITY.md and Zeke's approved framing outrank this; "
                "these are hypotheses about how I behave, held open on purpose):\n" + "\n".join(lines))
    except Exception:
        return ""


def _last_self_review() -> Optional[tuple[str, str]]:
    """The newest narrative line from state/self_model.json (its writer is avaagent's reflection
    path; on this host it is rare) — shown as context, never as a trait."""
    try:
        p = os.path.join(_base_dir(), "state", "self_model.json")
        with open(p, "r", encoding="utf-8") as f:
            m = json.load(f)
        note = str(m.get("growth_note") or "").strip()
        when = str(m.get("last_updated") or "")[:10]
        if note:
            return when, note[:220]
    except Exception:
        pass
    return None


def explain() -> dict[str, Any]:
    return {
        "model": "trait = a QUESTION with ≥2 rival hypotheses; observations support some of them; only "
                 "discriminating observations count; stabilise after ≥%d discriminating, ≥%.0fx the runner-up, "
                 "spanning ≥%d days; destabilise if a rival gets %d discriminating within %d days"
                 % (STABILISE_MIN_DISCRIMINATING, STABILISE_RATIO, STABILISE_MIN_DAYS, DESTABILISE_MIN, DESTABILISE_WINDOW_DAYS),
        "precedence": "IDENTITY.md / Zeke's approved framing > stabilised hypothesis > leaning > undistinguished",
        "consumers": ["iris_inner_monologue reflection prompt (SELF-MODEL block)", "iris_ambient_snapshot [self] line",
                      "self_model tool"],
        "store": store_path(), "events": events_path(),
    }
