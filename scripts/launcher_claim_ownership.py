"""Atomic first-owner-wins gate for the SDK launchers (Iris_fixes #4, 2026-09-30).

THE RACE (Zeke's spec): start_iris_v2.bat (Opus) and start_iris_v2_fable.bat
(Fable) both run destructive cleanup UNCONDITIONALLY — sweep 1 kills the body
host, sweep 2 kills claude.exe cognition, sweep 3 frees the ports — with NO lock
acquired first. The single-instance guards (iris.pid, watchdog mutexes) are all
DOWNSTREAM of the sweeps, and the pidfile is even deleted before relaunch. So if
one Iris is alive and a second launcher starts (an accidental Opus/Fable
double-launch), the second KILLS the first before ever discovering it lost. That
"accidentally replaces the existing Iris."

THE FIX: this script runs FIRST in each launcher, BEFORE any sweep. Under a global
named mutex (so two simultaneous launchers can't both pass), it asks: is a HEALTHY
Iris already running?
  healthy owner  = tick-loop heartbeat fresh (<120s)  AND  a claude.exe cognition
                   alive under this repo.
If yes -> this launcher LOST first-owner: exit 10, and the .bat stands down
WITHOUT sweeping (the existing Iris lives). If no healthy owner -> exit 0, the
.bat proceeds to sweep and take over (the normal restart path: the watchdog / Zeke
kills the old stack first, so no healthy owner exists at that point).

Why this correctly PERMITS restarts but BLOCKS twins: a legitimate restart kills
the old stack before relaunch, so the heartbeat is stale / cognition gone when the
new launcher runs -> exit 0. An accidental twin launches while the first is still
healthy -> exit 10. The mutex makes the check-and-decide atomic.

Exit codes (kept to 0 / 10 so `if errorlevel 10` in cmd is unambiguous):
  0  -> proceed to sweep (you are the owner, or no healthy owner exists, or the
        check errored — fail-open to preserve restart reliability)
  10 -> stand down, do NOT sweep (a healthy Iris already owns cognition)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MUTEX_NAME = r"Global\IrisCognitionOwnerClaim"
CLAIM_FILE = REPO / "state" / "cognition_owner.json"


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ownership] {msg}"
    print(line, flush=True)
    try:
        irislog = os.environ.get("IRISLOG")
        if irislog:
            with open(irislog, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


# ── the two liveness signals ──────────────────────────────────────────────────

def heartbeat_fresh(time_json: Path, max_age_s: float) -> tuple[bool, float]:
    try:
        st = json.loads(Path(time_json).read_text(encoding="utf-8"))
        last = float(st.get("last_tick_ts") or 0.0)
        if last <= 0:
            return False, -1.0
        age = time.time() - last
        return (age <= max_age_s), age
    except Exception:
        return False, -1.0


def cognition_alive(cmd_substr: str) -> bool:
    """Is a claude.exe cognition process alive whose command line contains
    cmd_substr (default the repo dir)? Uses CIM; falls back to False on error."""
    if not cmd_substr:
        return False
    try:
        import subprocess
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{cmd_substr}*' }} | "
            "Measure-Object | ForEach-Object { $_.Count }"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15)
        n = int((r.stdout or "0").strip() or "0")
        return n > 0
    except Exception:
        return False


# ── the atomic claim ──────────────────────────────────────────────────────────

def _with_mutex(hold_ms: int, fn):
    """Run fn() while holding the global named mutex. Returns fn()'s value. Fails
    open (runs fn without the mutex) only if pywin32 is entirely unavailable."""
    try:
        import win32event
        import win32api
        import winerror
    except Exception:
        _log("pywin32 unavailable — running claim WITHOUT mutex (degraded)")
        return fn()
    h = win32event.CreateMutex(None, False, MUTEX_NAME)
    # Wait up to 10s for the other launcher to finish its critical section.
    rc = win32event.WaitForSingleObject(h, 10000)
    got = rc in (win32event.WAIT_OBJECT_0, 0x00000080)  # signalled or abandoned
    try:
        if not got:
            _log("could not acquire ownership mutex within 10s — proceeding (fail-open)")
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        return fn()
    finally:
        try:
            win32event.ReleaseMutex(h)
        except Exception:
            pass
        try:
            win32api.CloseHandle(h)
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("IRIS_MODEL", "?"))
    ap.add_argument("--time-json", default=str(REPO / "state" / "iris_time.json"))
    ap.add_argument("--heartbeat-max-age", type=float, default=120.0)
    ap.add_argument("--cognition-cmd-substr", default="Wren-Companion")
    ap.add_argument("--check-only", action="store_true",
                    help="do not write the claim file (for tests)")
    ap.add_argument("--hold-mutex-ms", type=int, default=0,
                    help="hold the mutex this long (for contention tests)")
    ap.add_argument("--claim-bridge-s", type=float, default=30.0,
                    help="a claim younger than this from another launcher = stand down "
                         "(bridges the cold-start gap; short so restart retries aren't blocked)")
    ap.add_argument("--claim-file", default=None,
                    help="ownership claim path (overridable for tests)")
    ap.add_argument("--force", action="store_true",
                    help="take over even a healthy Iris (intentional replace / model flip); "
                         "also via env IRIS_FORCE_TAKEOVER=1")
    args = ap.parse_args()
    claim_path = Path(args.claim_file) if args.claim_file else CLAIM_FILE

    force = bool(args.force or os.environ.get("IRIS_FORCE_TAKEOVER") == "1")

    def _decide() -> int:
        if force:
            _log("FORCE takeover requested (--force / IRIS_FORCE_TAKEOVER=1) — "
                 "proceeding to sweep even if a healthy Iris is present")
            return 0
        hb_ok, hb_age = heartbeat_fresh(Path(args.time_json), args.heartbeat_max_age)
        cog_ok = cognition_alive(args.cognition_cmd_substr)
        owner_present = hb_ok and cog_ok
        _log(f"heartbeat_fresh={hb_ok} (age={hb_age:.1f}s) cognition_alive={cog_ok} "
             f"-> owner_present={owner_present} (model={args.model})")
        if owner_present:
            _log("a healthy Iris already owns cognition — STANDING DOWN, will NOT sweep")
            return 10
        # Cold-start bridge: the healthy-owner check misses the gap where a FIRST
        # launcher has claimed but its body host isn't up yet (heartbeat not fresh,
        # cognition not alive). A second launcher racing in that gap would proceed
        # and twin. So: a FRESH claim from another launcher (younger than the short
        # bridge window) also means "stand down". The window is deliberately short
        # (default 30s) so a legitimate restart RETRY after a failed cold start is
        # NOT blocked — only near-simultaneous double-launches (which hit this
        # preflight within ~1-2s of each other) fall inside it.
        try:
            if claim_path.exists():
                claim = json.loads(claim_path.read_text(encoding="utf-8"))
                age = time.time() - float(claim.get("ts") or 0.0)
                other = int(claim.get("pid") or 0) != os.getpid()
                if other and 0 <= age <= args.claim_bridge_s:
                    _log(f"a fresh ownership claim exists (age={age:.1f}s, pid={claim.get('pid')}, "
                         f"model={claim.get('model')}) — another launcher is mid-startup; "
                         f"STANDING DOWN, will NOT sweep")
                    return 10
        except Exception as e:
            _log(f"claim-bridge read failed (non-fatal, proceeding): {e!r}")
        if not args.check_only:
            try:
                claim_path.parent.mkdir(parents=True, exist_ok=True)
                claim_path.write_text(json.dumps({
                    "pid": os.getpid(), "model": args.model, "ts": time.time(),
                    "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }), encoding="utf-8")
            except Exception as e:
                _log(f"claim-file write failed (non-fatal): {e!r}")
        _log("no healthy owner — CLAIMED ownership, proceeding to sweep")
        return 0

    try:
        return _with_mutex(args.hold_mutex_ms, _decide)
    except Exception as e:
        _log(f"ownership check errored ({e!r}) — proceeding (fail-open) to not leave Iris down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
