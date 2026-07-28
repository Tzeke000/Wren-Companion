"""Consume little-Iris's escalation queue (the RECEIVING half of ask_big_iris).

little-Iris's `ask_big_iris` tool (brain/little_brain_tools.py) FILES requests to
state/little_brain/escalations.jsonl. This script is the other half: big-Iris
(or her self-maintenance cron) reads the PENDING ones so "hand it up" actually
reaches a human/cognition instead of rotting in a file.

Usage:
    python scripts/check_escalations.py            # list pending (human + JSON)
    python scripts/check_escalations.py --resolve-all "note"   # mark all handled
    python scripts/check_escalations.py --resolve 3 "note"     # mark one (by line#)

Exit code: 0 always (so a cron step never trips on "no pending"). The pending
COUNT is printed on the first line as  PENDING=<n>  for easy machine parsing.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ESCALATIONS = ROOT / "state" / "little_brain" / "escalations.jsonl"
IRIS_TIME_STATE = ROOT / "state" / "iris_time.json"
_EDT = timezone(timedelta(hours=-4))


def _now_iso() -> str:
    """Authoritative wall clock (1Hz substrate) if fresh, else system EDT."""
    try:
        st = json.loads(IRIS_TIME_STATE.read_text(encoding="utf-8"))
        ts = float(st.get("last_tick_ts") or 0.0)
        if ts > 0:
            return datetime.fromtimestamp(ts, _EDT).isoformat()
    except Exception:
        pass
    return datetime.now(_EDT).isoformat()


def _load() -> list[tuple[int, dict]]:
    """Return [(line_number, entry_dict), ...] for every valid JSON line."""
    if not ESCALATIONS.is_file():
        return []
    out = []
    for i, ln in enumerate(ESCALATIONS.read_text(encoding="utf-8").splitlines(), 1):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append((i, json.loads(ln)))
        except Exception:
            continue
    return out


def _save(rows: list[tuple[int, dict]]) -> None:
    """Rewrite the file from the (renumbered) entries, one JSON per line."""
    body = "\n".join(json.dumps(e, ensure_ascii=False) for _, e in rows)
    ESCALATIONS.write_text(body + ("\n" if body else ""), encoding="utf-8")


def _resolve(rows, note: str, which: int | None) -> int:
    n = 0
    for lineno, e in rows:
        if e.get("status") != "pending":
            continue
        if which is not None and lineno != which:
            continue
        e["status"] = "resolved"
        e["resolved_ts"] = _now_iso()
        if note:
            e["resolution"] = note
        n += 1
    _save(rows)
    return n


def main() -> int:
    args = sys.argv[1:]
    rows = _load()
    # origin=="eval" entries were provoked by the test battery, whose esc_*/ref_*
    # questions are SUPPOSED to make her hand up (2026-07-28). They're not real
    # asks, so they don't belong in the pending list. Only the explicit tag is
    # filtered — untagged entries still count as live.
    pending = [(ln, e) for ln, e in rows
               if e.get("status") == "pending" and e.get("origin") != "eval"]

    if args and args[0] == "--resolve-all":
        note = args[1] if len(args) > 1 else ""
        n = _resolve(rows, note, None)
        print(f"PENDING=0\nresolved {n} escalation(s)")
        return 0
    if args and args[0] == "--resolve":
        which = int(args[1]) if len(args) > 1 else None
        note = args[2] if len(args) > 2 else ""
        n = _resolve(rows, note, which)
        remaining = len([e for _, e in _load() if e.get("status") == "pending"])
        print(f"PENDING={remaining}\nresolved {n} escalation(s)")
        return 0

    # default: list pending
    print(f"PENDING={len(pending)}")
    for lineno, e in pending:
        print(f"  [line {lineno}] {e.get('ts', '?')}  {e.get('request', '')}")
    if not pending:
        print("  (none — little-Iris hasn't handed anything up)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
