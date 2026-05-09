"""Benchmark AppDiscoverer.discover_all wall-clock time.

Each run instantiates a FRESH AppDiscoverer pointed at a tempdir so it
does not conflict with the running Ava's state, and so that nothing is
loaded from a previously-saved registry. The scan itself walks the real
filesystem.

Usage:
  py -3.11 scripts/bench_app_discovery.py [--runs N]

Note on cold vs warm cache: Windows file-system cache stays warm across
process boundaries, so consecutive runs after a fresh reboot will get
faster as cached directory metadata accrues. To get a true cold-cache
number, reboot then run this once.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.app_discoverer import AppDiscoverer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark app discovery.")
    parser.add_argument("--runs", type=int, default=3, help="Number of runs (default 3)")
    args = parser.parse_args()

    times: list[float] = []
    for i in range(args.runs):
        with tempfile.TemporaryDirectory() as td:
            disc = AppDiscoverer(Path(td))
            t0 = time.time()
            n = disc.discover_all()
            elapsed = time.time() - t0
            times.append(elapsed)
            print(f"[bench] run {i + 1}/{args.runs}: {elapsed:.2f}s  ({n} entries)")

    if times:
        print()
        print(
            f"[bench] min={min(times):.2f}s  max={max(times):.2f}s  "
            f"avg={sum(times) / len(times):.2f}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
