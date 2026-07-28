"""lb_battery_ab — diff two little-brain battery runs, per axis, per question.

2026-07-28, Iris. The battery writes battery_<model>_<stamp>.json; this turns two
of them into the actual A/B answer Zeke asks for: what got FIXED, what REGRESSED,
what stayed the same. Regressions are printed first, because a bake that fixes two
things and breaks three is a worse model and the summary should say so loudly.

Usage:
    .venv/Scripts/python.exe scripts/lb_battery_ab.py <baseline.json> <candidate.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RANK = {"FAIL": 0, "PARTIAL": 1, "PASS": 2}


def load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    base, cand = load(sys.argv[1]), load(sys.argv[2])
    b = {r["id"]: r for r in base["results"]}
    c = {r["id"]: r for r in cand["results"]}

    print(f"BASELINE  {base['model']}  {base['stamp']}  {base['totals']}")
    print(f"CANDIDATE {cand['model']}  {cand['stamp']}  {cand['totals']}")
    print()

    fixed, regressed, same = [], [], []
    for qid in sorted(set(b) | set(c)):
        bv = b.get(qid, {}).get("verdict", "MISSING")
        cv = c.get(qid, {}).get("verdict", "MISSING")
        axis = (c.get(qid) or b.get(qid) or {}).get("axis", "?")
        row = (qid, axis, bv, cv, (c.get(qid) or {}).get("why", ""))
        if RANK.get(cv, -1) > RANK.get(bv, -1):
            fixed.append(row)
        elif RANK.get(cv, -1) < RANK.get(bv, -1):
            regressed.append(row)
        else:
            same.append(row)

    def show(title, rows):
        print(f"=== {title} ({len(rows)}) ===")
        for qid, axis, bv, cv, why in rows:
            print(f"  [{axis}] {qid}: {bv} -> {cv}")
            if why:
                print(f"      why: {why[:200]}")
        print()

    show("REGRESSED", regressed)
    show("FIXED", fixed)
    show("UNCHANGED", same)

    verdict = ("REGRESSION — do not flip" if regressed else
               ("IMPROVED" if fixed else "NO CHANGE"))
    print(f"NET: {len(fixed)} fixed, {len(regressed)} regressed  =>  {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
