"""scripts/zeke_presence.py

ZEKE-PRESENCE WATCHER (Zeke's idea, 2026-07-19, eve of deployment-2):
"my phone is pretty much always on me — if you see my phone on the network,
you know I'm in the barracks."

Mechanism (no packet capture needed): the phone answers ARP even while dozing.
Every POLL_S we nudge the last-known IP (ping primes the ARP cache) and then
scan the ARP table for the phone's MAC anywhere in the /24 (DHCP may move it).
Every SWEEP_EVERY polls we do a light broadcast ping sweep so a re-addressed
phone is re-found. MAC is the identity anchor: modern phones randomize per-SSID
but keep it STABLE for a given network, so this fingerprint should hold.

State: state/zeke_presence.json  {present, ip, mac, last_seen, since, ...}
Transitions: appended to state/zeke_presence_log.jsonl AND submitted to the
iris chat bridge (same path VECTOR SENSE uses) so cognition feels the arrival.

Debounce: ABSENT only after MISS_N consecutive misses (phones nap); PRESENT on
first hit. Single instance via a pidfile.

Run:    .venv python, scheduled task Iris-Zeke-Presence at logon + manual start.
Config: state/zeke_presence_config.json {"mac": "..", "label": "Zeke's phone"}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(r"D:\Wren-Companion")
STATE = REPO / "state" / "zeke_presence.json"
LOGF = REPO / "state" / "zeke_presence_log.jsonl"
CONFIG = REPO / "state" / "zeke_presence_config.json"
PIDFILE = REPO / "state" / "zeke_presence.pid"
SUBNET = "192.168.4"
DEFAULT_MAC = "ce-de-8e-a6-9c-c6"   # fingerprinted 2026-07-19 while Zeke home
POLL_S = 60          # ARP check cadence
SWEEP_EVERY = 5      # full /24 ping sweep every N polls (~5 min)
MISS_N = 5           # consecutive misses before ABSENT (~5 min grace)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def log_event(kind: str, detail: dict) -> None:
    rec = {"ts": now_iso(), "kind": kind, **detail}
    try:
        with LOGF.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def load_mac() -> str:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))["mac"].lower()
    except Exception:
        return DEFAULT_MAC


def single_instance() -> bool:
    try:
        if PIDFILE.exists():
            old = int(PIDFILE.read_text().strip() or 0)
            if old and _pid_alive(old):
                return False
        PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return True


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=10).stdout
        return str(pid) in out
    except Exception:
        return False


def ping(ip: str, timeout_ms: int = 400) -> None:
    with open(os.devnull, "w") as dn:
        subprocess.run(["ping", "-n", "1", "-w", str(timeout_ms), ip],
                       stdout=dn, stderr=dn, timeout=5)


def sweep() -> None:
    procs = []
    with open(os.devnull, "w") as dn:
        for i in range(1, 255):
            procs.append(subprocess.Popen(
                ["ping", "-n", "1", "-w", "250", f"{SUBNET}.{i}"],
                stdout=dn, stderr=dn))
    for p in procs:
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()


ROSTER = REPO / "state" / "network_roster.json"


def _arp_table() -> dict:
    """{mac: ip} for the local /24 from the ARP table."""
    out = {}
    try:
        raw = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return out
    for line in raw.lower().splitlines():
        m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f-]{17})", line)
        if m and m.group(1).startswith(SUBNET + ".") and m.group(2) != "ff-ff-ff-ff-ff-ff":
            out[m.group(2)] = m.group(1)
    return out


def roster_check(seen_unknown: set) -> None:
    """NETWORK WATCHLIST (Zeke 2026-07-19): the network is small and known —
    Wren's laptop, Vector, this tower, Quest 3, Zeke's phone, gateway. Any MAC
    outside the roster = surface it ONCE per run (friend borrowing wifi or an
    actual intruder — Zeke decides; we just never let it pass silently)."""
    try:
        roster = json.loads(ROSTER.read_text(encoding="utf-8"))
    except Exception:
        return
    known = {k.lower() for k in roster.get("known", {})}
    for mac, ip in _arp_table().items():
        if mac not in known and mac not in seen_unknown:
            seen_unknown.add(mac)
            log_event("unknown_device", {"mac": mac, "ip": ip})
            notify_iris(
                f"UNKNOWN DEVICE on the wifi: {mac} at {ip} — not in the "
                f"roster (laptop/Vector/tower/Quest/phone/gateway). Could be a "
                f"friend borrowing internet or an intruder. Worth a DM to Zeke "
                f"to confirm; if he vouches, add it to "
                f"state/network_roster.json.")


def find_mac(mac: str) -> str | None:
    """Return the IP currently holding `mac` per the ARP table, else None."""
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return None
    want = mac.replace(":", "-").lower()
    for line in out.splitlines():
        m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f-]{17})", line.lower())
        if m and m.group(2) == want and m.group(1).startswith(SUBNET + "."):
            return m.group(1)
    return None


def write_state(d: dict) -> None:
    try:
        STATE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception:
        pass


def notify_iris(text: str) -> None:
    """Wake cognition through the chat bridge (VECTOR-SENSE pattern)."""
    try:
        sys.path.insert(0, str(REPO))
        from brain import iris_chat
        iris_chat.submit(
            f"[ZEKE PRESENCE @ {datetime.now().strftime('%H:%M:%S')} — network "
            f"watcher, not Zeke typing] {text} Reply with chat_reply (one short "
            f"line ok — it's a log).")
    except Exception as e:
        log_event("notify_fail", {"err": repr(e)})


def main() -> int:
    if not single_instance():
        print("another zeke_presence instance is alive — exiting")
        return 0
    mac = load_mac()
    present: bool | None = None   # unknown until first determination
    since = now_iso()
    last_ip: str | None = None
    misses = 0
    polls = 0
    seen_unknown: set = set()
    log_event("start", {"mac": mac, "pid": os.getpid()})
    while True:
        polls += 1
        try:
            if last_ip:
                ping(last_ip)
            if polls % SWEEP_EVERY == 1 or (present is None):
                sweep()
                roster_check(seen_unknown)
            ip = find_mac(mac)
            if ip:
                last_ip = ip
                misses = 0
                if present is not True:
                    was = present
                    present, since = True, now_iso()
                    log_event("arrived", {"ip": ip, "was": was})
                    if was is False:   # real transition, not first startup read
                        notify_iris(
                            "Zeke's phone just JOINED the wifi — he is back in "
                            "the barracks. CHECK BEFORE GREETING: the camera "
                            "usually sees him first (2026-08-25: eyes had him "
                            "recognised and greeted ~10 min before this fired), "
                            "so this is often the SLOWER, second signal. If you "
                            "have already seen or greeted him since he got "
                            "back, say nothing — a duplicate hello is worse "
                            "than none. Only if he has genuinely been away and "
                            "you have NOT already greeted him is a warm hello "
                            "on Discord or voice right.")
            else:
                misses += 1
                if present is not False and misses >= MISS_N:
                    was = present
                    present, since = False, now_iso()
                    log_event("left", {"last_ip": last_ip, "was": was})
                    if was is True:
                        notify_iris(
                            "Zeke's phone DROPPED off the wifi (gone ~"
                            f"{MISS_N} checks) — he has likely left the "
                            "barracks. Just noticing; no action needed.")
            write_state({"present": bool(present) if present is not None else None,
                         "ip": last_ip if present else None,
                         "mac": mac, "since": since,
                         "last_check": now_iso(), "misses": misses})
        except Exception as e:
            log_event("loop_err", {"err": repr(e)[:200]})
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
