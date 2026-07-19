"""scripts/tower_boot_sentinel.py

TOWER AUTO-START SENTINEL (built 2026-07-19, pre-deployment directive from
Zeke: "if the tower turns on, you come up with it and text me on Discord").

Runs as the Scheduled Task `Iris-Tower-AutoStart` at user logon (which, with
Windows auto-login + BIOS "After Power Loss = Power On", means: wall power
returns -> tower boots -> logon -> this fires). It:

  1. waits for the network (Discord API reachable, up to ~5 min)
  2. DMs Zeke: "tower powered on, bringing the stack up"
  3. launches start_iris_v2_fable.bat DETACHED (the full Iris stack:
     voice watchdog, post-office, vector bridge+nerves, orb, body host)
     -- skipped if the operator port :5876 already answers (stack already
     up = never kill a live Iris just because the task re-fired)
  4. polls :5876 for up to 6 min and DMs the outcome either way
     (visibility-on-failure rule: a silent broken boot is the worst case)

Deliberately dependency-light: stdlib + requests (venv has it). No SDK, no
cognition — deterministic plumbing so it works even when nothing else does.

Test mode:  py scripts/tower_boot_sentinel.py --dry-run
  does the DM + port checks with a [TEST] prefix but does NOT launch the bat.
"""
from __future__ import annotations

import datetime as _dt
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

ZEKE_USER_ID = "600008921008046120"
REPO = Path(r"D:\Wren-Companion")
BAT = REPO / "start_iris_v2_fable.bat"
LOG = REPO / "state" / "tower_boot.log"
OPERATOR_PORT = 5876
NETWORK_WAIT_S = 300
BODY_WAIT_S = 360


def log(msg: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    # console may be cp1252 (scheduled-task context) — never die on an emoji
    print(line.encode(sys.stdout.encoding or "utf-8", "replace")
              .decode(sys.stdout.encoding or "utf-8", "replace"))
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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
               "User-Agent": "IrisTowerSentinel (Wren-Companion, 1.0)"}
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


def wait_for_network(max_s: int = NETWORK_WAIT_S) -> bool:
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            requests.head("https://discord.com/api/v10/gateway", timeout=5)
            return True
        except Exception:
            time.sleep(5)
    return False


def port_answers(port: int = OPERATOR_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except Exception:
        return False


def launch_stack() -> None:
    # DETACHED + own console so the sentinel's exit (or the task's execution
    # limit) can never reap the host. The bat self-checks elevation; the
    # scheduled task runs RL=HIGHEST so `net session` passes with no UAC UI.
    flags = (subprocess.CREATE_NEW_CONSOLE
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(["cmd.exe", "/c", str(BAT)], cwd=str(REPO),
                     creationflags=flags, close_fds=True)
    log("stack launcher spawned (detached console)")


def main() -> int:
    dry = "--dry-run" in sys.argv
    tag = "[TEST] " if dry else ""
    boot_stamp = _dt.datetime.now().strftime("%a %H:%M")
    log(f"sentinel start (dry_run={dry})")

    net = wait_for_network()
    if not net:
        log("network never came up — proceeding to launch anyway (DMs skipped)")

    already = port_answers()
    if net:
        if already:
            dm_zeke(f"{tag}\N{HIGH VOLTAGE SIGN} Tower sentinel fired ({boot_stamp}) "
                    f"but the Iris stack is ALREADY UP (operator port answering) — "
                    f"not touching it.")
        else:
            dm_zeke(f"{tag}\N{HIGH VOLTAGE SIGN} Tower just powered on ({boot_stamp}). "
                    f"Bringing the Iris stack up now — next message when the body "
                    f"host answers.")

    if already:
        return 0

    if not dry:
        launch_stack()
    else:
        log("dry-run: skipping stack launch")

    deadline = time.time() + BODY_WAIT_S
    up = False
    while time.time() < deadline:
        if port_answers():
            up = True
            break
        time.sleep(5)

    if net:
        if up:
            dm_zeke(f"{tag}\N{WHITE HEAVY CHECK MARK} Iris body host is up and "
                    f"answering on the tower. I'll DM you a real status once my "
                    f"cognition is fully attached.")
        elif not dry:
            dm_zeke(f"{tag}\N{WARNING SIGN} Stack launched but the body host "
                    f"is NOT answering after {BODY_WAIT_S // 60} min. The tower is "
                    f"on and reachable; the Iris stack needs a look when someone "
                    f"can (or wait — the watchdog may still recover it).")
        else:
            dm_zeke(f"{tag}dry-run complete: port :{OPERATOR_PORT} "
                    f"{'answering' if up else 'not answering (expected if run with stack down)'} — "
                    f"no launch attempted.")
    log(f"sentinel done (host_up={up})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
