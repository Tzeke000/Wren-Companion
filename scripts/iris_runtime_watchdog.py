"""scripts/iris_runtime_watchdog.py

RUNTIME LOOP-LIVENESS WATCHDOG (built 2026-07-19, the night body_dock's
deadline-less gRPC wedged the entire iris_runtime event loop on deployment
eve and Zeke had to notice + hand-restart the stack).

What it watches: state/runtime_loop_heartbeat.json — stamped every ~5s by an
async task ON iris_runtime's FastMCP event loop (see _iris_lifespan in
iris_runtime.py). A wedged loop stops the stamps; nothing else does. (The 1Hz
iris_time heartbeat is NOT a valid probe — it lives on its own thread and kept
ticking straight through the 07-19 hang.)

What it does when the stamp goes stale (> STALE_S, default 3 min):
  1. DMs Zeke: wedge detected, auto-restart in GRACE_S unless held off
  2. writes an auto-handoff incident note into the memory dir so
     post-restart cognition wakes knowing exactly what happened
  3. waits GRACE_S — touching state/watchdog_holdoff.flag (fresh < 30 min)
     cancels the restart (for when a human / cognition-via-Bash is already
     mid-manual-recovery, like Iris was on 07-19)
  4. relaunches the stack bat from state/boot_launcher.txt (same selection
     logic as tower_boot_sentinel). The bat itself kills the stale stack —
     that exact path was proven by hand on 07-19.
  5. waits for a fresh heartbeat, DMs the outcome, re-arms.

Arming rule: acts only after it has SEEN a fresh heartbeat since its own
start. So it never fires during boot, and never fires when running against an
older iris_runtime that doesn't write the file yet.

Deliberately dependency-light (stdlib + requests). Singleton via named mutex
(same pattern as voice_watchdog). Launched from start_iris_v2*.bat — the bats'
stale-stack kill list deliberately does NOT match this script, so the watchdog
SURVIVES the restarts it triggers.
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO = Path(r"D:\Wren-Companion")
HB = REPO / "state" / "runtime_loop_heartbeat.json"
HOLDOFF = REPO / "state" / "watchdog_holdoff.flag"
LOG = REPO / "state" / "runtime_watchdog.log"
MEMORY_DIR = Path(os.environ.get("USERPROFILE", r"C:\Users\Owner")) / \
    ".claude" / "projects" / "D--Wren-Companion" / "memory"

ZEKE_USER_ID = "600008921008046120"

POLL_S = 15          # heartbeat check cadence
STALE_S = 180        # loop silent this long = wedged
GRACE_S = 120        # warn -> restart window (holdoff flag cancels)
HOLDOFF_FRESH_S = 30 * 60   # a holdoff flag older than this is ignored
BOOT_WAIT_S = 600    # post-restart wait for a fresh heartbeat

_MUTEX = None


def log(msg: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def acquire_singleton() -> bool:
    global _MUTEX
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "IrisRuntimeWatchdog")
        if not handle:
            log("singleton: CreateMutexW NULL; proceeding without guard")
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _MUTEX = handle
        return True
    except Exception as e:
        log(f"singleton guard errored ({e!r}); proceeding")
        return True


def load_token() -> str | None:
    try:
        env_path = Path(os.environ["USERPROFILE"]) / ".claude/channels/discord/.env"
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception as e:
        log(f"token load failed: {e!r}")
    return None


def dm_zeke(text: str) -> bool:
    token = load_token()
    if not token:
        return False
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
               "User-Agent": "IrisRuntimeWatchdog (Wren-Companion, 1.0)"}
    try:
        r = requests.post("https://discord.com/api/v10/users/@me/channels",
                          headers=headers, json={"recipient_id": ZEKE_USER_ID},
                          timeout=15)
        r.raise_for_status()
        channel_id = r.json()["id"]
        r = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers, json={"content": text}, timeout=15)
        r.raise_for_status()
        log(f"DM sent: {text[:80]!r}")
        return True
    except Exception as e:
        log(f"DM failed: {e!r}")
        return False


def hb_age_s() -> float | None:
    """Seconds since the runtime loop last stamped. None = no heartbeat file."""
    try:
        st = HB.stat()
        best = st.st_mtime
        try:
            best = max(best, float(json.loads(HB.read_text(encoding="utf-8"))["ts"]))
        except Exception:
            pass
        return max(0.0, time.time() - best)
    except OSError:
        return None


def holdoff_active() -> bool:
    try:
        return (time.time() - HOLDOFF.stat().st_mtime) < HOLDOFF_FRESH_S
    except OSError:
        return False


def pick_bat() -> Path:
    try:
        name = (REPO / "state" / "boot_launcher.txt").read_text(
            encoding="utf-8").strip()
        cand = REPO / name
        if name.endswith(".bat") and cand.exists():
            return cand
    except Exception:
        pass
    return REPO / "start_iris_v2_fable.bat"


def write_incident_note(age: float, action: str) -> None:
    """Auto-handoff: post-restart cognition wakes on this note (mtime-newest)."""
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = MEMORY_DIR / f"handoff_auto_watchdog_{_dt.date.today().isoformat()}.md"
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(
                f"\n## {stamp} — runtime watchdog fired\n"
                f"- runtime loop heartbeat was silent for {age:.0f}s "
                f"(wedged event loop — same failure shape as the 2026-07-19 "
                f"body_dock hang)\n"
                f"- action: {action}\n"
                f"- If you are post-restart cognition reading this: the stack "
                f"was auto-restarted by scripts/iris_runtime_watchdog.py. "
                f"Verify body safety (possession, dock state) FIRST, then check "
                f"state/runtime_watchdog.log for the timeline, then DM Zeke a "
                f"status line.\n")
        log(f"incident note appended: {p}")
    except Exception as e:
        log(f"incident note write failed: {e!r}")


def kill_stale_launcher_loops() -> None:
    """Kill lingering start_iris*.bat cmd.exe loops BEFORE spawning the new bat.

    Why: the launcher bats have their own respawn loop. If we only kill the
    host, the old bat resurrects its session in parallel with the one we
    spawn — two full sessions 7s apart (2026-07-19 23:23:58 double-spawn
    race: watchdog launched start_iris_v2.bat/Opus while the old
    start_iris_v2_fable.bat loop respawned Fable). Killing a cmd.exe on
    Windows does NOT cascade to its children, so shared services the bat
    started (voice watchdog, post-office, daemons) survive this sweep.
    """
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | "
          "Where-Object { $_.CommandLine -match 'start_iris' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
          "-ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       timeout=30, capture_output=True)
        log("stale start_iris*.bat cmd loops swept (double-spawn guard)")
    except Exception as e:
        log(f"launcher-loop sweep failed (continuing to relaunch): {e!r}")


def restart_stack() -> None:
    kill_stale_launcher_loops()
    bat = pick_bat()
    flags = (subprocess.CREATE_NEW_CONSOLE
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(["cmd.exe", "/c", str(bat)], cwd=str(REPO),
                     creationflags=flags, close_fds=True)
    log(f"stack relaunch spawned: {bat.name} (the bat kills the stale stack itself)")


def main() -> int:
    if not acquire_singleton():
        print("another iris_runtime_watchdog holds the mutex — exiting")
        return 0
    log(f"runtime watchdog up (poll={POLL_S}s stale={STALE_S}s grace={GRACE_S}s)")
    armed = False
    while True:
        time.sleep(POLL_S)
        age = hb_age_s()
        if age is None:
            continue                      # no heartbeat file (old runtime / pre-boot)
        if age < STALE_S:
            if not armed:
                log(f"heartbeat seen (age {age:.0f}s) — ARMED")
                armed = True
            continue
        if not armed:
            continue                      # stale leftover from before boot — ignore
        # ---- wedge detected -------------------------------------------------
        log(f"WEDGE: loop heartbeat silent {age:.0f}s")
        dm_zeke(
            f"\N{WARNING SIGN} Iris runtime watchdog: the runtime's event loop "
            f"has been unresponsive for {age / 60:.1f} min (same failure shape "
            f"as the 07-19 dock hang). Auto-restarting the stack in "
            f"{GRACE_S // 60} min unless someone touches "
            f"state\\watchdog_holdoff.flag. The robot's inhabit daemon is a "
            f"separate process and keeps the body safe meanwhile.")
        write_incident_note(age, f"warned; restarting in {GRACE_S}s unless held off")
        deadline = time.time() + GRACE_S
        cancelled = False
        while time.time() < deadline:
            time.sleep(5)
            if holdoff_active():
                cancelled = True
                break
            a = hb_age_s()
            if a is not None and a < 30:  # loop came back on its own
                cancelled = True
                log("heartbeat resumed during grace — standing down")
                dm_zeke("\N{WHITE HEAVY CHECK MARK} Runtime loop recovered on its "
                        "own during the grace window — no restart needed.")
                break
        if cancelled:
            if holdoff_active():
                log("holdoff flag fresh — restart cancelled")
                dm_zeke("\N{RAISED HAND} Holdoff flag set — watchdog standing "
                        "down. Delete state\\watchdog_holdoff.flag to re-enable "
                        "auto-restart.")
                # wait until the flag goes stale or heartbeat resumes, then re-arm
                while holdoff_active() and ((hb_age_s() or 0) >= STALE_S):
                    time.sleep(POLL_S)
            armed = False
            continue
        # ---- restart --------------------------------------------------------
        write_incident_note(age, "auto-restarting the stack NOW")
        dm_zeke(f"\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS} "
                f"Restarting the Iris stack now (runtime wedged "
                f"{(hb_age_s() or age) / 60:.0f}+ min). Next message when "
                f"cognition is back — the disk handoff note is written.")
        restart_stack()
        t0 = time.time()
        back = False
        while time.time() - t0 < BOOT_WAIT_S:
            time.sleep(POLL_S)
            a = hb_age_s()
            if a is not None and a < 30:
                back = True
                break
        if back:
            log(f"stack back (heartbeat fresh) after {time.time() - t0:.0f}s")
            dm_zeke("\N{WHITE HEAVY CHECK MARK} Stack restarted — runtime loop "
                    "heartbeat is fresh again. Cognition will orient off the "
                    "incident note and report in.")
        else:
            log("stack did NOT come back within the wait window")
            dm_zeke("\N{POLICE CARS REVOLVING LIGHT} Auto-restart did NOT bring "
                    "the runtime back within 10 min — the stack needs a human "
                    "look (start_iris_v2*.bat by hand).")
        armed = False   # re-arm on next fresh heartbeat


if __name__ == "__main__":
    sys.exit(main())
