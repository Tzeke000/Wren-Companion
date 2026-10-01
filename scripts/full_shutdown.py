"""full_shutdown.py - take the whole Iris stack down ON PURPOSE (2026-10-01, Zeke: "for now shut down").

Order matters:
  1. restart_switch OFF with a reason  -> the watchdog stands down instead of resurrecting the stack
  2. kill the watchdog(s)              -> nothing left that respawns
  3. kill the launcher shells          -> cmd.exe /c start_iris*.bat
  4. kill the stack                    -> claude.exe, body host, runtime, voice daemon + mouth, orb app,
                                          runtime watchdog/detector, vector daemons, little pilot
LEFT RUNNING on purpose: post-office :5877 (Wren's lifeline), the Iris-Zeke-Presence task, wire-pod (Vector's
voice), Ollama, this shell. Real shutdowns need --arm (dry run otherwise). Logs to state/full_shutdown.log.
Process work is done with psutil - never taskkill from Git Bash (its /PID gets path-mangled, scar 10-01).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
import time

import psutil

ROOT = r"D:\Wren-Companion"
LOG = os.path.join(ROOT, "state", "full_shutdown.log")
SWITCH = os.path.join(ROOT, "scripts", "restart_switch.ps1")
CREATE_NO_WINDOW = 0x08000000

KEEP_MARKERS = ("postoffice", "post_office", "zeke_presence", "chipper", "ollama", "full_shutdown.py")

# (label, predicate on (name_lower, cmdline_lower)) - evaluated in order
STAGES = [
    ("watchdog.ps1",      lambda n, c: n.startswith("powershell") and "iris_watchdog.ps1" in c),
    ("launcher cmd",      lambda n, c: n == "cmd.exe" and "start_iris" in c),
    ("claude.exe",        lambda n, c: n == "claude.exe"),
    ("body host",         lambda n, c: "iris_body_host.py" in c),
    ("runtime watchdog",  lambda n, c: "iris_runtime_watch" in c),
    ("runtime",           lambda n, c: "iris_runtime.py" in c),
    ("voice daemon",      lambda n, c: "wren_voice_daemon.py" in c),
    ("mouth",             lambda n, c: "styletts" in c and ".py" in c),
    ("orb app",           lambda n, c: n.startswith("iris-control") or ("iris-control" in c and "tauri" in c)),
    ("vector nerves",     lambda n, c: "vector_inhabit_daemon.py" in c),
    ("vector brain",      lambda n, c: "vector_brain_server.py" in c),
    ("little pilot",      lambda n, c: "little_pilot.py" in c),
]


def log(msg: str) -> None:
    line = "[" + _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] " + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def matches(stage, p) -> bool:
    try:
        n = (p.info.get("name") or "").lower()
        c = " ".join(p.info.get("cmdline") or []).lower()
    except Exception:
        return False
    if p.pid == os.getpid() or any(k in c for k in KEEP_MARKERS):
        return False
    return bool(stage[1](n, c))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="store_true", help="actually kill; without it this is a dry run")
    ap.add_argument("--reason", default="Zeke asked for a shutdown")
    a = ap.parse_args()
    log("=== full_shutdown " + ("ARMED" if a.arm else "DRY RUN") + " - " + a.reason)

    if a.arm:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SWITCH, "off", a.reason],
                           capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        log("restart_switch off -> rc=" + str(r.returncode) + " " + (r.stdout or "").strip().replace("\n", " | ")[:200])

    for stage in STAGES:
        victims = [p for p in psutil.process_iter(["pid", "name", "cmdline"]) if matches(stage, p)]
        for p in victims:
            log(("kill " if a.arm else "would kill ") + stage[0] + " pid " + str(p.pid))
            if a.arm:
                try:
                    p.kill()
                except Exception as e:
                    log("  kill failed: " + repr(e))
        if a.arm and victims:
            time.sleep(1.0)

    if a.arm:
        time.sleep(2.0)
        left = [p.pid for p in psutil.process_iter(["pid", "name", "cmdline"])
                if any(matches(s, p) for s in STAGES)]
        log("=== sweep done. left running on purpose: post-office, presence task, wire-pod, ollama. "
            + ("stragglers: " + str(left) if left else "no stragglers") + ". goodnight. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
