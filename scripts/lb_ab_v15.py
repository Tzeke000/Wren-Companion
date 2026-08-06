"""lb_ab_v15 — drive the full v12-vs-v15 battery A/B, same session (2026-08-01).

Protocol per v15_plan_2026-07-28 + the HARD RULE from the v12-instability
finding: baseline is RE-MEASURED in the same session/body state, never taken
from a stored table. 5 runs v12 -> swap :8772 to v15 -> 5 runs v15 -> restore
v12. After every server swap the served model is VERIFIED from the flight
recorder (a battery that silently hits the wrong model must abort, not score).

Production env (user-level IRIS_LOCAL_MODEL=iris-little-v12) is never touched;
the v15 server gets its env only in its own spawned process.

Run:  .venv\\Scripts\\python.exe -u scripts\\lb_ab_v15.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

REPO = Path(r"D:\Wren-Companion")
PY = REPO / ".venv" / "Scripts" / "python.exe"
SERVER = REPO / "scripts" / "vector_brain_server.py"
BATTERY = REPO / "scripts" / "lb_test_battery.py"
LOG_DIR = REPO / "state" / "little_brain" / "turn_log"
RUNS = 5
_DETACHED = 0x00000008 | 0x00000200


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def kill_server() -> None:
    cmd = (r"Get-CimInstance Win32_Process | "
           r"Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'vector_brain_server' } | "
           r"ForEach-Object { Write-Output $_.ProcessId; "
           r"Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=60)
    log(f"killed server PIDs: {r.stdout.strip().split() or 'none'}")


def health_ok() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8772/health", timeout=5) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:
        return False


def spawn_server(model: str) -> None:
    env = dict(os.environ)
    env["IRIS_LOCAL_MODEL"] = model
    env["IRIS_LB_TOOLS"] = "1"
    subprocess.Popen([str(PY), str(SERVER)], cwd=str(REPO), env=env,
                     creationflags=_DETACHED, close_fds=True)
    t0 = time.time()
    while time.time() - t0 < 60:
        if health_ok():
            log(f"server up (model={model})")
            return
        time.sleep(2)
    raise RuntimeError(f"server for {model} never came healthy")


def served_model() -> str:
    """Ask one throwaway question, read the newest flight record's model."""
    body = json.dumps({"model": "iris", "messages": [
        {"role": "user", "content": "say ok"}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:8772/v1/chat/completions",
                                 data=body, headers={
                                     "Content-Type": "application/json",
                                     "x-iris-local-only": "1"})
    try:
        with urllib.request.urlopen(req, timeout=170) as r:
            r.read()
    except Exception as e:
        log(f"probe ask failed: {e!r}")
    time.sleep(1.5)
    p = LOG_DIR / (time.strftime("%Y-%m-%d") + ".jsonl")
    try:
        last = json.loads(p.read_text(encoding="utf-8").splitlines()[-1])
        return str(last.get("model") or "unknown")
    except Exception:
        return "unknown"


def swap_to(model: str) -> None:
    kill_server()
    time.sleep(3)
    spawn_server(model)
    m = served_model()
    if m != model:
        raise RuntimeError(f"ABORT: server says model={m}, wanted {model}")
    log(f"verified served model = {m}")


def run_battery(model: str, n: int) -> None:
    env = dict(os.environ)
    env["IRIS_LOCAL_MODEL"] = model
    for i in range(1, n + 1):
        log(f"battery run {i}/{n} for {model}...")
        r = subprocess.run([str(PY), "-u", str(BATTERY)], cwd=str(REPO),
                           env=env, capture_output=True, text=True,
                           timeout=60 * 40)
        tail = "\n".join((r.stdout or "").splitlines()[-3:])
        log(f"run {i} done rc={r.returncode}\n{tail}")


def main() -> int:
    # Current server should already be v12 (production env); verify, don't assume.
    if not health_ok():
        log("no server on :8772 — spawning production v12")
        spawn_server("iris-little-v12")
    m = served_model()
    log(f"pre-baseline served model: {m}")
    if m != "iris-little-v12":
        swap_to("iris-little-v12")

    log("=== PHASE 1: v12 baseline, same session ===")
    run_battery("iris-little-v12", RUNS)

    log("=== PHASE 2: swap to v15 ===")
    swap_to("iris-little-v15")
    run_battery("iris-little-v15", RUNS)

    log("=== PHASE 3: restore v12 (production) ===")
    swap_to("iris-little-v12")
    log("A/B COMPLETE — battery files in state/little_brain/, "
        "aggregate with lb_battery_aggregate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
