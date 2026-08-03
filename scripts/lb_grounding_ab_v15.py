# SELF_ASSESSMENT: thin driver — swap :8772 to v15, run the grounding probe
# (same-session v12 baseline already measured 2026-08-03 09:04), restore v12
# in a finally so production is never left on v15 even on crash.
"""Run: .venv\\Scripts\\python.exe -u scripts\\lb_grounding_ab_v15.py"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(r"D:\Wren-Companion")
sys.path.insert(0, str(REPO / "scripts"))
from lb_ab_v15 import PY, log, swap_to  # noqa: E402

PROBE = REPO / "scripts" / "lb_grounding_probe.py"


def main() -> int:
    try:
        log("=== swap to v15 ===")
        swap_to("iris-little-v15")
        env = dict(os.environ)
        env["IRIS_LOCAL_MODEL"] = "iris-little-v15"
        log("=== grounding probe x5 on v15 ===")
        r = subprocess.run([str(PY), "-u", str(PROBE), "5"], cwd=str(REPO),
                           env=env, timeout=60 * 45)
        log(f"probe rc={r.returncode}")
        return r.returncode
    finally:
        log("=== restore v12 (production) ===")
        swap_to("iris-little-v12")
        log("restored + verified v12")


if __name__ == "__main__":
    raise SystemExit(main())
