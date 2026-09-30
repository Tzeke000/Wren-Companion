"""Adversarial test for the first-owner-wins launcher gate (Iris_fixes #4).

Proves the gate stands down (exit 10) when a healthy Iris or a mid-startup twin
already owns cognition, and proceeds (exit 0) on a legitimate restart. Runs the
real script as a subprocess with injected signals — no real process is killed.

Run: .venv/Scripts/python.exe scripts/test_launcher_ownership.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "Scripts" / "python.exe")
SCRIPT = str(REPO / "scripts" / "launcher_claim_ownership.py")
PASS, FAIL = [], []


def run(args):
    r = subprocess.run([PY, SCRIPT, *args], capture_output=True, text=True, timeout=60)
    return r.returncode


def check(name, got, expected):
    ok = got == expected
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: exit {got}, expected {expected}")


def _tmp(content=None, suffix=".json"):
    fd, p = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    if content is not None:
        Path(p).write_text(json.dumps(content), encoding="utf-8")
    else:
        os.unlink(p)
    return Path(p)


fresh_time = _tmp({"last_tick_ts": time.time()})
stale_time = _tmp({"last_tick_ts": 1.0})
NONE = "ZZZ_NO_SUCH_COGNITION"

print("== 1: healthy owner present (fresh heartbeat + real claude.exe) -> STAND DOWN (10) ==")
# cognition substr = the real repo dir, so it detects the live me.
claim1 = _tmp();
check("healthy-owner -> stand down",
      run(["--check-only", "--time-json", str(fresh_time),
           "--cognition-cmd-substr", "Wren-Companion", "--claim-file", str(claim1)]),
      10)

print("\n== 2: no healthy owner (stale heartbeat, no cognition), no claim -> PROCEED (0) ==")
claim2 = _tmp()
check("no-owner -> proceed",
      run(["--check-only", "--time-json", str(stale_time),
           "--cognition-cmd-substr", NONE, "--claim-file", str(claim2)]),
      0)

print("\n== 3: cold-start bridge — a FRESH claim from another pid -> STAND DOWN (10) ==")
claim3 = _tmp({"pid": 999999, "model": "claude-opus-5", "ts": time.time()})
check("fresh-twin-claim -> stand down",
      run(["--check-only", "--time-json", str(stale_time),
           "--cognition-cmd-substr", NONE, "--claim-file", str(claim3),
           "--claim-bridge-s", "30"]),
      10)

print("\n== 4: STALE claim (older than bridge window) -> PROCEED (restart retry not blocked) ==")
claim4 = _tmp({"pid": 999999, "model": "claude-opus-5", "ts": time.time() - 300})
check("stale-claim -> proceed",
      run(["--check-only", "--time-json", str(stale_time),
           "--cognition-cmd-substr", NONE, "--claim-file", str(claim4),
           "--claim-bridge-s", "30"]),
      0)

print("\n== 5: simultaneous double-launch simulation ==")
# First launcher (NOT check-only) claims into a shared file; second sees the fresh
# claim and stands down. The first uses a different pid than the second (subprocess),
# so the 'other pid' guard holds.
shared = _tmp()
rc_first = run(["--time-json", str(stale_time), "--cognition-cmd-substr", NONE,
                "--claim-file", str(shared), "--claim-bridge-s", "30"])
check("first-of-two -> proceeds & claims", rc_first, 0)
check("claim file written", Path(shared).exists(), True)
rc_second = run(["--check-only", "--time-json", str(stale_time), "--cognition-cmd-substr", NONE,
                 "--claim-file", str(shared), "--claim-bridge-s", "30"])
check("second-of-two -> stands down", rc_second, 10)

for p in (fresh_time, stale_time, claim1, claim2, claim3, claim4, shared):
    try:
        Path(p).unlink()
    except Exception:
        pass

print("\n" + "=" * 50)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("FIRST-OWNER-WINS HELD — a second launcher cannot kill a live or mid-startup Iris.")
