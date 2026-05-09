"""
Phase 47 — Watchdog restart system.

Lightweight process that:
1. Watches state/restart_requested.flag
2. Reads state/pickup_note.json for restart context
3. Kills avaagent.py by PID from state/ava.pid
4. Waits 3 seconds then restarts avaagent.py
5. Polls http://127.0.0.1:5876 until online
6. Logs restart event to state/restart_log.jsonl

Run alongside avaagent.py. Does not import avaagent — stays lightweight.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
STATE_DIR = BASE_DIR / "state"
FLAG_FILE = STATE_DIR / "restart_requested.flag"
PID_FILE = STATE_DIR / "ava.pid"
PICKUP_FILE = STATE_DIR / "pickup_note.json"
LOG_FILE = STATE_DIR / "restart_log.jsonl"
OPERATOR_URL = "http://127.0.0.1:5876/api/v1/snapshot"

POLL_INTERVAL = 2.0
KILL_WAIT = 3.0
ONLINE_TIMEOUT = 60.0
ONLINE_POLL = 2.0


def _read_pid() -> int | None:
    if not PID_FILE.is_file():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _read_pickup() -> dict:
    if not PICKUP_FILE.is_file():
        return {}
    try:
        return json.loads(PICKUP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _log_restart(event: str, reason: str, pid_killed: int | None, new_pid: int | None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "dt": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "reason": reason[:300],
        "pid_killed": pid_killed,
        "new_pid": new_pid,
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _kill_process(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1.0)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return True
    except Exception as ex:
        print(f"[watchdog] kill {pid} failed: {ex!r}")
        return False


def _wait_online(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(OPERATOR_URL, timeout=2.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(ONLINE_POLL)
    return False


def _start_ava() -> subprocess.Popen | None:
    py = sys.executable
    agent_path = BASE_DIR / "avaagent.py"
    if not agent_path.is_file():
        print(f"[watchdog] avaagent.py not found at {agent_path}")
        return None
    try:
        proc = subprocess.Popen(
            [py, str(agent_path)],
            cwd=str(BASE_DIR),
        )
        print(f"[watchdog] started avaagent.py PID={proc.pid}")
        return proc
    except Exception as ex:
        print(f"[watchdog] start failed: {ex!r}")
        return None


def _do_restart() -> None:
    reason = ""
    try:
        reason = FLAG_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    pickup = _read_pickup()
    pid = _read_pid()

    print(f"[watchdog] restart triggered. reason={reason!r} pid={pid}")
    _log_restart("restart_triggered", reason, pid, None)

    # Remove flag first to avoid re-triggering
    try:
        FLAG_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    if pid:
        print(f"[watchdog] killing PID {pid}…")
        _kill_process(pid)
        time.sleep(KILL_WAIT)

    # Clean up PID file
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    proc = _start_ava()
    if proc is None:
        _log_restart("restart_failed", "could not start avaagent.py", pid, None)
        return

    print(f"[watchdog] waiting for :5876 to come online…")
    online = _wait_online(ONLINE_TIMEOUT)
    if online:
        print(f"[watchdog] Ava is online. PID={proc.pid}")
        _log_restart("restart_completed", reason, pid, proc.pid)
    else:
        print(f"[watchdog] Ava did not come online within {ONLINE_TIMEOUT}s")
        _log_restart("restart_timeout", reason, pid, proc.pid)


def run() -> None:
    print(f"[watchdog] started. watching {FLAG_FILE}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            if FLAG_FILE.is_file():
                _do_restart()
        except Exception as ex:
            print(f"[watchdog] error in restart loop: {ex!r}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
