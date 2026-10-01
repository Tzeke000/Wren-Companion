"""adaptive_iris — close the adaptive-learning loop on the IRIS path (Iris_fixes #7).

Zeke (Iris_fixes.docx): "Iris already learns values such as conversation pacing, curiosity
usefulness, memory usefulness, proactive-trigger usefulness, and when silence is preferable.
Those values are persisted, but I couldn't find consistent consumers ... Goal: experience →
learning → stored adjustment → changed future behavior."

What the audit found: brain/adaptive_learning.py keeps those weights in
state/learning/adaptive_preferences.json, but in the Iris process NEITHER its writer
(perception_pipeline — Ava-only) NOR its reader (perception_state_adapter — Ava-only) ever
runs. So on this machine nothing was learned and nothing was consumed. This module is the
Iris-path writer + the consumer helpers, reusing adaptive_learning's schema so the file stays
compatible (weights in [0,1], 0.5 = neutral, EWMA-bounded, never touches ava_core).

EVIDENCE → WEIGHT (each is honest about what it can and can't see):
  proactive_trigger_usefulness  ← state/wake_verdicts.jsonl (Iris_fixes #5): acted / (acted+nothing)
  silence_when_better           ← the same ledger, inverted: nothing / (acted+nothing)
  conversation_pacing           ← state/transcript.jsonl: voice replies that Zeke CUT with a
                                  barge-in ("heard N of M sentences", N<M) vs all voice replies.
                                  High = my pacing is fine; low = I run long.
  interruption_yield_habits     ← same source: barge-ins that landed mid-sentence and were
                                  honoured (the reply stopped) — currently == pacing evidence
  memory_usefulness             ← state/iris_memory_meta.json: share of memories that were
                                  retrieved again after first access (access_count >= 2)
  curiosity_usefulness          ← state/learning_log.jsonl vs state/leisure_log.jsonl: curiosity
                                  browses that produced a REAL learning record (not the "I
                                  couldn't research X" placeholder)
The other four focus areas (repair proposals, social continuity, response style, comfort)
have no honest evidence source in this process yet → left neutral, evidence 0, and SAID SO.

CONSUMERS (the part that was missing):
  hints(prefs)          → short advisory strings; rendered by iris_ambient_snapshot ([adaptive])
                          and by the body host's voice/perception prompts
  cooldown_scale(prefs) → multiplier for question_engine's between-questions cooldown
                          (proactive usefulness 0.5 → ×1.0; 0.25 → ×2.0; 1.0 → ×0.5)
  rerank_memories(rows) → memory_search results re-ordered by distance blended with each
                          memory's effective_importance, blend strength from memory_usefulness
All fail-open: any error → neutral (no change in behaviour).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

ALPHA = 0.2                      # EWMA step per learn() run
MIN_EVIDENCE = 5                 # fewer observations than this → no update for that focus
LOOKBACK_S = 30 * 86400.0        # evidence window
VOICE_TURNS_LOOKBACK = 200       # transcript rows (voice) considered for pacing
# The daemon's real marker (measured from state/transcript.jsonl, 2026-09-30):
#   "[barge-in] [cut mid-sentence at: \"...\" | 2 sentence(s) fully heard, 3 never played]"
# The two non-cut variants are "all N sentence(s) heard" and "nothing had been spoken".
_CUT_RE = re.compile(r"(\d+)\s+sentence\(s\)\s+fully heard,\s+(\d+)\s+never played", re.I)
_CUT_RE_ALT = re.compile(r"heard\s+(\d+)\s+of\s+(\d+)\s+sentence", re.I)   # tolerated older wording

FOCUS = {
    "proactive": "proactive_trigger_usefulness",
    "silence": "silence_when_better_response",
    "pacing": "conversation_pacing",
    "yield": "interruption_yield_habits",
    "memory": "memory_usefulness",
    "curiosity": "curiosity_usefulness",
}


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _state(root: Optional[str], *parts: str) -> str:
    return os.path.join(root or _repo_root(), "state", *parts)


def _read_jsonl(path: str, *, max_bytes: int = 2_000_000) -> list[dict[str, Any]]:
    try:
        st = os.stat(path)
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "rb") as f:
            if st.st_size > max_bytes:
                f.seek(st.st_size - max_bytes)
            blob = f.read().decode("utf-8", errors="replace")
        lines = blob.split("\n")
        if st.st_size > max_bytes:
            lines = lines[1:]
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if isinstance(d, dict):
                rows.append(d)
    except Exception:
        return []
    return rows


# ---------------------------------------------------------------- evidence extractors
def evidence_proactive(root: Optional[str] = None, *, now: Optional[float] = None) -> dict[str, Any]:
    t = float(now) if now is not None else time.time()
    rows = [r for r in _read_jsonl(_state(root, "wake_verdicts.jsonl"))
            if (t - float(r.get("ts") or 0.0)) <= LOOKBACK_S]
    acted = sum(1 for r in rows if r.get("verdict") == "acted")
    nothing = sum(1 for r in rows if r.get("verdict") == "nothing")
    n = acted + nothing
    return {"acted": acted, "nothing": nothing, "n": n,
            "target": (acted / n) if n else None,
            "note": "wake_verdicts.jsonl explicit verdicts (Iris_fixes #5)"}


def evidence_pacing(root: Optional[str] = None) -> dict[str, Any]:
    rows = _read_jsonl(_state(root, "transcript.jsonl"))
    voice = [r for r in rows if str(r.get("modality") or "") == "voice"][-VOICE_TURNS_LOOKBACK:]
    replies = sum(1 for r in voice if r.get("role") == "assistant")
    cut = 0
    for r in voice:
        if r.get("role") != "user":
            continue
        c = str(r.get("content") or "")
        if "[barge-in]" not in c:
            continue
        m = _CUT_RE.search(c)
        if m:
            if int(m.group(2)) > 0:          # something never played = I was cut
                cut += 1
            continue
        m = _CUT_RE_ALT.search(c)
        if m and int(m.group(1)) < int(m.group(2)):
            cut += 1
    return {"replies": replies, "cut": cut, "n": replies,
            "target": (1.0 - min(1.0, (cut / replies) * 2.0)) if replies else None,
            "note": "voice replies Zeke cut mid-way (N never played) — ×2 so a 50% cut rate reads as 0"}


def evidence_memory(root: Optional[str] = None) -> dict[str, Any]:
    p = _state(root, "iris_memory_meta.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    accessed = [m for m in meta.values() if isinstance(m, dict) and int(m.get("access_count") or 0) >= 1]
    again = sum(1 for m in accessed if int(m.get("access_count") or 0) >= 2)
    n = len(accessed)
    return {"accessed": n, "retrieved_again": again, "n": n,
            "target": (again / n) if n else None,
            "note": "memories retrieved more than once (iris_memory_meta access_count>=2)"}


def evidence_curiosity(root: Optional[str] = None, *, now: Optional[float] = None) -> dict[str, Any]:
    t = float(now) if now is not None else time.time()
    leisure = [r for r in _read_jsonl(_state(root, "leisure_log.jsonl"))
               if (t - float(r.get("ts") or 0.0)) <= LOOKBACK_S
               and "curiosity" in str(r.get("activity") or "")]
    learned = [r for r in _read_jsonl(_state(root, "learning_log.jsonl"))
               if (t - float(r.get("ts") or 0.0)) <= LOOKBACK_S]
    real = sum(1 for r in learned
               if not str(r.get("knowledge") or "").lower().startswith("i couldn't research"))
    n = len(leisure)
    return {"browses": n, "real_learnings": real, "placeholder_learnings": len(learned) - real, "n": n,
            "target": min(1.0, real / n) if n else None,
            "note": "curiosity browses that produced a real learning record (placeholders excluded)"}


# ---------------------------------------------------------------- the writer
# ---------------------------------------------------------------- evidence signatures (round-2 fix 2.4)
# learn() used to walk the EWMA toward the SAME target on every run — five runs on identical
# evidence still moved the weights. The signature of the evidence that last moved each focus
# lives in a sidecar (adaptive_learning.load_preferences() drops unknown keys, so it cannot ride
# in the prefs file). Same signature → "no new evidence", weights untouched.
def _sig_path(root: Optional[str]) -> str:
    return _state(root, "learning", "adaptive_evidence_sig.json")


def _load_sigs(root: Optional[str]) -> dict[str, list]:
    try:
        with open(_sig_path(root), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_sigs(root: Optional[str], sigs: dict[str, list]) -> None:
    try:
        p = _sig_path(root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sigs, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        pass


def _evidence_sig(target: float, n: int) -> list:
    return [round(float(target), 4), int(n)]


def learn(root: Optional[str] = None, *, now: Optional[float] = None, dry_run: bool = False) -> dict[str, Any]:
    """Derive targets from evidence and EWMA them into adaptive_preferences.json.
    Returns {updated: {focus: {old, new, target, n}}, skipped: {focus: reason}, prefs}."""
    from brain import adaptive_learning as al
    prefs = al.load_preferences()
    before = dict(prefs["weights"])
    ev = {
        "proactive": evidence_proactive(root, now=now),
        "pacing": evidence_pacing(root),
        "memory": evidence_memory(root),
        "curiosity": evidence_curiosity(root, now=now),
    }
    updated: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    prev_sigs = _load_sigs(root)
    new_sigs: dict[str, list] = dict(prev_sigs)

    def _apply(key: str, target: Optional[float], n: int) -> None:
        focus = FOCUS[key]
        if target is None or n < MIN_EVIDENCE:
            skipped[focus] = "insufficient evidence (%d/%d)" % (n, MIN_EVIDENCE)
            return
        sig = _evidence_sig(target, n)
        if prev_sigs.get(focus) == sig:
            skipped[focus] = "no new evidence (same %d observations, target %.3f) - weights left as they are" % (n, float(target))
            return
        new_sigs[focus] = sig
        old = float(prefs["weights"].get(focus, 0.5))
        new = al._ewma(old, float(target), ALPHA)
        prefs["weights"][focus] = new
        prefs["evidence_counts"][focus] = int(n)
        updated[focus] = {"old": round(old, 3), "new": round(new, 3), "target": round(float(target), 3), "n": n}

    _apply("proactive", ev["proactive"]["target"], ev["proactive"]["n"])
    if ev["proactive"]["target"] is not None:
        _apply("silence", 1.0 - ev["proactive"]["target"], ev["proactive"]["n"])
    else:
        skipped[FOCUS["silence"]] = skipped.get(FOCUS["proactive"], "insufficient evidence")
    _apply("pacing", ev["pacing"]["target"], ev["pacing"]["n"])
    _apply("yield", ev["pacing"]["target"], ev["pacing"]["n"])
    _apply("memory", ev["memory"]["target"], ev["memory"]["n"])
    _apply("curiosity", ev["curiosity"]["target"], ev["curiosity"]["n"])
    for focus in al.ALL_FOCUS:
        if focus not in updated and focus not in skipped:
            skipped[focus] = "no honest evidence source in the Iris process (left neutral)"
    if updated and not dry_run:
        al.save_preferences(prefs)
        _save_sigs(root, new_sigs)
    return {"ok": True, "updated": updated, "skipped": skipped, "evidence": ev, "evidence_sig_path": _sig_path(root),
            "before": before, "prefs": prefs, "dry_run": dry_run, "path": str(al.PREFERENCES_PATH)}


# ---------------------------------------------------------------- consumers
def load() -> dict[str, Any]:
    try:
        from brain import adaptive_learning as al
        return al.load_preferences()
    except Exception:
        return {"weights": {}, "evidence_counts": {}, "updated_at": 0.0}


def _w(prefs: dict[str, Any], key: str) -> tuple[float, int]:
    focus = FOCUS[key]
    try:
        return float(prefs["weights"].get(focus, 0.5)), int(prefs["evidence_counts"].get(focus, 0))
    except Exception:
        return 0.5, 0


def hints(prefs: Optional[dict[str, Any]] = None) -> list[str]:
    """Short advisories for prompts. Only speaks when there is evidence; silence otherwise."""
    p = prefs if prefs is not None else load()
    out: list[str] = []
    try:
        w, n = _w(p, "pacing")
        if n >= MIN_EVIDENCE:
            if w < 0.4:
                out.append("pacing %.2f (Zeke cuts my voice replies often, n=%d) → keep spoken replies to 1–2 sentences" % (w, n))
            elif w > 0.8:
                out.append("pacing %.2f (replies rarely cut, n=%d)" % (w, n))
            else:
                out.append("pacing %.2f (n=%d) → stay short" % (w, n))
        w, n = _w(p, "proactive")
        if n >= MIN_EVIDENCE:
            if w < 0.3:
                out.append("proactive %.2f (%d wakes, mostly 'nothing') → don't volunteer; act only when asked or a real change" % (w, n))
            elif w > 0.7:
                out.append("proactive %.2f (wakes usually worth acting on, n=%d)" % (w, n))
            else:
                out.append("proactive %.2f (n=%d)" % (w, n))
        w, n = _w(p, "memory")
        if n >= MIN_EVIDENCE:
            out.append("memory %.2f (%d accessed; share retrieved again drives it)" % (w, n))
        w, n = _w(p, "curiosity")
        if n >= MIN_EVIDENCE:
            if w < 0.3:
                out.append("curiosity %.2f (browses rarely produce a real learning, n=%d) → pursue fewer, deeper" % (w, n))
            else:
                out.append("curiosity %.2f (n=%d)" % (w, n))
    except Exception:
        return []
    return out


def cooldown_scale(prefs: Optional[dict[str, Any]] = None) -> float:
    """Multiplier for proactive cooldowns (question_engine). Neutral 0.5 → 1.0; bounded [0.5, 3.0].
    No evidence → 1.0 (unchanged behaviour)."""
    try:
        w, n = _w(prefs if prefs is not None else load(), "proactive")
        if n < MIN_EVIDENCE:
            return 1.0
        return max(0.5, min(3.0, 0.5 / max(0.05, w)))
    except Exception:
        return 1.0


def rerank_memories(rows: list[dict[str, Any]], prefs: Optional[dict[str, Any]] = None, *,
                    importance_fn=None) -> list[dict[str, Any]]:
    """Re-order memory_search rows: chroma distance blended with each memory's effective
    importance (access-boosted, decayed). Blend strength = learned memory_usefulness; no
    evidence → rows returned untouched. Fail-open."""
    try:
        w, n = _w(prefs if prefs is not None else load(), "memory")
        if n < MIN_EVIDENCE or not rows:
            return rows
        blend = max(0.0, min(0.6, w * 0.6))      # at most 60% of the score from importance
        if importance_fn is None:
            from brain.iris_human_memory import effective_importance as importance_fn  # type: ignore
        dists = [float(r.get("distance") or 0.0) for r in rows]
        lo, hi = min(dists), max(dists)
        span = (hi - lo) or 1.0
        scored = []
        for r, d in zip(rows, dists):
            sim = 1.0 - (d - lo) / span            # 1 = closest
            try:
                imp = float(importance_fn(str(r.get("id") or ""), float(r.get("importance") or 0.0),
                                          float(r.get("ts") or 0.0)))
            except Exception:
                imp = 0.0
            scored.append(((1.0 - blend) * sim + blend * imp, r))
        scored.sort(key=lambda x: -x[0])
        out = []
        for s, r in scored:
            r2 = dict(r)
            r2["rerank_score"] = round(s, 4)
            out.append(r2)
        return out
    except Exception:
        return rows


def explain(root: Optional[str] = None) -> dict[str, Any]:
    p = load()
    return {
        "weights": p.get("weights", {}), "evidence_counts": p.get("evidence_counts", {}),
        "updated_at": p.get("updated_at", 0.0),
        "hints": hints(p), "cooldown_scale": cooldown_scale(p),
        "evidence_sources": {
            FOCUS["proactive"]: "state/wake_verdicts.jsonl acted vs nothing",
            FOCUS["silence"]: "1 - proactive",
            FOCUS["pacing"]: "transcript voice replies cut by a barge-in (heard N<M sentences)",
            FOCUS["yield"]: "same as pacing (no separate signal yet)",
            FOCUS["memory"]: "iris_memory_meta access_count>=2 share",
            FOCUS["curiosity"]: "leisure curiosity browses → real learning_log rows",
        },
        "consumers": ["iris_ambient_snapshot [adaptive] line", "body host voice/perception prompts",
                      "question_engine cooldown × cooldown_scale", "iris_semantic_memory.search rerank"],
        "min_evidence": MIN_EVIDENCE, "alpha": ALPHA,
    }
