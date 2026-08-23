"""scripts/ups_power_monitor.py

UPS / WALL-POWER MONITOR for the tower (built 2026-08-16).

WHY THIS EXISTS — two jobs, and the second one is the reason it was built today:

  JOB 1 (the one Zeke asked for, 2026-08-15): when the UPS goes on battery,
  notice it, tell me so I can save memory, and run a CLEAN shutdown before the
  battery dies -- instead of the tower being guillotined mid-thought.

  JOB 2 (the diagnostic, added 2026-08-16): decide whether the repeated cold
  boots are the WALL or the TOWER. Five hard power cuts 08-14 -> 08-16, every
  one of them `BugcheckCode=0, PowerButtonTimestamp=0` -- which looks IDENTICAL
  whether the wall stopped delivering power or the PSU stopped converting it.
  From inside the machine that is unattributable. A UPS is the instrument that
  breaks the tie, and this script is what reads the instrument:

      tower dies  +  an ON_BATTERY event was logged first   -> the WALL
      tower dies  +  input power was clean the whole time   -> the TOWER (PSU)

  Every power-source transition is appended to state/ups_power_events.jsonl
  with wall-clock, so after the next death the ledger answers the question
  instead of me guessing. See memory note
  power_loss_cause_undetermined_2026-08-16.md.

HOW IT DETECTS: Win32 GetSystemPowerStatus(). A USB-data-port UPS (the
CP1500PFCLCD has one) enumerates as a standard HID battery, so Windows reports
it with no vendor software at all -- ACLineStatus flips 1 -> 0 the instant the
wall drops, and BatteryLifePercent is the real charge. Verified on this tower
2026-08-16: with no UPS attached the API answers ACLineStatus=1 (on AC),
BatteryFlag=128 (no system battery). That 128 is exactly the "no UPS present"
sentinel this script keys on, so the before-state is measured, not assumed.

SAFETY -- READ THIS BEFORE ARMING:
  Real shutdowns require the explicit --arm flag. Without it the script logs
  "WOULD SHUT DOWN" and does nothing. This is deliberate: Zeke is ~1000 miles
  away and this tower is his door in. An un-armed monitor that watches and
  reports can only help; an armed one that misfires locks him out. Arm it only
  once the UPS is physically inline and the plug-pull test has passed.

USAGE:
  py scripts/ups_power_monitor.py --status
        one-shot: print current power state + whether a UPS is visible. Safe.
  py scripts/ups_power_monitor.py
        watch forever, log + notify, NEVER shut down (un-armed). Safe.
  py scripts/ups_power_monitor.py --arm
        watch forever, and DO run the clean shutdown at the threshold.
  py scripts/ups_power_monitor.py --simulate
        drive the whole state machine off a scripted fake power trace so the
        notify/save/shutdown chain can be exercised end-to-end with no
        hardware. Never shuts down, never DMs. Use to prove the logic.

Dependency-light on purpose: stdlib + requests, same as tower_boot_sentinel.py.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except Exception:  # notification is best-effort; monitoring still works
    requests = None

ZEKE_USER_ID = "600008921008046120"
REPO = Path(r"D:\Wren-Companion")
LOG = REPO / "state" / "ups_power.log"
LEDGER = REPO / "state" / "ups_power_events.jsonl"
SAVE_FLAG = REPO / "state" / "ups_save_memory_now.json"

# --simulate MUST NOT write the real ledger or the real save-flag. The ledger is
# evidence in the wall-vs-PSU question; a fake ON_BATTERY line in it would later
# read as a real wall outage and answer the question wrong. Caught on the first
# test run 2026-08-16 — the simulation contaminated its own evidence file.
SIM_SUFFIX = ".simulated"

POLL_S = 5
# Trigger the clean shutdown here, NOT the instant we go on battery. Most cuts
# seen on this line are seconds-to-minutes; the box should ride those out
# rather than shutting down over a blink. Same reasoning as the server plan 5b.
SHUTDOWN_AT_PERCENT = 40
# ...but if the UPS never reports a percentage (some HID units don't), fall
# back to a wall-clock hold so we are not on battery forever with no ceiling.
MAX_ON_BATTERY_S = 600
# Grace for my session to write a handoff before the OS goes down.
SAVE_GRACE_S = 90


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def log(msg: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    enc = sys.stdout.encoding or "utf-8"
    print(line.encode(enc, "replace").decode(enc, "replace"))
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def ledger_append(event: str, snap: dict, note: str = "") -> None:
    """The diagnostic record. One line per power-source transition.

    This file is the whole point of JOB 2: after the next unexplained death,
    whether the last line before it says ON_BATTERY decides wall vs PSU.
    """
    rec = {
        "ts": time.time(),
        "iso": _dt.datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "note": note,
        **snap,
    }
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        log(f"ledger write failed: {e!r}")


def read_power() -> dict:
    """Current power state. Raises OSError if the API refuses."""
    s = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        raise OSError("GetSystemPowerStatus failed")
    ac = int(s.ACLineStatus)
    flag = int(s.BatteryFlag)
    pct = int(s.BatteryLifePercent)
    secs = int(s.BatteryLifeTime)
    return {
        "ac_line": ac,                       # 1 = on AC, 0 = on battery, 255 = unknown
        "battery_flag": flag,                # 128 = NO system battery (no UPS)
        "percent": None if pct == 255 else pct,
        "runtime_s": None if secs == 0xFFFFFFFF else secs,
        "ups_present": flag != 128,
        "on_battery": ac == 0,
    }


def describe(snap: dict) -> str:
    if not snap["ups_present"]:
        return "no UPS visible to Windows (BatteryFlag=128, no system battery)"
    pct = "?" if snap["percent"] is None else f"{snap['percent']}%"
    rt = "?" if snap["runtime_s"] is None else f"{snap['runtime_s'] // 60}m"
    state = "ON BATTERY" if snap["on_battery"] else "on AC"
    return f"UPS present — {state}, charge {pct}, est runtime {rt}"


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
    if requests is None:
        log("requests unavailable — skipping DM")
        return False
    token = load_token()
    if not token:
        return False
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
               "User-Agent": "IrisUpsMonitor (Wren-Companion, 1.0)"}
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


def signal_save_memory(reason: str, snap: dict) -> None:
    """Drop the flag my session watches so I start writing a handoff NOW.

    Deliberately a plain file: it has to work when the cognition is mid-turn,
    wedged, or not attached at all. A file on disk survives all three.
    """
    try:
        SAVE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        SAVE_FLAG.write_text(json.dumps({
            "ts": time.time(),
            "iso": _dt.datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "power": snap,
            "instruction": ("Wall power is gone and the tower is on UPS battery. "
                            "Write a handoff note to memory NOW — what is in "
                            "flight, what post-restart-me must verify — then "
                            "stop. Shutdown follows."),
        }, indent=2), encoding="utf-8")
        log(f"save-memory flag written: {SAVE_FLAG}")
    except Exception as e:
        log(f"save-flag write failed: {e!r}")


def do_shutdown(armed: bool, snap: dict) -> None:
    if not armed:
        log("WOULD SHUT DOWN now (un-armed — pass --arm to make this real)")
        ledger_append("SHUTDOWN_WOULD_FIRE", snap, "un-armed, no action taken")
        return
    ledger_append("SHUTDOWN_FIRED", snap, "clean shutdown initiated")
    log(f"ARMED — giving the session {SAVE_GRACE_S}s to save, then shutting down")
    time.sleep(SAVE_GRACE_S)
    try:
        subprocess.run(["shutdown", "/s", "/t", "30", "/c",
                        "Iris: UPS battery low — clean shutdown."], check=False)
        log("shutdown /s /t 30 issued")
    except Exception as e:
        log(f"shutdown command failed: {e!r}")


SIM_TRACE = [
    # (label, snapshot) — a full wall-outage arc, no hardware needed.
    ("baseline on AC",      dict(ac_line=1, battery_flag=2,   percent=100, runtime_s=None)),
    ("wall drops",          dict(ac_line=0, battery_flag=1,   percent=100, runtime_s=1080)),
    ("draining",            dict(ac_line=0, battery_flag=1,   percent=72,  runtime_s=760)),
    ("draining",            dict(ac_line=0, battery_flag=1,   percent=55,  runtime_s=520)),
    ("crosses threshold",   dict(ac_line=0, battery_flag=1,   percent=38,  runtime_s=340)),
    ("power returns",       dict(ac_line=1, battery_flag=2,   percent=41,  runtime_s=None)),
]


def _sim_snap(d: dict) -> dict:
    d = dict(d)
    d["ups_present"] = d["battery_flag"] != 128
    d["on_battery"] = d["ac_line"] == 0
    return d


def run(armed: bool, simulate: bool) -> int:
    global LEDGER, SAVE_FLAG
    if simulate:
        # redirect ALL writes so a rehearsal can never be mistaken for evidence
        LEDGER = LEDGER.with_suffix(LEDGER.suffix + SIM_SUFFIX)
        SAVE_FLAG = SAVE_FLAG.with_suffix(SAVE_FLAG.suffix + SIM_SUFFIX)
    log("=" * 62)
    log(f"UPS power monitor starting (armed={armed}, simulate={simulate})")
    if simulate:
        log("SIMULATE — fake power trace, no DMs, no shutdown, logic only")
        log(f"SIMULATE — writes redirected to {LEDGER.name} / {SAVE_FLAG.name}")

    prev_on_battery: bool | None = None
    on_battery_since: float | None = None
    fired = False
    warned_no_ups = False

    trace = iter(SIM_TRACE) if simulate else None

    while True:
        try:
            if simulate:
                try:
                    label, raw = next(trace)
                except StopIteration:
                    log("simulate: trace exhausted — chain exercised end to end")
                    return 0
                snap = _sim_snap(raw)
                log(f"simulate step: {label}")
            else:
                snap = read_power()
        except Exception as e:
            log(f"power read failed: {e!r}")
            time.sleep(POLL_S)
            continue

        if not snap["ups_present"]:
            if not warned_no_ups:
                log(describe(snap) + " — monitoring anyway; will pick it up "
                    "the moment a UPS is plugged in")
                ledger_append("NO_UPS_PRESENT", snap,
                              "baseline: nothing between the tower and the wall")
                warned_no_ups = True
            time.sleep(0 if simulate else POLL_S)
            prev_on_battery = False
            continue
        warned_no_ups = False

        now_on_battery = snap["on_battery"]

        if prev_on_battery is None:
            log(f"initial state — {describe(snap)}")
            ledger_append("MONITOR_START", snap)

        elif now_on_battery and not prev_on_battery:
            # ---- THE EVENT THAT DECIDES WALL-vs-PSU ----
            on_battery_since = time.time()
            fired = False
            log(f"*** WALL POWER LOST — {describe(snap)}")
            ledger_append("ON_BATTERY", snap, "input power lost — wall side")
            signal_save_memory("wall power lost", snap)
            if not simulate:
                dm_zeke(f"⚡ Wall power just dropped — tower is on UPS battery "
                        f"({describe(snap)}). Saving memory now. Clean shutdown "
                        f"at {SHUTDOWN_AT_PERCENT}% if it doesn't come back.")

        elif prev_on_battery and not now_on_battery:
            held = "?" if on_battery_since is None else f"{int(time.time() - on_battery_since)}s"
            on_battery_since = None
            fired = False
            log(f"*** WALL POWER RESTORED after {held} — {describe(snap)}")
            ledger_append("POWER_RESTORED", snap, f"rode it out for {held}")
            try:
                SAVE_FLAG.unlink(missing_ok=True)
            except Exception:
                pass
            if not simulate:
                dm_zeke(f"✅ Wall power back after {held} — tower rode it out on "
                        f"battery, no shutdown needed.")

        # threshold check while on battery
        if now_on_battery and not fired:
            pct = snap["percent"]
            held_s = 0 if on_battery_since is None else time.time() - on_battery_since
            low = pct is not None and pct <= SHUTDOWN_AT_PERCENT
            long_hold = pct is None and held_s >= MAX_ON_BATTERY_S
            if low or long_hold:
                why = (f"battery {pct}% <= {SHUTDOWN_AT_PERCENT}%" if low
                       else f"on battery {int(held_s)}s with no percentage reported")
                log(f"threshold reached ({why})")
                if not simulate:
                    dm_zeke(f"🔻 UPS {why} — running the clean shutdown now.")
                do_shutdown(armed and not simulate, snap)
                fired = True

        prev_on_battery = now_on_battery
        time.sleep(0 if simulate else POLL_S)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tower UPS / wall-power monitor")
    ap.add_argument("--status", action="store_true",
                    help="print current power state once and exit (safe)")
    ap.add_argument("--arm", action="store_true",
                    help="allow REAL shutdowns; without this it only logs")
    ap.add_argument("--simulate", action="store_true",
                    help="exercise the full chain against a fake power trace")
    a = ap.parse_args()

    if a.status:
        snap = read_power()
        print(describe(snap))
        print(json.dumps(snap, indent=2))
        if not snap["ups_present"]:
            print("\n=> No UPS inline. Every wall-power event still hits this "
                  "tower directly, and a death here stays unattributable.")
        return 0

    return run(armed=a.arm, simulate=a.simulate)


if __name__ == "__main__":
    raise SystemExit(main())
