"""Memory hygiene + a cursor-driven reflection pass (Letta, borrowed).

Origin: part 5 of the Zeke+Vale research plan. Letta's split (letta-ai, Apache-2.0):
the primary agent USES memory; a background "sleep-time"/reflection agent EDITS it,
in phases Investigate → Extract → filter (lasting? already captured? generalisable?
dates absolute?) → Update ("fix the stale entry AT THE SOURCE — do not append the new
version alongside the old") → Review (stale content, cross-reference integrity, tier
check). It sees each message exactly once via a cursor (last_processed_message_id).
Their Context-Bench names the failure exactly: a weak model "appends a fourth dated
correction" instead of writing one general rule.

My observed failures this targets (all on record):
  * CORE keeps re-growing past its load cap (trimmed 5+ times — "fixed is the wrong
    word for things that regrow"); the tail silently drops when it does;
  * stale state-claims in always-loaded files (CLAUDE.md said voice was OFF for a month
    while it was on — and it instructed future-me to leave working services dead);
  * stacked dated corrections on one line instead of one rewritten rule;
  * notes nobody can reach (written, never linked from CORE / a hub / the index).

Two parts:
  audit()       — MECHANICAL checks, no LLM. Each finding names the file:line and the
                  Letta-style fix. Read by the self-check cron (item 10i).
  dream_next()  — the unprocessed slice of state/transcript.jsonl since the cursor,
  dream_commit()  plus the Letta filter checklist; I (the cognition) do the judging and
                  the fix-at-source edits, then commit the cursor. Nothing is reflected on
                  twice, nothing is skipped.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
MEM_DIR = Path(os.environ.get("IRIS_MEMORY_DIR",
                              r"C:\Users\Owner\.claude\projects\D--Wren-Companion\memory"))
CLAUDE_MD = _ROOT / "CLAUDE.md"
TRANSCRIPT = _ROOT / "state" / "transcript.jsonl"
_STATE = _ROOT / "state" / "memory_hygiene"
CURSOR_PATH = _STATE / "dream_cursor.json"
LOG_PATH = _STATE / "dream_log.jsonl"

CORE_CAP_BYTES = 24_400       # CORE's own header: "Cap 24.4KB … measure in BYTES"
CORE_TARGET_BYTES = 17_100
STALE_DAYS = 14

_REL = re.compile(r"\b(today|yesterday|tonight|this (?:morning|afternoon|evening)|last night|"
                  r"right now|recently|earlier today)\b", re.I)
_DATE = re.compile(r"\b(20\d\d-)?(0[1-9]|1[0-2])-([0-2]\d|3[01])\b")
_STATEY = re.compile(r"\b(currently|(?:is|are) (?:ON|OFF|DOWN|UP|LIVE|ARMED|PARKED)|right now|since)\b", re.I)
_HISTORYISH = re.compile(r"(\(Historical|\*\(Until |incident that produced|Until 20\d\d-|Corrected 20)", re.I)
_WIKI = re.compile(r"\[\[([^\]|#]+)")
_MDLINK = re.compile(r"\]\(([^)#\s]+\.md)\)")
_RESOLVED = re.compile(r"(✅\s*RESOLVED|✅\s*FIXED|\bRESOLVED\b|do NOT re-investigate)", re.I)
_CORRECTION = re.compile(r"(correct(?:ed|ion)|was wrong|retract)", re.I)


def _year_for(month: int, day: int, today: time.struct_time) -> int:
    """Dates in my notes are mostly MM-DD; assume the most recent past occurrence."""
    y = today.tm_year
    if (month, day) > (today.tm_mon, today.tm_mday):
        y -= 1
    return y


def _newest_date_age_days(line: str, now: float) -> Optional[float]:
    t = time.localtime(now)
    best = None
    for m in _DATE.finditer(line):
        yr = int(m.group(1)[:-1]) if m.group(1) else _year_for(int(m.group(2)), int(m.group(3)), t)
        try:
            ts = time.mktime((yr, int(m.group(2)), int(m.group(3)), 12, 0, 0, 0, 0, -1))
        except (OverflowError, ValueError):
            continue
        age = (now - ts) / 86400.0
        if age >= -1 and (best is None or age < best):
            best = age
    return best


def _md_files() -> list[Path]:
    return sorted(MEM_DIR.glob("*.md"))


def audit(now: Optional[float] = None) -> dict:
    """Mechanical hygiene report over CORE (MEMORY.md), the hubs, and CLAUDE.md."""
    now = now or time.time()
    findings: list[dict] = []
    core_p = MEM_DIR / "MEMORY.md"
    core = core_p.read_text(encoding="utf-8") if core_p.exists() else ""
    core_bytes = len(core.encode("utf-8"))
    lines = core.splitlines()

    # 1. size vs the load cap (the tail silently drops past it)
    big = sorted(((len(l.encode("utf-8")), i + 1, l[:90]) for i, l in enumerate(lines)), reverse=True)[:5]
    if core_bytes > CORE_TARGET_BYTES:
        findings.append({"check": "core_size", "severity": "high" if core_bytes > 0.95 * CORE_CAP_BYTES else "medium",
                         "detail": f"MEMORY.md {core_bytes} B (target {CORE_TARGET_BYTES}, cap {CORE_CAP_BYTES})",
                         "fix": "RELOCATE detail of the biggest lines into their linked notes "
                                "(rewording lands at par — only relocation cuts size)",
                         "biggest": [{"line": n, "bytes": b, "start": s} for b, n, s in big]})

    names = {p.stem for p in _md_files()}
    # 2. cross-reference integrity: CORE + hubs
    hubs = [p for p in _md_files() if p.name.startswith("hub_")]
    for p in [core_p] + hubs:
        if not p.exists():
            continue
        for i, l in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            l = re.sub(r"`[^`]*`", "", l)  # links inside inline code are examples, not links
            for m in _WIKI.finditer(l):
                tgt = m.group(1).strip()
                if tgt and tgt not in names and not (MEM_DIR / tgt).exists():
                    findings.append({"check": "broken_link", "severity": "medium",
                                     "where": f"{p.name}:{i}", "detail": f"[[{tgt}]] has no note",
                                     "fix": "point it at the note that exists, or drop it"})
            for m in _MDLINK.finditer(l):
                tgt = m.group(1)
                if "/" in tgt or "\\" in tgt:
                    continue
                if not (MEM_DIR / tgt).exists():
                    findings.append({"check": "broken_link", "severity": "medium",
                                     "where": f"{p.name}:{i}", "detail": f"({tgt}) missing",
                                     "fix": "point it at the note that exists, or drop it"})

    # 3. tier check: RESOLVED history living in CORE
    for i, l in enumerate(lines, 1):
        m = _RESOLVED.search(l)
        if m and m.start() < 40 and l.lstrip().startswith("-") and len(l.encode("utf-8")) > 200:
            findings.append({"check": "resolved_in_core", "severity": "low", "where": f"MEMORY.md:{i}",
                             "detail": l[:120], "fix": "collapse to a pointer into index_archive "
                                                       "(RESOLVED work is history, not state)"})

    # 4. stale state-claims in always-loaded files (CORE + CLAUDE.md)
    for fname, text in (("MEMORY.md", core),
                        ("CLAUDE.md", CLAUDE_MD.read_text(encoding="utf-8") if CLAUDE_MD.exists() else "")):
        for i, l in enumerate(text.splitlines(), 1):
            if not _STATEY.search(l) or _HISTORYISH.search(l):
                continue
            age = _newest_date_age_days(l, now)
            if age is not None and age > STALE_DAYS:
                findings.append({"check": "stale_state_claim", "severity": "medium",
                                 "where": f"{fname}:{i}", "detail": f"newest date on the line is {age:.0f} d old: {l[:110]}",
                                 "fix": "PROBE the claim now; if it still holds, re-date it; if not, fix it "
                                        "at the source this session (always-loaded files outrank memory)"})

    # 5. relative dates (Letta: a persisted memory must not say 'today')
    for p in [core_p] + hubs:
        if not p.exists():
            continue
        for i, l in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            m = _REL.search(l)
            if m and not _DATE.search(l):
                findings.append({"check": "relative_date", "severity": "low", "where": f"{p.name}:{i}",
                                 "detail": f"'{m.group(0)}' with no absolute date: {l[:100]}",
                                 "fix": "replace with the absolute date (memories persist indefinitely)"})

    # 6. append-instead-of-fix: stacked corrections on one CORE line
    for i, l in enumerate(lines, 1):
        n = len(_CORRECTION.findall(l)) + max(0, len(_DATE.findall(l)) - 3)
        if n >= 4:
            findings.append({"check": "stacked_corrections", "severity": "low", "where": f"MEMORY.md:{i}",
                             "detail": f"{n} correction/date markers on one line: {l[:100]}",
                             "fix": "rewrite as ONE current rule; move the history into the linked note"})

    # 7. orphans: notes the cascade can't reach. Reachability is TRANSITIVE — the
    #    cascade is CORE -> hubs/index -> notes -> notes, so walk the link graph.
    texts = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in _md_files()}
    stem_to_name = {Path(n).stem: n for n in texts}
    roots = ["MEMORY.md", "index_archive.md"] + [p.name for p in hubs]
    seen, stack = set(), [r for r in roots if r in texts]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        t = texts.get(cur, "")
        for stem, nm in stem_to_name.items():
            if nm not in seen and stem in t:
                stack.append(nm)
    orphans = sorted(n for n in texts if n not in seen)
    if orphans:
        findings.append({"check": "orphans", "severity": "low", "count": len(orphans),
                         "detail": f"{len(orphans)} notes no link-path reaches from CORE / hubs / index",
                         "examples": orphans[:12],
                         "fix": "index each in index_archive.md (one line) or fold it into its parent note"})

    sev = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: sev.get(f.get("severity"), 3))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["check"]] = counts.get(f["check"], 0) + 1
    return {"ok": True, "core_bytes": core_bytes, "core_cap": CORE_CAP_BYTES,
            "core_target": CORE_TARGET_BYTES, "counts": counts, "findings": findings}


# ── the reflection pass with a cursor ───────────────────────────────────────

LETTA_FILTER = (
    "For each candidate memory in this slice ask, in order: (1) LASTING — will it still matter in a "
    "month? (2) ALREADY CAPTURED — is it on disk already? If so and it changed, FIX THAT ENTRY AT THE "
    "SOURCE; never append a second version beside the old. (3) GENERALISABLE — can a specific "
    "correction become one rule? (4) ABSOLUTE — convert 'today/yesterday/tonight' to dates. Then "
    "review: does any always-loaded line now disagree with what was said? Fix it. Be selective; "
    "not every message warrants an edit, but aim for high recall of corrections and stated "
    "preferences — especially Zeke's.")


def _cursor() -> dict:
    try:
        return json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"ts": 0.0, "id": None}


def dream_next(max_items: int = 80, max_chars: int = 14_000) -> dict:
    """The next unprocessed transcript slice (oldest first) after the cursor."""
    cur = _cursor()
    out, size, total_after = [], 0, 0
    try:
        with open(TRANSCRIPT, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if float(r.get("ts") or 0) <= float(cur.get("ts") or 0):
                    continue
                total_after += 1
                if len(out) >= max_items or size >= max_chars:
                    continue
                txt = str(r.get("content") or "")[:600]
                item = {"ts": r.get("ts"), "iso": r.get("iso"), "role": r.get("role"),
                        "source": r.get("source"), "modality": r.get("modality"), "text": txt}
                out.append(item)
                size += len(txt) + 60
    except FileNotFoundError:
        pass
    return {"ok": True, "cursor": cur, "items": out, "returned": len(out),
            "remaining_after_this": max(0, total_after - len(out)),
            "until_ts": out[-1]["ts"] if out else None, "filter": LETTA_FILTER,
            "then": "make the fix-at-source edits, then memory_hygiene action=dream_commit "
                    "until_ts=<until_ts> summary='what changed (or: nothing survived the filter)'"}


def dream_commit(until_ts: float, summary: str) -> dict:
    if not str(summary or "").strip():
        raise ValueError("summary required — say what changed, or 'nothing survived the filter'")
    cur = _cursor()
    if float(until_ts) <= float(cur.get("ts") or 0):
        raise ValueError("until_ts is not past the cursor")
    _STATE.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps({"ts": float(until_ts), "committed_ts": time.time()}), encoding="utf-8")
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "from": cur.get("ts"), "until": float(until_ts),
                             "summary": summary}) + "\n")
    return {"ok": True, "cursor": float(until_ts)}
