"""Action ledger — intent and completion are SEPARATE states (verification-first).

Origin: Zeke + Vale's research handoff (10-04, item 2 "Reflexion", priority VERY HIGH)
and the 09-30 "organ donor" guide (§7: problem → consumer → evidence → write-back).
The observed failure: my logged pattern is RECORDING INTENT AS COMPLETION
(profiles/iris/patterns.md) — writing "will do X" / "fixed" and letting the note stand
in for the act. The code audit (10-05) found the same bug in my own machinery:
brain/planner.py marks every step "completed" without ever running its tool.

Mechanism borrowed from Reflexion (noahshinn/reflexion, MIT):
  * an EXTERNAL, preferably binary evaluator decides success (probe readback, exit
    code, file, port) — an LLM judgement is the fallback, never the default;
  * reflections are written ONLY on failure, keyed by task kind, capped at the last 3
    (their Ω=1-3), and handed back VERBATIM the next time that kind of action opens;
  * a retry budget: the same approach failing repeatedly is a loop, not persistence.
From the guide's design constraints: an action may end impossible / abandoned /
uncertain — "persistence should not mean infinite pursuit".

Outcomes: success | failure | uncertain | abandoned | impossible
  success    — REQUIRES evidence (what was observed), or a passing verifier
  failure    — wants a lesson (diagnosis + what to do next time); if none is given
               the action is listed by `unreflected()` until one is written
  impossible — wants a lesson too (why it can't be done)
  abandoned  — REQUIRES a reason
  uncertain  — honest "can't tell"; still closed, never silently a pass

State (consumers named):
  state/actions/actions.json  — every action row (open + recent closed)
      READ BY: the self-check cron (overdue + unreflected), `act` tool, ptz_predict
  state/actions/lessons.jsonl — failure lessons
      READ BY: open_action() — returns the last 3 lessons of the same kind to the
               caller at the moment it starts the same kind of thing again
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
_DIR = _ROOT / "state" / "actions"
ACTIONS_PATH = _DIR / "actions.json"
LESSONS_PATH = _DIR / "lessons.jsonl"
_LOCK = threading.RLock()

OUTCOMES = ("success", "failure", "uncertain", "abandoned", "impossible")
LESSON_K = 3                 # Reflexion's Ω
RETRY_WINDOW_S = 7 * 86400   # look-back for the loop-breaker
RETRY_BUDGET = 2             # same kind+intent failing this many times => warn
KEEP_CLOSED_S = 30 * 86400
DEFAULT_DEADLINE_S = 6 * 3600


# ── storage ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        d = json.loads(ACTIONS_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    # prune old CLOSED rows; open rows are never pruned (they must be resolved)
    d = {k: v for k, v in d.items()
         if v.get("status") == "open" or now - float(v.get("closed_ts") or now) < KEEP_CLOSED_S}
    tmp = ACTIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, ACTIONS_PATH)


def _lessons() -> list[dict]:
    out = []
    try:
        with open(LESSONS_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return out


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


# ── public API ──────────────────────────────────────────────────────────────

def lessons_for(kind: str, k: int = LESSON_K) -> list[dict]:
    """The last k lessons for this kind of action (Reflexion memory injection)."""
    rows = [r for r in _lessons() if r.get("kind") == kind]
    return rows[-k:]


def open_action(*, kind: str, intent: str, expected: str, verify: Optional[dict] = None,
                source: str = "iris", deadline_s: Optional[float] = None,
                ref: Optional[str] = None) -> dict:
    """Start an action. Returns the row PLUS the past lessons for this kind and a
    loop warning if the same approach keeps failing. `expected` = the observable
    result that would make this a success (required — no expected result, no action)."""
    if not str(expected or "").strip():
        raise ValueError("expected result is required — what would I observe if this worked?")
    now = time.time()
    row = {"id": uuid.uuid4().hex[:10], "kind": str(kind), "intent": str(intent),
           "expected": str(expected), "verify": verify, "source": source, "ref": ref,
           "opened_ts": now, "deadline_ts": now + float(deadline_s or DEFAULT_DEADLINE_S),
           "status": "open"}
    with _LOCK:
        d = _load()
        recent_fail = [v for v in d.values()
                       if v.get("kind") == row["kind"] and v.get("outcome") in ("failure", "impossible")
                       and _norm(v.get("intent")) == _norm(intent)
                       and now - float(v.get("closed_ts") or 0) < RETRY_WINDOW_S]
        d[row["id"]] = row
        _save(d)
    out = dict(row)
    out["lessons"] = lessons_for(row["kind"])
    if len(recent_fail) >= RETRY_BUDGET:
        out["loop_warning"] = (f"this exact intent failed {len(recent_fail)}x in the last 7 days — "
                               f"change the approach, or close it as impossible with a reason")
    return out


def close_action(action_id: str, *, outcome: str, evidence: Any = None,
                 lesson: Optional[dict] = None, reason: Optional[str] = None,
                 verifier: Optional[str] = None) -> dict:
    """Close an action. Enforces the evidence rules; returns the closed row."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    if outcome == "success" and not evidence:
        raise ValueError("success needs evidence — what did I actually observe? "
                         "(or close as uncertain)")
    if outcome == "abandoned" and not (reason or evidence):
        raise ValueError("abandoned needs a reason")
    with _LOCK:
        d = _load()
        row = d.get(action_id)
        if row is None:
            raise KeyError(f"no action {action_id}")
        if row.get("status") != "open":
            return {"ok": False, "error": "already closed", "row": row}
        row.update(status="closed", outcome=outcome, evidence=evidence, reason=reason,
                   verifier=verifier, closed_ts=time.time())
        if lesson and outcome in ("failure", "impossible", "uncertain"):
            row["lesson_id"] = _write_lesson(row, lesson)
        d[action_id] = row
        _save(d)
        return {"ok": True, "row": row}


