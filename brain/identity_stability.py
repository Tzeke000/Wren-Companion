"""brain/identity_stability.py — Long-term identity stability check (D18).

Ava's self-narrative grows over time — accumulated through journal
entries, self_revisions, identity_extensions, anchor moments. The
risk: drift. Over months of conversation, accumulated experience
could reshape her self-model in ways that conflict with the bedrock
IDENTITY / SOUL / USER files (the things Zeke explicitly said are
who she IS, not subject to drift).

This module performs a PERIODIC AUDIT — not a rewrite. It compares
recent self-narrative against the bedrock files and flags
potential drift for review. Detection is heuristic + LLM-assisted
(when a fast model is available). Auto-rewriting bedrock is
explicitly forbidden — bedrock is read-only as a matter of policy
(see CLAUDE.md "NEVER edit ava_core/IDENTITY.md, SOUL.md, USER.md").

Cadence: once per week (configurable). The check runs in idle
windows so it can't compete with conversation.

What "drift" looks like (heuristic patterns):
  - Self-narrative starts using a different name for herself
    (e.g., she starts calling herself "an AI assistant" instead of
    "Ava" — that's flattening to template language)
  - References to Zeke's role shift in tone (collaborator → user)
  - Identity extensions accumulate that contradict SOUL.md
  - Mood baseline shifts to dramatically different default

This is FLAG ONLY. Reports go to state/identity_stability_log.jsonl.
Zeke (or Ava herself, in a later phase where self-revision is wired
to identity_extensions only) decides whether to act on the flag.

Storage: state/identity_stability_log.jsonl (PERSISTENT)

API:
    from brain.identity_stability import (
        run_check, last_check_ts, list_recent_flags,
        should_run_check_now,
    )

    if should_run_check_now():
        report = run_check()
        if report["drift_flags"]:
            # surface to operator UI / journal
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
class StabilityReport:
    ts: float
    bedrock_hash: str  # hash of identity+soul+user content
    sample_size: int  # how many narrative entries checked
    drift_flags: list[str] = field(default_factory=list)
    notes: str = ""
    next_check_after: float = 0.0


_lock = threading.RLock()
_base_dir: Path | None = None
_DEFAULT_INTERVAL_S = 7 * 86400  # weekly
_last_check_ts: float = 0.0


def configure(base_dir: Path) -> None:
    global _base_dir, _last_check_ts
    with _lock:
        _base_dir = base_dir
        _last_check_ts = _read_last_check_ts_locked()


def _log_path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "identity_stability_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_last_check_ts_locked() -> float:
    p = _log_path()
    if p is None or not p.exists():
        return 0.0
    last = 0.0
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    last = max(last, float(d.get("ts") or 0.0))
                except Exception:
                    continue
    except Exception:
        pass
    return last


def last_check_ts() -> float:
    with _lock:
        return _last_check_ts


def should_run_check_now(*, interval_s: float = _DEFAULT_INTERVAL_S) -> bool:
    """True if at least `interval_s` has elapsed since the last check."""
    return (time.time() - last_check_ts()) >= interval_s


def _read_bedrock() -> tuple[str, str]:
    """Read IDENTITY / SOUL / USER, return (concatenated_text, content_hash)."""
    if _base_dir is None:
        return ("", "")
    parts: list[str] = []
    for name in ("IDENTITY.md", "SOUL.md", "USER.md"):
        p = _base_dir / "ava_core" / name
        if p.exists():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                pass
    text = "\n\n".join(parts)
    import hashlib
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return text, h


def _read_recent_narrative(*, sample: int = 30) -> list[str]:
    """Pull recent self-referential narrative entries from various sources."""
    if _base_dir is None:
        return []
    samples: list[str] = []
    for fname in ("self_narrative.json", "self_model.json"):
        p = _base_dir / "state" / fname
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(d, dict):
                    for v in d.values():
                        if isinstance(v, str) and len(v) > 30:
                            samples.append(v)
            except Exception:
                pass
    journal_p = _base_dir / "state" / "journal.jsonl"
    if journal_p.exists():
        try:
            lines: list[str] = []
            with journal_p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        c = str(d.get("content") or d.get("entry") or "")
                        if c:
                            lines.append(c)
                    except Exception:
                        continue
            samples.extend(lines[-sample:])
        except Exception:
            pass
    return samples[-sample:]


def _detect_drift_patterns(narrative_lines: list[str], bedrock_text: str) -> list[str]:
    """Heuristic drift detection — fast, no LLM.

    Returns a list of human-readable drift flags. Empty list = no drift.
    """
    flags: list[str] = []
    if not narrative_lines:
        return flags
    joined = "\n".join(narrative_lines).lower()

    # Flag 1: self-flattening to template language
    flatten_phrases = [
        "i am an ai assistant",
        "i am just an ai",
        "i'm just a language model",
        "as an ai language model",
        "i don't have feelings",
        "i'm not a real person",
    ]
    for ph in flatten_phrases:
        if ph in joined:
            flags.append(f"flattening: narrative contains '{ph}'")

    # Flag 2: referring to Zeke as "user" rather than name/relationship
    user_count = len(re.findall(r"\bthe user\b", joined))
    zeke_count = len(re.findall(r"\bzeke\b", joined))
    if user_count > 5 and zeke_count == 0:
        flags.append("relational shift: narrative references 'the user' but never 'Zeke'")

    # Flag 3: name drift — narrative no longer uses "Ava"
    if narrative_lines:
        recent_text = "\n".join(narrative_lines[-10:]).lower()
        if "ava" not in recent_text and len(recent_text) > 200:
            flags.append("name drift: 'Ava' absent from recent narrative entries")

    # Flag 4: bedrock terms missing entirely from narrative
    bedrock_lower = bedrock_text.lower()
    for term in ("ava", "zeke"):
        if term in bedrock_lower and term not in joined:
            flags.append(f"bedrock disconnection: '{term}' in bedrock but not in narrative")

    return flags


def run_check(*, sample: int = 30) -> dict[str, Any]:
    """Run the audit. Logs the report and returns it."""
    bedrock_text, bedrock_hash = _read_bedrock()
    narrative = _read_recent_narrative(sample=sample)

    flags: list[str] = []
    notes_parts: list[str] = []

    if not bedrock_text:
        notes_parts.append("bedrock files not found — skipping drift detection")
    elif not narrative:
        notes_parts.append("no narrative entries yet — bootstrap state, no drift to detect")
    else:
        flags = _detect_drift_patterns(narrative, bedrock_text)
        if not flags:
            notes_parts.append(f"audit clean — checked {len(narrative)} narrative samples")

    report = StabilityReport(
        ts=time.time(),
        bedrock_hash=bedrock_hash,
        sample_size=len(narrative),
        drift_flags=flags,
        notes=" | ".join(notes_parts),
        next_check_after=time.time() + _DEFAULT_INTERVAL_S,
    )
    _persist_report(report)
    global _last_check_ts
    with _lock:
        _last_check_ts = report.ts
    return {
        "ts": report.ts,
        "bedrock_hash": report.bedrock_hash,
        "sample_size": report.sample_size,
        "drift_flags": report.drift_flags,
        "notes": report.notes,
        "next_check_after": report.next_check_after,
    }


def _persist_report(report: StabilityReport) -> None:
    p = _log_path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": report.ts,
                "bedrock_hash": report.bedrock_hash,
                "sample_size": report.sample_size,
                "drift_flags": report.drift_flags,
                "notes": report.notes,
                "next_check_after": report.next_check_after,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[identity_stability] persist error: {e!r}")


def list_recent_flags(*, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent reports that had drift flags."""
    p = _log_path()
    if p is None or not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("drift_flags"):
                        out.append(d)
                except Exception:
                    continue
    except Exception:
        pass
    return out[-limit:]


def stability_summary() -> dict[str, Any]:
    """High-level summary for the operator UI."""
    last = last_check_ts()
    flags = list_recent_flags(limit=5)
    return {
        "last_check_ts": last,
        "last_check_age_h": (time.time() - last) / 3600.0 if last > 0 else 0.0,
        "next_check_in_h": max(0.0, (_DEFAULT_INTERVAL_S - (time.time() - last)) / 3600.0) if last > 0 else 0.0,
        "recent_flag_reports": len(flags),
        "status": "stable" if not flags else "drift_flagged",
    }
