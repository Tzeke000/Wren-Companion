"""wake_salience — the meta-loop (Iris_fixes #5, 2026-09-30).

The open loop this closes: my eyes wake cognition ("a face appeared, not yet
recognized"), I spend a full context pass deciding "nothing to act on", and that
verdict is written NOWHERE — so the same wake fires again next boot, next return,
next time he bends his head. Vale's audit (open_loops_audit_vale_2026-09-30.md)
found the wake gate in iris_body_host.py is RAM-only and reads no memory at all.

Three parts, all stdlib, all fail-open (any error ⇒ WAKE, never silence):

  1. a VERDICT LEDGER  — state/wake_verdicts.jsonl. One line per judged wake:
     what the wake was (a compact SIGNATURE), what I decided (acted / nothing),
     and a note. Written by cognition through the `wake_verdict` tool, and by the
     host itself when it auto-suppresses.
  2. a FAST GATE       — judge(signature) answers "have I seen this before, and did
     it matter?" from the ledger in microseconds, without waking cognition.
  3. a PRECISION guard — the gate can only quiet a wake that has ALREADY been
     judged "nothing" MIN_EVIDENCE times in a row with no "acted" among recent
     verdicts; it NEVER quiets an unknown face while Zeke is away (the study rule);
     and every AUDIT_EVERY-th suppression is let through anyway so the gate keeps
     re-validating itself against a live me instead of drifting.

Signatures are deliberately coarse — they name the SHAPE of a wake, not the frame:
  face_appeared|unknown|boot|wifi=present     (the every-restart stranger alarm)
  face_appeared|zeke|return<300|wifi=present  (him sitting back down)
  face_appeared|unknown|steady|wifi=away      (never suppressed — study it)
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from typing import Any, Optional

# ---- knobs (module constants on purpose: the gate must be legible, not tunable by accident)
BOOT_WINDOW_S = 120.0        # a wake this soon after the host started is a "boot" wake
RETURN_SHORT_S = 300.0       # matches PERCEPTION_REWAKE_MIN_GONE_S in the host
MIN_EVIDENCE = 3             # explicit verdicts needed before the gate may quiet anything
RECENT_ACTED_WINDOW = 10     # an "acted" among the last N explicit verdicts blocks suppression
NOTHING_RATIO_MIN = 0.8      # and overall nothing-share must be at least this
AUDIT_EVERY = 8              # every Nth consecutive suppression fires anyway (audit sample)
MAX_AGE_S = 30 * 86400.0     # verdicts older than this are history, not evidence
TAIL_LINES = 4000            # how much ledger to read (newest lines)

EXPLICIT = ("acted", "nothing")                     # what cognition may record
VERDICTS = EXPLICIT + ("auto_suppressed", "unrecorded")


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ledger_path() -> str:
    """state/wake_verdicts.jsonl (env WAKE_SALIENCE_LEDGER overrides — tests use it)."""
    p = os.environ.get("WAKE_SALIENCE_LEDGER", "").strip()
    if p:
        return p
    try:
        from brain.iris_paths import paths  # type: ignore
        return str(paths.state_dir / "wake_verdicts.jsonl")
    except Exception:
        return os.path.join(_repo_root(), "state", "wake_verdicts.jsonl")


# ---------------------------------------------------------------- signature
def signature(stype: str, who: str, *, boot_age_s: Optional[float] = None,
              first_obs: bool = False, gone_s: Optional[float] = None,
              zeke_wifi: str = "unknown") -> str:
    """Compact, deterministic name for the SHAPE of a wake.

    who       : person id ('zeke') or '' / 'unknown'
    boot_age_s: seconds since the host started (None = unknown)
    first_obs : first observation of the session (host's announced_present was None)
    gone_s    : seconds the person had been gone before this arrival (None = no gap)
    zeke_wifi : 'present' | 'away' | 'unknown' (the wifi watcher's verdict)
    """
    w = (who or "").strip().lower()
    if w in ("", "none", "unknown"):
        w = "unknown"
    if boot_age_s is not None and boot_age_s < BOOT_WINDOW_S:
        phase = "boot"
    elif first_obs:
        phase = "first"
    elif gone_s is not None:
        phase = "return<%d" % int(RETURN_SHORT_S) if gone_s < RETURN_SHORT_S else "return"
    else:
        phase = "steady"
    wifi = (zeke_wifi or "unknown").strip().lower()
    if wifi not in ("present", "away", "unknown"):
        wifi = "unknown"
    return "%s|%s|%s|wifi=%s" % ((stype or "signal").strip().lower(), w, phase, wifi)


def parse_signature(sig: str) -> dict[str, str]:
    parts = (sig or "").split("|")
    out = {"type": parts[0] if parts else "", "who": "", "phase": "", "wifi": "unknown"}
    if len(parts) > 1:
        out["who"] = parts[1]
    if len(parts) > 2:
        out["phase"] = parts[2]
    for p in parts[3:]:
        if p.startswith("wifi="):
            out["wifi"] = p[5:]
    return out


# ---------------------------------------------------------------- ledger I/O
def record(sig: str, verdict: str, *, note: str = "", source: str = "perception",
           extra: Optional[dict[str, Any]] = None, ts: Optional[float] = None) -> dict[str, Any]:
    """Append one verdict. Raises ValueError on a bad verdict (a tool should surface that,
    not swallow it — the counterfactual archive died of swallowed errors)."""
    v = (verdict or "").strip().lower()
    if v not in VERDICTS:
        raise ValueError("verdict must be one of %s, got %r" % (", ".join(VERDICTS), verdict))
    if not (sig or "").strip():
        raise ValueError("signature is required")
    now = float(ts) if ts is not None else time.time()
    rec: dict[str, Any] = {
        "ts": now,
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "sig": sig.strip(),
        "verdict": v,
        "source": source,
        "note": (note or "")[:400],
    }
    if extra:
        rec["extra"] = extra
    p = ledger_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _CACHE["mtime"] = -1.0  # invalidate
    return rec


_CACHE: dict[str, Any] = {"path": "", "mtime": -1.0, "rows": []}


def _load(now: Optional[float] = None) -> list[dict[str, Any]]:
    """Newest TAIL_LINES rows, oldest→newest, corrupt lines skipped, stale rows dropped."""
    p = ledger_path()
    try:
        st = os.stat(p)
    except OSError:
        return []
    if _CACHE["path"] == p and _CACHE["mtime"] == st.st_mtime:
        rows = _CACHE["rows"]
    else:
        rows = []
        try:
            with open(p, "rb") as f:
                # read a bounded tail: TAIL_LINES * ~200B is plenty; fall back to whole file
                size = st.st_size
                f.seek(max(0, size - TAIL_LINES * 240))
                blob = f.read().decode("utf-8", errors="replace")
            lines = blob.split("\n")
            if size > TAIL_LINES * 240:
                lines = lines[1:]  # first line may be partial
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("sig") and d.get("verdict") in VERDICTS:
                    rows.append(d)
        except Exception:
            rows = []
        _CACHE.update({"path": p, "mtime": st.st_mtime, "rows": rows})
    t = float(now) if now is not None else time.time()
    return [r for r in rows if (t - float(r.get("ts") or 0.0)) <= MAX_AGE_S]


def stats(sig: str, *, now: Optional[float] = None) -> dict[str, Any]:
    rows = [r for r in _load(now) if r.get("sig") == sig]
    by = Counter(r["verdict"] for r in rows)
    explicit = [r for r in rows if r["verdict"] in EXPLICIT]
    # consecutive auto_suppressed since the last non-suppressed row (for the audit sample)
    streak = 0
    for r in reversed(rows):
        if r["verdict"] == "auto_suppressed":
            streak += 1
        else:
            break
    last = rows[-1] if rows else None
    return {
        "sig": sig,
        "total": len(rows),
        "by_verdict": dict(by),
        "explicit": len(explicit),
        "recent_explicit": [r["verdict"] for r in explicit[-RECENT_ACTED_WINDOW:]],
        "suppressed_streak": streak,
        "last_verdict": last["verdict"] if last else None,
        "last_ts": float(last["ts"]) if last else None,
        "last_note": (last.get("note") or "") if last else "",
    }


# ---------------------------------------------------------------- the gate
def judge(sig: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """Should this wake reach cognition? Returns {wake, reason, stats, sig}.

    Fail-open by construction: every early return that isn't a proven pattern wakes."""
    try:
        meta = parse_signature(sig)
        st = stats(sig, now=now)
        # 1. the study rule: an unknown face while Zeke is not known-present is the MOST
        #    important wake in the system, never the least. Wifi 'unknown' counts as away.
        if meta["who"] == "unknown" and meta["wifi"] != "present":
            return {"wake": True, "reason": "unknown face while Zeke not known-present (study rule)",
                    "stats": st, "sig": sig}
        # 2. not enough evidence yet
        if st["explicit"] < MIN_EVIDENCE:
            return {"wake": True, "reason": "insufficient evidence (%d/%d explicit verdicts)"
                    % (st["explicit"], MIN_EVIDENCE), "stats": st, "sig": sig}
        recent = st["recent_explicit"]
        # 3. I acted on this shape recently → it matters
        if "acted" in recent:
            return {"wake": True, "reason": "acted on this shape within the last %d verdicts"
                    % len(recent), "stats": st, "sig": sig}
        # 4. the last MIN_EVIDENCE explicit verdicts must all be 'nothing'
        if any(v != "nothing" for v in recent[-MIN_EVIDENCE:]):
            return {"wake": True, "reason": "recent verdicts not uniformly 'nothing'",
                    "stats": st, "sig": sig}
        n_nothing = st["by_verdict"].get("nothing", 0)
        ratio = n_nothing / float(max(1, st["explicit"]))
        if ratio < NOTHING_RATIO_MIN:
            return {"wake": True, "reason": "nothing-ratio %.2f below %.2f" % (ratio, NOTHING_RATIO_MIN),
                    "stats": st, "sig": sig}
        # 5. audit sample: every AUDIT_EVERY-th consecutive suppression fires anyway
        if (st["suppressed_streak"] + 1) % AUDIT_EVERY == 0:
            return {"wake": True, "reason": "audit sample (%d suppressed in a row)" % st["suppressed_streak"],
                    "stats": st, "sig": sig, "audit": True}
        return {"wake": False,
                "reason": "judged 'nothing' %d× (last %d in a row), nothing-ratio %.2f, no recent action"
                          % (n_nothing, MIN_EVIDENCE, ratio),
                "stats": st, "sig": sig}
    except Exception as e:  # fail OPEN
        return {"wake": True, "reason": "salience error (fail-open): %r" % (e,), "sig": sig,
                "stats": {}}


def history_line(sig: str, *, now: Optional[float] = None) -> str:
    """One line of history for the wake prompt, so cognition knows what past-me decided."""
    try:
        st = stats(sig, now=now)
        if st["total"] == 0:
            return "SALIENCE: first time I've seen this shape (%s)." % sig
        t = float(now) if now is not None else time.time()
        age = t - float(st["last_ts"] or t)
        if age < 90:
            ago = "%ds ago" % int(age)
        elif age < 5400:
            ago = "%dm ago" % int(age / 60)
        else:
            ago = "%.1fh ago" % (age / 3600.0)
        by = st["by_verdict"]
        parts = ["%s×%d" % (k, v) for k, v in sorted(by.items(), key=lambda kv: -kv[1])]
        line = "SALIENCE: shape %s seen %d× (%s); last verdict %s %s" % (
            sig, st["total"], ", ".join(parts), st["last_verdict"], ago)
        if st["last_note"]:
            line += " — \"%s\"" % st["last_note"][:80]
        return line + "."
    except Exception:
        return "SALIENCE: (history unavailable)"


def recent(n: int = 20, *, sig: Optional[str] = None) -> list[dict[str, Any]]:
    rows = _load()
    if sig:
        rows = [r for r in rows if r.get("sig") == sig]
    return rows[-max(1, int(n)):]


def explain() -> dict[str, Any]:
    return {
        "what": "verdict ledger + fast gate for eyes-wakes; the host asks judge(sig) before "
                "waking cognition and records auto-suppressions; cognition records acted/nothing "
                "via the wake_verdict tool",
        "ledger": ledger_path(),
        "rules": {
            "never_suppress": ["unknown face while Zeke not known-present (study rule)",
                               "fewer than %d explicit verdicts" % MIN_EVIDENCE,
                               "an 'acted' among the last %d explicit verdicts" % RECENT_ACTED_WINDOW,
                               "recent verdicts not uniformly 'nothing'",
                               "nothing-ratio below %.2f" % NOTHING_RATIO_MIN],
            "audit_sample": "every %dth consecutive suppression wakes anyway" % AUDIT_EVERY,
            "evidence_max_age_days": MAX_AGE_S / 86400.0,
        },
        "verdicts": {"acted": "I did something outward (spoke, DM'd, moved, studied)",
                     "nothing": "looked, decided nothing / quiet note only",
                     "auto_suppressed": "written by the host when the gate quieted a wake",
                     "unrecorded": "host fallback when a wake turn ended without a verdict (not evidence)"},
    }
