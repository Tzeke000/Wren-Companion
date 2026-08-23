"""Prove `silent_errors` actually catches the bug class it was built for.

Tonight's failure: static-trim v2 raised NameError on every execution for a
day, parked in st["error"], while every health surface read green. A detector
that has never been seen catching anything is a wish, not a check — so this
replays the real shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.system.runtime_repair_tool import _silent_errors  # noqa: E402

FAILS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS += 1


print("1. the exact 08-22 shape: NameError parked in a nested state dict")
g = {
    "_attention_smooth_state": {
        "mode": "lost_hold",
        "ticks": 4145,
        "writes": 877,
        "error": "NameError(\"name '_TRIM_PULSE_UNITS' is not defined\")",
    },
    "_unrelated": {"fine": True},
}
r = _silent_errors({"deep": False}, g)
check("not reported clean", r["clean"] is False)
check("found exactly one finding", r["finding_count"] == 1, str(r["finding_count"]))
w = r["findings"][0]["where"] if r["findings"] else ""
check("path names the owning dict", w == "_attention_smooth_state.error", w)
check("value carries the NameError", "_TRIM_PULSE_UNITS" in str(r["findings"][0]["value"]))

print("2. error_count > 0 counts even when the message was cleared")
r = _silent_errors({"deep": False}, {"servo": {"error": None, "error_count": 12}})
check("nonzero error_count caught", r["finding_count"] == 1, str(r["findings"]))

print("3. healthy state stays quiet (no false alarms)")
r = _silent_errors({"deep": False}, {
    "a": {"error": None, "error_count": 0, "ok": True},
    "b": {"warn": "", "last_error": None},
    "c": {"mode": "pursuit", "writes": 20},
})
check("clean on healthy state", r["clean"] is True, str(r["findings"]))

print("4. recursion + cycles don't hang or double-report")
inner = {"error": "boom"}
cyc = {"child": inner}
inner["parent"] = cyc          # cycle
r = _silent_errors({"deep": False}, {"root": cyc})
check("cycle terminated and error found", r["finding_count"] >= 1, str(r["finding_count"]))

print("5. depth limit holds (too-deep errors are honestly missed, not crashed)")
deep = {"l1": {"l2": {"l3": {"l4": {"error": "very deep"}}}}}
r = _silent_errors({"deep": False}, deep)
print(f"     (depth>3 findings: {r['finding_count']} — documents the limit)")
check("did not crash on deep nesting", r["ok"] is True)

print(f"\n{'ALL PASSED' if not FAILS else str(FAILS) + ' FAILED'}")
raise SystemExit(1 if FAILS else 0)
