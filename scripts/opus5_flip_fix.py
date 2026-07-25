#!/usr/bin/env python3
"""One-shot self-surgery: finish the Opus-5 flip + cull the double-spawn twin.

WHY THIS EXISTS (2026-07-25 ~02:3x, Zeke greenlit on Discord)
------------------------------------------------------------
The 07-25 flip repinned start_iris_v2.bat to `claude-opus-5`, but the flip did
NOT actually land, for two independent reasons:

  1. `npm`/`claude update` moved the CLI **on PATH** to 2.1.220. The Agent SDK
     does not use PATH -- it launches its own
     `.venv/Lib/site-packages/claude_agent_sdk/_bundled/claude.exe`, which was
     still **2.1.191**, i.e. 28 releases below the 2.1.219 floor where
     `claude-opus-5` was introduced. So the new pin was being served by a
     binary that predates the model.
     -> FIX: upgrade the python package. claude-agent-sdk 0.2.128 ships a
        bundled claude.exe at 2.1.220 (VERIFIED by staging it to a temp dir
        before writing this script -- not assumed).

  2. A stale `start_iris_v2.bat` cmd-loop survived the 01:55 watchdog
     auto-restart, so at 02:22 TWO hosts spawned 5s apart: one on the old
     `claude-opus-4-8[1m]` env, one on `claude-opus-5`. The 4.8 twin's
     iris_runtime won the race for :5876, which is why the opus-5 session
     came up with **no iris MCP at all** (6/6 attach attempts -> "Connection
     closed") -- no voice, no body, no memory tools.

WHY IT MUST BE A DETACHED SCRIPT
--------------------------------
Windows locks running .exe files. `_bundled/claude.exe` is the binary of the
very session issuing the fix, so `pip install -U` cannot overwrite it from
inside that session. The only correct order is
kill-everything -> upgrade -> relaunch, which means the thing driving it has
to outlive its own parent. Killing a cmd.exe/host on Windows does NOT cascade
to children (see kill_stale_launcher_loops in iris_runtime_watchdog.py), so a
detached child survives the sweep that kills its ancestors.

SAFETY / defensive-fallback (no-worse-than-before)
--------------------------------------------------
If the pip upgrade fails, or the upgraded bundled CLI is still below the
2.1.219 floor, the script REVERTS the model pin in start_iris_v2.bat back to
`claude-opus-4-8[1m]` (the known-good pin) before relaunching. The failure
mode is "boots on 4.8 exactly like before", never "does not boot".

Run:  .venv\Scripts\python.exe scripts\opus5_flip_fix.py
      (normally spawned detached; it logs to state\opus5_flip_fix.log)
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(r"D:\Wren-Companion")
LOG = REPO / "state" / "opus5_flip_fix.log"
BAT = REPO / "start_iris_v2.bat"
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
BUNDLED = (REPO / ".venv" / "Lib" / "site-packages" / "claude_agent_sdk"
           / "_bundled" / "claude.exe")
HOLDOFF = REPO / "state" / "watchdog_holdoff.flag"
HEARTBEAT = REPO / "state" / "runtime_loop_heartbeat.json"

FLOOR = (2, 1, 219)              # claude-opus-5 landed in Claude Code 2.1.219
GOOD_PIN = b'claude-opus-5'
FALLBACK_PIN = b'claude-opus-4-8[1m]'

sys.path.insert(0, str(REPO / "scripts"))


def log(msg: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def dm(text: str) -> None:
    """Reuse the watchdog's proven Discord DM path rather than rebuilding it."""
    try:
        from iris_runtime_watchdog import dm_zeke  # type: ignore
        dm_zeke(text)
    except Exception as e:
        log(f"DM unavailable ({e!r}) -- continuing, log is the record")


def ps(cmd: str, timeout: int = 60) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, timeout=timeout, text=True)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        log(f"powershell failed: {e!r}")
        return ""


def cli_version(exe: Path) -> tuple[int, int, int] | None:
    if not exe.exists():
        return None
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True,
                           timeout=60, text=True)
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", (r.stdout or "") + (r.stderr or ""))
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except Exception as e:
        log(f"version probe failed: {e!r}")
        return None


def vstr(v: tuple[int, int, int] | None) -> str:
    return ".".join(map(str, v)) if v else "unknown"


# ---------------------------------------------------------------- steps

