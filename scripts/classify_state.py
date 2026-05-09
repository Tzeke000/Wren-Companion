"""Classify state/ files. Run: py -3.11 scripts/classify_state.py"""
from __future__ import annotations
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.state_classification import report  # noqa: E402


def main() -> int:
    state_dir = ROOT / "state"
    if not state_dir.is_dir():
        print(f"No state dir at {state_dir}")
        return 1
    listing = sorted(os.listdir(state_dir))
    r = report(listing)
    print(f"\n--- state/ classification ---")
    print(f"Persistent:   {len(r['persistent'])} entries — survives restart, must be backed up")
    print(f"Ephemeral:    {len(r['ephemeral'])} entries — runtime-only, safe to clear anytime")
    print(f"Derived:      {len(r['derived'])} entries — regeneratable from canonical sources")
    print(f"Unclassified: {len(r['unclassified'])} entries — UNKNOWN, please classify in brain/state_classification.py")
    print()
    if r["unclassified"]:
        print("UNCLASSIFIED entries (action required — add to brain/state_classification.py):")
        for e in r["unclassified"]:
            print(f"  - {e}")
        print()
    print("Persistent entries (survive restart):")
    for e in r["persistent"]:
        print(f"  - {e}")
    print()
    print("Ephemeral entries (safe to clear):")
    for e in r["ephemeral"]:
        print(f"  - {e}")
    print()
    print("Derived entries (regeneratable):")
    for e in r["derived"]:
        print(f"  - {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