def _write_lesson(row: dict, lesson: dict) -> str:
    lid = uuid.uuid4().hex[:10]
    rec = {"id": lid, "ts": time.time(), "kind": row.get("kind"), "action_id": row.get("id"),
           "intent": row.get("intent"), "outcome": row.get("outcome"),
           "diagnosis": str(lesson.get("diagnosis") or "").strip(),
           "next_time": str(lesson.get("next_time") or "").strip()}
    _DIR.mkdir(parents=True, exist_ok=True)
    with open(LESSONS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return lid


def reflect(action_id: str, *, diagnosis: str, next_time: str) -> dict:
    """Attach a lesson to an already-closed failure (the Reflexion step, done later)."""
    if not diagnosis.strip() or not next_time.strip():
        raise ValueError("a lesson needs both a diagnosis and what to do next time")
    with _LOCK:
        d = _load()
        row = d.get(action_id)
        if row is None:
            raise KeyError(f"no action {action_id}")
        row["lesson_id"] = _write_lesson(row, {"diagnosis": diagnosis, "next_time": next_time})
        d[action_id] = row
        _save(d)
        return {"ok": True, "lesson_id": row["lesson_id"]}


def list_actions(status: Optional[str] = None, kind: Optional[str] = None, n: int = 20) -> list[dict]:
    rows = list(_load().values())
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    rows.sort(key=lambda r: r.get("opened_ts", 0))
    return rows[-n:]


def overdue(now: Optional[float] = None) -> list[dict]:
    """Open actions past their deadline — intent that never became a result."""
    now = now or time.time()
    return [r for r in list_actions(status="open", n=10_000)
            if float(r.get("deadline_ts") or 0) < now]


def unreflected(source_filter: Optional[str] = None) -> list[dict]:
    """Failures/impossibles with no lesson yet — Reflexion's 'reflect on failure' queue."""
    rows = [r for r in _load().values()
            if r.get("outcome") in ("failure", "impossible") and not r.get("lesson_id")]
    if source_filter:
        rows = [r for r in rows if r.get("source") == source_filter]
    return sorted(rows, key=lambda r: r.get("closed_ts", 0))


def unreflected_by_kind() -> dict:
    """Grouped view for the self-check: per kind, how many failures lack a lesson,
    the newest example, and whether ANY lesson exists for the kind yet. A kind with
    no lesson => write one (reflect). A kind with a lesson => read one example and
    either write a new lesson or mark the rows covered (ack_covered)."""
    have = {}
    for l in _lessons():
        have[l.get("kind")] = l.get("id")
    out: dict[str, dict] = {}
    for r in unreflected():
        k = r.get("kind", "?")
        b = out.setdefault(k, {"count": 0, "latest": None, "existing_lesson": have.get(k)})
        b["count"] += 1
        b["latest"] = {kk: r.get(kk) for kk in ("id", "intent", "outcome", "evidence", "closed_ts")}
    return out


def ack_covered(kind: str, lesson_id: str) -> dict:
    """Mark this kind's unreflected failures as explained by an EXISTING lesson."""
    if not any(l.get("id") == lesson_id for l in _lessons()):
        raise KeyError(f"no lesson {lesson_id}")
    n = 0
    with _LOCK:
        d = _load()
        for r in d.values():
            if (r.get("kind") == kind and r.get("outcome") in ("failure", "impossible")
                    and not r.get("lesson_id")):
                r["lesson_id"] = lesson_id
                r["lesson_covered"] = True
                n += 1
        _save(d)
    return {"ok": True, "marked": n}


def stats() -> dict:
    by: dict[str, dict] = {}
    for r in _load().values():
        k = r.get("kind", "?")
        b = by.setdefault(k, {"open": 0, **{o: 0 for o in OUTCOMES}})
        if r.get("status") == "open":
            b["open"] += 1
        elif r.get("outcome") in OUTCOMES:
            b[r["outcome"]] += 1
    return by


# ── verifiers (external, binary where possible) ─────────────────────────────

def run_verifier(spec: Optional[dict], opened_ts: float = 0.0) -> dict:
    """Run a verifier spec. Returns {passed: True|False|None, evidence}. None = could
    not judge. Probes are READ-ONLY by construction."""
    if not spec:
        return {"passed": None, "evidence": "no verifier attached"}
    t = str(spec.get("type") or "")
    try:
        if t == "port":
            host, port = spec.get("host", "127.0.0.1"), int(spec["port"])
            with socket.create_connection((host, port), timeout=2.0):
                pass
            return {"passed": True, "evidence": f"{host}:{port} accepts connections"}
        if t == "http":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            want = int(spec.get("expect_status", 200))
            try:
                with urllib.request.urlopen(spec["url"], timeout=4.0, context=ctx) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
            return {"passed": code == want, "evidence": f"GET {spec['url']} -> {code} (want {want})"}
        if t == "file":
            p = Path(spec["path"])
            if not p.exists():
                return {"passed": False, "evidence": f"{p} does not exist"}
            if spec.get("newer_than_open") and p.stat().st_mtime < opened_ts:
                return {"passed": False, "evidence": f"{p} not modified since the action opened"}
            if spec.get("contains"):
                ok = str(spec["contains"]) in p.read_text(encoding="utf-8", errors="replace")
                return {"passed": ok, "evidence": f"{p} {'contains' if ok else 'lacks'} {spec['contains']!r}"}
            return {"passed": True, "evidence": f"{p} exists (mtime {p.stat().st_mtime:.0f})"}
        if t == "process":
            import psutil
            needle = str(spec["match"]).lower()
            for pr in psutil.process_iter(["pid", "cmdline"]):
                cl = " ".join(pr.info.get("cmdline") or []).lower()
                if needle in cl:
                    return {"passed": True, "evidence": f"pid {pr.info['pid']} matches {needle!r}"}
            return {"passed": False, "evidence": f"no process matches {needle!r}"}
        if t == "git_pushed":
            # 10-05 scar: `git status` walks the whole working tree (this repo's
            # ignored state/ is huge) — it blew its 10 s timeout INSIDE the runtime and
            # the Windows kill-then-communicate path then hung the tool call ~3 min.
            # rev-list compares commits only: no index refresh, no tree walk, no
            # grandchildren holding the pipes.
            import subprocess
            repo = spec.get("repo", str(_ROOT))
            cp = subprocess.run(["git", "--no-optional-locks", "-C", repo, "rev-list",
                                 "--left-right", "--count", "HEAD...@{u}"],
                                capture_output=True, text=True, timeout=8,
                                stdin=subprocess.DEVNULL,
                                creationflags=0x08000000 if os.name == "nt" else 0)
            if cp.returncode != 0:
                return {"passed": None, "evidence": f"rev-list failed: {cp.stderr.strip()[:160]}"}
            ahead, behind = (int(x) for x in cp.stdout.split()[:2])
            return {"passed": ahead == 0,
                    "evidence": f"HEAD is {ahead} ahead / {behind} behind its upstream"}
        if t == "ptz_home":
            from tools.system.ptz_predict_tool import check_home_now
            r = check_home_now(source="action_ledger")
            v = r.get("verdict")
            return {"passed": {"at_home": True, "off_home": False}.get(v),
                    "evidence": {"verdict": v, "offset_deg": r.get("offset_deg")}}
    except Exception as e:  # noqa: BLE001
        return {"passed": False if t in ("port",) else None, "evidence": f"{t} probe error: {e!r}"[:200]}
    return {"passed": None, "evidence": f"unknown verifier type {t!r}"}


def verify(action_id: str, *, close_on_result: bool = True) -> dict:
    """Run the action's verifier. Pass => close success with the probe as evidence.
    Fail => close failure only if past the deadline (it may still be in progress).
    Can't judge => leave open, note the attempt."""
    with _LOCK:
        row = _load().get(action_id)
    if row is None:
        raise KeyError(f"no action {action_id}")
    if row.get("status") != "open":
        return {"ok": True, "already": row.get("outcome"), "row": row}
    res = run_verifier(row.get("verify"), float(row.get("opened_ts") or 0))
    passed = res.get("passed")
    if close_on_result and passed is True:
        return {"verify": res, **close_action(action_id, outcome="success",
                                              evidence=res["evidence"], verifier="auto")}
    if close_on_result and passed is False and time.time() > float(row.get("deadline_ts") or 0):
        return {"verify": res, **close_action(action_id, outcome="failure",
                                              evidence=res["evidence"], verifier="auto")}
    with _LOCK:
        d = _load()
        if action_id in d:
            d[action_id]["last_verify"] = {"ts": time.time(), **res}
            _save(d)
    return {"ok": True, "verify": res, "still_open": True}