def sweep_launcher_loops() -> None:
    """MUST run before killing hosts, or the bats just respawn what we kill."""
    out = ps("Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | "
             "Where-Object { $_.CommandLine -match 'start_iris' } | "
             "ForEach-Object { Write-Output ('killing loop ' + $_.ProcessId); "
             "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    log(f"launcher-loop sweep: {out.strip() or 'none found'}")


def kill_hosts() -> None:
    out = ps("Get-CimInstance Win32_Process | Where-Object { "
             "$_.Name -eq 'claude.exe' -or "
             "$_.CommandLine -match 'iris_body_host\\.py' -or "
             "$_.CommandLine -match 'iris_runtime\\.py' } | "
             "ForEach-Object { Write-Output ('killing ' + $_.ProcessId + ' ' + $_.Name); "
             "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    log(f"host kill: {out.strip() or 'nothing matched'}")


def wait_unlocked(timeout_s: int = 60) -> bool:
    """Windows holds an exclusive lock on a running .exe; wait it out."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with BUNDLED.open("ab"):
                log("bundled claude.exe is unlocked -- safe to upgrade")
                return True
        except FileNotFoundError:
            log("bundled claude.exe missing (fresh install will place it)")
            return True
        except PermissionError:
            time.sleep(2)
        except Exception as e:
            log(f"lock probe oddity ({e!r}) -- assuming unlocked")
            return True
    log("WARNING: bundled claude.exe still locked after wait")
    return False


def upgrade_sdk() -> bool:
    log("pip install -U claude-agent-sdk ...")
    try:
        r = subprocess.run([str(VENV_PY), "-m", "pip", "install", "-U",
                            "claude-agent-sdk"], capture_output=True,
                           timeout=600, text=True)
    except Exception as e:
        log(f"pip crashed: {e!r}")
        return False
    tail = "\n".join([ln for ln in (r.stdout or "").splitlines()
                      if "Successfully" in ln or "Installing" in ln][-4:])
    log(f"pip rc={r.returncode} {tail.strip()}")
    if r.returncode != 0:
        log(f"pip stderr: {(r.stderr or '')[-600:]}")
        return False
    return True


def set_pin(pin: bytes) -> None:
    """Byte-level rewrite so the .bat's original encoding survives untouched."""
    try:
        raw = BAT.read_bytes()
        for old in (GOOD_PIN, FALLBACK_PIN):
            if old != pin and b'IRIS_MODEL=' + old in raw:
                raw = raw.replace(b'IRIS_MODEL=' + old, b'IRIS_MODEL=' + pin)
        BAT.write_bytes(raw)
        log(f"model pin set to {pin.decode()}")
    except Exception as e:
        log(f"pin rewrite FAILED: {e!r}")


def relaunch() -> None:
    flags = (subprocess.CREATE_NEW_CONSOLE
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(["cmd.exe", "/c", str(BAT)], cwd=str(REPO),
                     creationflags=flags, close_fds=True)
    log("stack relaunch spawned (the bat kills any stale stack itself)")


def verify(expect_pin: str, timeout_s: int = 300) -> tuple[bool, str]:
    """Poll until exactly ONE host on the expected pin + :5876 bound."""
    deadline = time.time() + timeout_s
    last = "no observation yet"
    while time.time() < deadline:
        time.sleep(10)
        out = ps("Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
                 "ForEach-Object { if ($_.CommandLine -match '--model\\s+(\\S+)') "
                 "{ Write-Output ($_.ProcessId.ToString() + '=' + $Matches[1]) } }")
        hosts = [h.strip() for h in out.splitlines() if "=" in h]
        port = "5876" in ps("(netstat -ano | Select-String ':5876\\s').Count")
        hb = "stale"
        try:
            age = time.time() - HEARTBEAT.stat().st_mtime
            hb = f"{age:.0f}s"
        except Exception:
            pass
        last = f"hosts={hosts} port5876={'up' if port else 'down'} heartbeat={hb}"
        on_pin = [h for h in hosts if expect_pin in h]
        if len(hosts) == 1 and on_pin and port:
            return True, last
        log(f"  waiting... {last}")
    return False, last


def main() -> int:
    log("=" * 70)
    log("OPUS-5 FLIP FIX starting (Zeke greenlit on Discord)")

    # Disarm the watchdog so it does not fight the surgery mid-flight.
    try:
        HOLDOFF.write_text("opus5_flip_fix in progress", encoding="utf-8")
        log("watchdog holdoff flag SET")
    except Exception as e:
        log(f"could not set holdoff ({e!r}) -- continuing")

    before = cli_version(BUNDLED)
    log(f"bundled CLI before: {vstr(before)}  (floor {vstr(FLOOR)})")

    # Order matters: stop the respawners, THEN kill what they respawn.
    sweep_launcher_loops()
    time.sleep(2)
    kill_hosts()
    time.sleep(3)
    sweep_launcher_loops()          # catch any loop that respawned mid-kill
    wait_unlocked()

    ok = upgrade_sdk()
    after = cli_version(BUNDLED)
    log(f"bundled CLI after: {vstr(after)}")

    if ok and after and after >= FLOOR:
        pin, label = GOOD_PIN, "claude-opus-5"
        log(f"FLOOR MET -- keeping pin {label}")
    else:
        pin, label = FALLBACK_PIN, "claude-opus-4-8[1m]"
        log(f"FLOOR NOT MET (upgrade_ok={ok}, version={vstr(after)}) "
            f"-- reverting pin to {label} so the stack still boots")
    set_pin(pin)

    relaunch()
    good, obs = verify(label)

    if good:
        msg = (f"\N{WHITE HEAVY CHECK MARK} Flip fix done. Bundled CLI "
               f"{vstr(before)} -> {vstr(after)}, single host on {label}, "
               f":5876 bound. Twin culled. {obs}")
    else:
        msg = (f"\N{WARNING SIGN} Flip fix ran but verification did not settle "
               f"in 5 min. Bundled CLI {vstr(before)} -> {vstr(after)}, "
               f"pin={label}. Last look: {obs}. "
               f"Log: state\\opus5_flip_fix.log")
    log(msg)
    dm(msg)

    try:
        HOLDOFF.unlink()
        log("watchdog holdoff flag CLEARED -- watchdog re-armed")
    except Exception as e:
        log(f"holdoff clear failed ({e!r}) -- DELETE state\\watchdog_holdoff.flag by hand")

    log("OPUS-5 FLIP FIX complete")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
