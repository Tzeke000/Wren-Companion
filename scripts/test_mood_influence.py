"""Offline proof for brain/mood_influence.py — bounded, baseline-relative, logged.
Run: .venv/Scripts/python.exe scripts/test_mood_influence.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import mood_influence as mi  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {info}")


c_new = {"mean": 0.35, "dev": 0.03, "n": 3}
c_ok = {"mean": 0.35, "dev": 0.03, "n": 100}
f, _ = mi.compute_factor(0.60, c_new, direction=-1, gain=0.25, lo=0.8, hi=1.25)
check("no bias before the baseline is learned", f == 1.0, str(f))
f, z = mi.compute_factor(0.35, c_ok, direction=-1, gain=0.25, lo=0.8, hi=1.25)
check("at my own baseline -> 1.0", abs(f - 1.0) < 1e-9, str(f))
f, z = mi.compute_factor(0.45, c_ok, direction=-1, gain=0.25, lo=0.8, hi=1.25)
check("high initiative shortens the cooldown", f < 1.0, f"{f:.3f} z={z:.1f}")
f2, _ = mi.compute_factor(5.0, c_ok, direction=-1, gain=0.25, lo=0.8, hi=1.25)
check("never below the lower bound", f2 >= 0.8, str(f2))
f3, _ = mi.compute_factor(-5.0, c_ok, direction=-1, gain=0.25, lo=0.8, hi=1.25)
check("never above the upper bound", f3 <= 1.25, str(f3))

with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent.parent / "scratch" / "tmp")) as td:
    mi._DIR = Path(td)
    mi.CENTERS_PATH, mi.LOG_PATH = Path(td) / "c.json", Path(td) / "l.jsonl"
    mi._cache.update(ts=1e18, mods={"initiative": 0.4}, mood="calmness")
    f = mi.factor("question_cooldown", "initiative", base=600)
    check("live call returns 1.0 while learning", f == 1.0, str(f))
    rows = mi.recent()
    check("influence logged with the learning note", rows and "learning my baseline" in rows[-1].get("note", ""),
          str(rows[-1] if rows else None))
    mi._cache.update(mods={})
    check("missing modifier -> 1.0, never raises", mi.factor("x", "initiative") == 1.0)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
