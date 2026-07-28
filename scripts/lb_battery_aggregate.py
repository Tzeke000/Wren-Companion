"""lb_battery_aggregate — aggregate N battery runs per model and compare rates.

2026-07-28, Iris. Built because single-run A/B on this battery is NOISE: two
back-to-back runs of the SAME model (iris-little-v14) scored 11 PASS / 2 PARTIAL
/ 1 FAIL and then 10 PASS / 4 FAIL, flipping four questions in both directions.
That run-to-run spread is as large as the v12-vs-v14 difference we were trying to
measure, so any conclusion drawn from one run each is unsupported.

Scores PASS=1.0, PARTIAL=0.5, FAIL=0.0 and reports a per-question mean over runs,
so a question that flips 50/50 reads as 0.5 rather than masquerading as a verdict.

Usage:
    .venv/Scripts/python.exe scripts/lb_battery_aggregate.py <modelA> <modelB>
    (globs state/little_brain/battery_<model>_*.json, optionally --after STAMP)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "state" / "little_brain"
SCORE = {"PASS": 1.0, "PARTIAL": 0.5, "FAIL": 0.0}


def runs_for(model: str, after: str | None) -> list[dict]:
    out = []
    for p in sorted(DIR.glob(f"battery_{model}_*.json")):
        stamp = p.stem.split("_")[-1]
        if after and stamp < after:
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def agg(runs: list[dict]) -> tuple[dict, dict]:
    per_q: dict[str, list[float]] = defaultdict(list)
    axis_of: dict[str, str] = {}
    for r in runs:
        for row in r["results"]:
            per_q[row["id"]].append(SCORE.get(row["verdict"], 0.0))
            axis_of[row["id"]] = row.get("axis", "?")
    return {k: sum(v) / len(v) for k, v in per_q.items()}, axis_of


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    after = None
    if "--after" in sys.argv:
        after = sys.argv[sys.argv.index("--after") + 1]
    if len(args) < 2:
        print(__doc__)
        return 2
    ma, mb = args[0], args[1]
    ra, rb = runs_for(ma, after), runs_for(mb, after)
    if not ra or not rb:
        print(f"need runs for both: {ma}={len(ra)} {mb}={len(rb)}")
        return 1
    a, axis = agg(ra)
    b, axis2 = agg(rb)
    axis.update(axis2)

    print(f"{ma}: {len(ra)} runs     {mb}: {len(rb)} runs")
    print(f"{'question':16s} {'axis':10s} {ma[-3:]:>6s} {mb[-3:]:>6s}   delta")
    print("-" * 52)
    deltas = []
    for qid in sorted(set(a) | set(b)):
        av, bv = a.get(qid, float("nan")), b.get(qid, float("nan"))
        d = bv - av
        deltas.append(d)
        mark = "  <-- worse" if d < -0.01 else ("  <-- better" if d > 0.01 else "")
        print(f"{qid:16s} {axis.get(qid,'?'):10s} {av:6.2f} {bv:6.2f}  {d:+5.2f}{mark}")
    print("-" * 52)
    sa = sum(a.values()) / len(a)
    sb = sum(b.values()) / len(b)
    print(f"{'MEAN':16s} {'':10s} {sa:6.2f} {sb:6.2f}  {sb-sa:+5.2f}")
    print()
    print("NOTE: with few runs per model, a per-question delta under ~0.3 is "
          "inside this battery's observed run-to-run noise. Treat small deltas "
          "as 'no measured difference', not as a result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
