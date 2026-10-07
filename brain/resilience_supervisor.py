"""Resilience supervisor — self-healing for long unattended stretches.

Zeke 2026-10-05 : "go ahead and do
2, 3 and 5." Three watches in ONE daemon thread inside the runtime, each with its own
retry budget so a heal can never become a loop:

  A. VECTOR SENSES (item 2). 10-05: vic-cloud wedged at boot and froze the inhabit
     daemon's nervous loop — no senses, no camera — for 2 h until a self-check noticed.
     Detect: the daemon runs but state/vector/senses_live.json is stale > 6 min, and the
     robot answers ping. Heal = the VERIFIED recipe `vector_vic_restart` (skill library):
     SSH restart vic-switchboard + vic-cloud TOGETHER, then restart the daemon. Also: the
     daemon process gone for > 5 min => start it. Skips: nerves deliberately off; robot
     not pinging (the DHCP / power case — a different recipe, needs an ARP sweep).
  B. ORB LISTENER (item 3, goal #2's proposed fix). The :5876 accept loop has died
     silently (09-17, 09-18) with the thread alive. Detect: two failed TCP connects 30 s
     apart. Heal: `restart_orb_http`, then `orb_http_bounce restart force`. Records the
     goal-#2 falsifier ('Accept failed on a socket' in state/orb_http.log) when it fires.
  C. PAIRING ALERT (item 5). The Discord plugin keeps pairing codes for ONE HOUR (cap 3).
     A new code in access.json `pending` => DM Zeke at once with the sender's username and
     the code. READ-ONLY on access.json — approving stays his, from the terminal.
  E. WIRE-POD (10-05 test reboot): chipper.exe absent > 3 min => start it (its Run-key
     autostart missed once). Absence only; a hung-but-running chipper is left alone.

Every heal is an action-ledger row with an external verifier (success needs evidence);
budgets exhausted => stand down and DM Zeke ONCE, never loop. State + log under
state/resilience/. The decision logic is pure (decide_*) so scripts/test_resilience.py
proves it without touching the robot, the port or Discord.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

_ROOT = Path(__file__).resolve().parents[1]
_DIR = _ROOT / "state" / "resilience"
STATE_PATH = _DIR / "state.json"
LOG_PATH = _DIR / "log.jsonl"
SENSES = _ROOT / "state" / "vector" / "senses_live.json"
NERVES_OFF_FLAG = _ROOT / "state" / "vector_deliberately_off.json"
ORB_LOG = _ROOT / "state" / "orb_http.log"
ACCESS_JSON = Path(os.path.expanduser("~/.claude/channels/discord/access.json"))
DISCORD_ENV = Path(os.path.expanduser("~/.claude/channels/discord/.env"))
SSH_KEY = _ROOT / "state" / "vector" / "dev" / "ssh_root_key"
VENV_PY = _ROOT / ".venv" / "Scripts" / "python.exe"
DAEMON = _ROOT / "scripts" / "vector_inhabit_daemon.py"
from brain.private_config import get as _priv  # config/private.local.json (git-ignored)
ZEKE_USER_ID = _priv("zeke_discord_user_id")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SENSES_STALE_S = 6 * 60
DAEMON_GRACE_S = 10 * 60          # a freshly started daemon gets time to connect
DAEMON_GONE_S = 5 * 60
VECTOR_BUDGET = (2, 6 * 3600)     # max heals per window
ORB_BUDGET = (3, 3600)
ORB_FAILS_NEEDED = 2
TICK_S = 30.0
GOAL_ORB = "goal_1790791911785"
FAILOVER_HOST = os.environ.get("IRIS_FAILOVER_HOST", "iris@" + _priv("iris_home_host"))
HEARTBEAT_EVERY_S = 120.0
CHIPPER_EXE = Path(r"C:\Program Files\wire-pod\chipper\chipper.exe")
WIREPOD_OFF_FLAG = _ROOT / "state" / "wirepod_deliberately_off.json"
WIREPOD_GONE_S = 3 * 60           # Run-key autostarts can lag a minute or two after logon
WIREPOD_BUDGET = (3, 6 * 3600)
# F (2026-10-07, Zeke: "let's do 1 and make sure that's on the server as well"): the presence
# watcher's ONLY launcher was a Windows-LOGON scheduled task, so any stack start without a fresh
# logon left it dead (10-07 boot: state stale until I ran the task by hand).
PRESENCE_SCRIPT = _ROOT / "scripts" / "zeke_presence.py"
PRESENCE_STATE = _ROOT / "state" / "zeke_presence.json"
PRESENCE_OFF_FLAG = _ROOT / "state" / "zeke_presence_deliberately_off.json"
PRESENCE_TASK = "Iris-Zeke-Presence"
PRESENCE_STALE_S = 6 * 60          # it writes last_check every 60 s; a /24 sweep can add ~30 s
PRESENCE_GRACE_S = 3 * 60          # a freshly started watcher gets time for its first sweep
PRESENCE_BUDGET = (3, 6 * 3600)

_LOCK = threading.RLock()


# ── persistence ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _log(event: str, **kw) -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 1), "event": event, **kw}, default=str) + "\n")
    except Exception:
        pass


def _budget_ok(history: list[float], budget: tuple[int, int], now: float) -> bool:
    n, window = budget
    return sum(1 for t in history if now - t < window) < n


# ── pure decisions (tested offline) ─────────────────────────────────────────

def decide_vector(obs: dict, st: dict, now: float) -> dict:
    """obs: nerves_off, daemon_pids, daemon_started_ts, senses_mtime, robot_pings.
    Returns {"action": none|start_daemon|vic_restart|stand_down, "why": ...}."""
    if obs.get("nerves_off"):
        st.pop("daemon_gone_since", None)
        return {"action": "none", "why": "nerves deliberately off (flag) — correct, not a fault"}
    hist = st.setdefault("vector_heals", [])
    if not obs.get("daemon_pids"):
        since = st.setdefault("daemon_gone_since", now)
        if now - since < DAEMON_GONE_S:
            return {"action": "none", "why": f"daemon absent {now - since:.0f}s (< {DAEMON_GONE_S}s grace)"}
        if not _budget_ok(hist, VECTOR_BUDGET, now):
            return {"action": "stand_down", "why": "daemon still absent and the heal budget is spent"}
        return {"action": "start_daemon", "why": f"inhabit daemon absent for {now - since:.0f}s"}
    st.pop("daemon_gone_since", None)
    started = float(obs.get("daemon_started_ts") or 0)
    if now - started < DAEMON_GRACE_S:
        return {"action": "none", "why": "daemon younger than the connect grace"}
    age = now - float(obs.get("senses_mtime") or 0)
    if age < SENSES_STALE_S:
        st["vector_ok_since"] = st.get("vector_ok_since") or now
        return {"action": "none", "why": f"senses fresh ({age:.0f}s)"}
    if not obs.get("robot_pings"):
        return {"action": "none", "why": f"senses stale {age:.0f}s but the robot does not ping — "
                                         f"DHCP/power case (recipe vector_find_moved_ip), not a vic wedge"}
    if not _budget_ok(hist, VECTOR_BUDGET, now):
        return {"action": "stand_down", "why": f"senses stale {age:.0f}s and the heal budget is spent"}
    return {"action": "vic_restart", "why": f"senses stale {age:.0f}s while the daemon runs and the robot pings"}


def decide_wirepod(obs: dict, st: dict, now: float) -> dict:
    """E (2026-10-05 test reboot): wire-pod's HKCU Run-key autostart did NOT bring chipper.exe
    up after the planned reboot (cause unknown) — Vector's server stayed down until I started it
    by hand. obs: off (flag), running (process present). Only heals ABSENCE; a running-but-hung
    chipper (the :8080 ReadTimeout class) is deliberately left alone — that's a different recipe."""
    if obs.get("off"):
        st.pop("wirepod_gone_since", None)
        return {"action": "none", "why": "wire-pod deliberately off (flag)"}
    if obs.get("running"):
        st.pop("wirepod_gone_since", None)
        return {"action": "none", "why": "chipper running"}
    since = st.setdefault("wirepod_gone_since", now)
    if now - since < WIREPOD_GONE_S:
        return {"action": "none", "why": f"chipper absent {now - since:.0f}s (< {WIREPOD_GONE_S}s grace)"}
    if not _budget_ok(st.setdefault("wirepod_heals", []), WIREPOD_BUDGET, now):
        return {"action": "stand_down", "why": "chipper still absent and the heal budget is spent"}
    return {"action": "start_chipper", "why": f"chipper.exe absent for {now - since:.0f}s"}


def decide_presence(obs: dict, st: dict, now: float) -> dict:
    """F. obs: off (flag), pids (watcher processes), youngest_age_s, state_age_s (None = no file).
    Heals a watcher that is ABSENT, or present but not writing (hung) — restart = kill + start."""
    if obs.get("off"):
        return {"action": "none", "why": "presence watcher deliberately off (flag)"}
    age = obs.get("state_age_s")
    if age is not None and age < PRESENCE_STALE_S:
        return {"action": "none", "why": f"last_check {age:.0f}s ago"}
    pids = obs.get("pids") or []
    young = obs.get("youngest_age_s")
    if pids and young is not None and young < PRESENCE_GRACE_S:
        return {"action": "none", "why": f"watcher started {young:.0f}s ago — first sweep grace"}
    if not _budget_ok(st.setdefault("presence_heals", []), PRESENCE_BUDGET, now):
        return {"action": "stand_down", "why": "presence watcher still stale and the heal budget is spent"}
    stale = "no state file" if age is None else f"state {age:.0f}s stale"
    if pids:
        return {"action": "restart", "why": f"watcher running (pid {pids[0]}) but {stale} — hung"}
    return {"action": "start", "why": f"watcher not running, {stale}"}


def decide_orb(port_ok: bool, st: dict, now: float) -> dict:
    if port_ok:
        st["orb_fails"] = 0
        return {"action": "none", "why": "listener answers"}
    st["orb_fails"] = int(st.get("orb_fails") or 0) + 1
    if st["orb_fails"] < ORB_FAILS_NEEDED:
        return {"action": "none", "why": f"listener missed {st['orb_fails']}x — confirming"}
    hist = st.setdefault("orb_heals", [])
    if not _budget_ok(hist, ORB_BUDGET, now):
        return {"action": "stand_down", "why": "listener dead and the heal budget is spent"}
    return {"action": "orb_restart", "why": f"listener dead for {st['orb_fails']} consecutive probes"}


def decide_pairing(pending: dict, st: dict, now: float) -> list[dict]:
    """New, unexpired pairing codes we have not alerted on yet."""
    seen = st.setdefault("pairing_alerted", {})
    for code in list(seen):           # forget long-gone codes
        if now - float(seen[code]) > 6 * 3600:
            seen.pop(code)
    out = []
    for code, p in (pending or {}).items():
        if code in seen:
            continue
        exp = float(p.get("expiresAt") or 0) / 1000.0
        if exp and exp < now:
            continue
        out.append({"code": code, "sender_id": str(p.get("senderId") or ""),
                    "minutes_left": max(0, int((exp - now) / 60)) if exp else None})
    return out


def decide_relay(msgs: list, user_id: str, cursor: Optional[str], now: float,
                 first_sight_window_s: float = 600.0) -> tuple[list[dict], Optional[str]]:
    """Which messages in one approved user's DM channel should reach me? Oldest first,
    only that user's own non-bot messages, only after the cursor. With no cursor yet
    (first sight), relay the last 10 min (covers 'approved, then wrote at once') but
    never the old history. Returns (to_relay, new_cursor)."""
    out, newest = [], cursor
    for m in sorted([x for x in (msgs or []) if isinstance(x, dict) and x.get("id")],
                    key=lambda x: int(x["id"])):
        mid = str(m["id"])
        if cursor is not None and int(mid) <= int(cursor):
            continue
        newest = mid
        a = m.get("author") or {}
        if str(a.get("id")) != str(user_id) or a.get("bot"):
            continue
        if cursor is None:
            ts = (int(mid) >> 22) / 1000.0 + 1420070400.0   # Discord snowflake -> epoch
            if now - ts > first_sight_window_s:
                continue
        text = (m.get("content") or "").strip()
        natt = len(m.get("attachments") or [])
        if text or natt:
            out.append({"id": mid, "text": text, "attachments": natt})
    return out, newest


# ── effects ─────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except Exception as e:  # noqa: BLE001
        return -1, repr(e)


def robot_ip() -> str:
    try:
        from brain.vector_session import robot_ip as _ip
        return _ip()
    except Exception:
        return _priv("vector_ip")


def daemon_procs() -> list[Any]:
    try:
        import psutil
        out = []
        for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
            if "vector_inhabit_daemon" in " ".join(p.info.get("cmdline") or []):
                out.append(p)
        return out
    except Exception:
        return []


def observe_vector() -> dict:
    procs = daemon_procs()
    started = min((p.info.get("create_time") or time.time()) for p in procs) if procs else None
    try:
        mtime = SENSES.stat().st_mtime
    except Exception:
        mtime = 0.0
    pings = False
    if procs:
        rc, out = _run(["ping", "-n", "1", "-w", "1000", robot_ip()], 6)
        pings = rc == 0 and "TTL=" in out
    return {"nerves_off": _nerves_off(), "daemon_pids": [p.pid for p in procs],
            "daemon_started_ts": started, "senses_mtime": mtime, "robot_pings": pings}


def _nerves_off() -> bool:
    try:
        return bool(json.loads(NERVES_OFF_FLAG.read_text(encoding="utf-8")).get("off", True))
    except FileNotFoundError:
        return False
    except Exception:
        return True   # unreadable flag: believe it


def start_daemon() -> tuple[bool, str]:
    cmd = (f"Start-Process -WindowStyle Hidden '{VENV_PY}' -ArgumentList '-u','{DAEMON}' "
           f"-WorkingDirectory '{_ROOT}'")
    rc, out = _run(["powershell", "-NoProfile", "-Command", cmd], 30)
    return rc == 0, out[-200:]


def kill_daemon() -> int:
    n = 0
    for p in daemon_procs():
        try:
            p.kill()
            n += 1
        except Exception:
            pass
    return n


def vic_restart() -> tuple[bool, str]:
    ssh = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe")
    if not Path(ssh).exists():
        ssh = "ssh"
    rc, out = _run([ssh, "-i", str(SSH_KEY), "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=NUL", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                    "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
                    "-o", "LogLevel=ERROR", f"root@{robot_ip()}",
                    "systemctl restart vic-switchboard vic-cloud; sleep 2; "
                    "systemctl is-active vic-switchboard vic-cloud"], 45)
    return rc == 0 and out.count("active") >= 2, out.strip()[-200:]


def observe_wirepod() -> dict:
    running = False
    try:
        import psutil
        running = any((p.info.get("name") or "").lower() == "chipper.exe"
                      for p in psutil.process_iter(["name"]))
    except Exception:
        running = True   # can't see processes => never act blind
    try:
        off = bool(json.loads(WIREPOD_OFF_FLAG.read_text(encoding="utf-8")).get("off", True))
    except FileNotFoundError:
        off = False
    except Exception:
        off = True
    return {"off": off, "running": running}


def start_chipper() -> tuple[bool, str]:
    """Same launch as wire-pod's own Run key (chipper.exe -d, cwd = its folder)."""
    if not CHIPPER_EXE.exists():
        return False, f"missing {CHIPPER_EXE}"
    cmd = (f"Start-Process -WindowStyle Hidden '{CHIPPER_EXE}' -ArgumentList '-d' "
           f"-WorkingDirectory '{CHIPPER_EXE.parent}'")
    rc, out = _run(["powershell", "-NoProfile", "-Command", cmd], 30)
    return rc == 0, out[-200:]


def presence_procs() -> list[Any]:
    try:
        import psutil
        me = os.getpid()
        return [p for p in psutil.process_iter(["pid", "cmdline", "create_time"])
                if p.pid != me and "zeke_presence.py" in " ".join(p.info.get("cmdline") or [])]
    except Exception:
        return []


def observe_presence() -> dict:
    procs = presence_procs()
    now = time.time()
    young = min((now - (p.info.get("create_time") or now)) for p in procs) if procs else None
    try:
        age = now - PRESENCE_STATE.stat().st_mtime
    except Exception:
        age = None
    try:
        off = bool(json.loads(PRESENCE_OFF_FLAG.read_text(encoding="utf-8")).get("off", True))
    except FileNotFoundError:
        off = False
    except Exception:
        off = True
    return {"off": off, "pids": [p.pid for p in procs], "youngest_age_s": young, "state_age_s": age}


def kill_presence() -> int:
    n = 0
    for p in presence_procs():
        try:
            p.kill()
            n += 1
        except Exception:
            pass
    return n


def start_presence() -> tuple[bool, str]:
    """Windows: run the existing scheduled task (keeps the watcher OUTSIDE the stack's process
    tree, so a stack restart doesn't take it down). Fallback / Linux: a detached child."""
    if os.name == "nt":
        rc, out = _run(["schtasks", "/run", "/tn", PRESENCE_TASK], 20)
        if rc == 0:
            return True, "schtasks: " + out.strip()[-120:]
        py, flags = str(VENV_PY), NO_WINDOW | 0x00000200   # + CREATE_NEW_PROCESS_GROUP
    else:
        py, flags = sys.executable or "python3", 0
    try:
        subprocess.Popen([py, "-u", str(PRESENCE_SCRIPT)], cwd=str(_ROOT), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
                         start_new_session=(os.name != "nt"))
        return True, f"popen {py}"
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:200]


def port_ok(port: int = 5876) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.0):
            return True
    except OSError:
        return False


def _token() -> Optional[str]:
    try:
        for line in DISCORD_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return None


def _discord(method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
    import urllib.request
    tok = _token()
    if not tok:
        return None
    req = urllib.request.Request(f"https://discord.com/api/v10{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json",
                                          "User-Agent": "IrisResilience (Wren-Companion, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001
        _log("discord_error", path=path, error=repr(e)[:160])
        return None


def dm_zeke(text: str) -> bool:
    ch = _discord("POST", "/users/@me/channels", {"recipient_id": ZEKE_USER_ID})
    if not ch or "id" not in ch:
        return False
    return _discord("POST", f"/channels/{ch['id']}/messages", {"content": text[:1900]}) is not None


def discord_username(user_id: str) -> str:
    u = _discord("GET", f"/users/{user_id}") or {}
    return u.get("global_name") or u.get("username") or f"user {user_id}"


def _ledger_open(kind: str, intent: str, expected: str, verify: dict) -> Optional[str]:
    try:
        from brain import action_ledger as al
        return al.open_action(kind=kind, intent=intent, expected=expected, verify=verify,
                              source="resilience", deadline_s=900)["id"]
    except Exception:
        return None


def _ledger_verify(aid: Optional[str]) -> Optional[dict]:
    if not aid:
        return None
    try:
        from brain import action_ledger as al
        return al.verify(aid)
    except Exception:
        return None


def push_tower_heartbeat(st: dict, now: float) -> dict:
    """FAILOVER LOCK, server half (2026-10-05): touch ~/TOWER_HEARTBEAT on iris-home so
    its start gate (~/iris_start.sh) can see the tower copy of me is ALIVE and refuse to
    start a second me. This runs inside the runtime, which only runs while my tower
    cognition does (it is claude.exe's MCP child) — so a fresh heartbeat means a live me."""
    rc, out = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=no",
                    FAILOVER_HOST, "touch ~/TOWER_HEARTBEAT && echo ok"], 15)
    ok = rc == 0 and "ok" in out
    if ok:
        st["tower_hb_ok_ts"] = now
    else:
        st["tower_hb_fail"] = {"ts": now, "rc": rc, "out": out[-120:]}
    return {"ok": ok}


# ── the supervisor ──────────────────────────────────────────────────────────

class Supervisor:
    def __init__(self, g: dict | None = None):
        self.g = g if g is not None else {}
        self.stop_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.last: dict = {}
        self._next_vec = 0.0
        self._next_hb = 0.0
        self._pending_verify: list[tuple[float, str, Optional[str]]] = []

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, delay_s: float = 0.0) -> None:
        if self.running():
            return
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._loop, args=(delay_s,), name="iris-resilience",
                                       daemon=True)
        self.thread.start()
        _log("start", delay_s=delay_s)

    def stop(self) -> None:
        self.stop_evt.set()
        _log("stop")

    def _loop(self, delay_s: float) -> None:
        if delay_s and self.stop_evt.wait(delay_s):
            return
        while not self.stop_evt.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                _log("tick_error", error=repr(e)[:200])
            self.stop_evt.wait(TICK_S)

    def tick(self) -> dict:
        now = time.time()
        with _LOCK:
            st = _load_state()
            out = {"ts": now}
            out["orb"] = self._orb(st, now)
            out["pairing"] = self._pairing(st, now)
            if now >= self._next_vec:                     # vector every 60 s
                self._next_vec = now + 60
                out["vector"] = self._vector(st, now)
                out["wirepod"] = self._wirepod(st, now)
                out["presence"] = self._presence(st, now)
            out["relay"] = self._relay_other_dms(st, now)
            if now >= self._next_hb:                      # tower heartbeat every 120 s
                self._next_hb = now + HEARTBEAT_EVERY_S
                out["tower_heartbeat"] = push_tower_heartbeat(st, now)
            self._verify_due(now)
            _save_state(st)
            self.last = out
            return out

    # A
    def _vector(self, st: dict, now: float) -> dict:
        d = decide_vector(observe_vector(), st, now)
        if d["action"] == "none":
            st.pop("vector_stood_down", None)
            return d
        if d["action"] == "stand_down":
            if not st.get("vector_stood_down"):
                st["vector_stood_down"] = now
                _log("vector_stand_down", why=d["why"])
                dm_zeke("⚠ Vector self-heal stood down (budget spent): " + d["why"] +
                        ". I won't keep retrying; his body needs a look when someone can.")
            return d
        st.setdefault("vector_heals", []).append(now)
        aid = _ledger_open("vector_self_heal", d["action"] + ": " + d["why"],
                           "state/vector/senses_live.json written again after the heal",
                           {"type": "file", "path": str(SENSES), "newer_than_open": True})
        if d["action"] == "start_daemon":
            ok, info = start_daemon()
        else:
            ok, info = vic_restart()
            killed = kill_daemon()
            time.sleep(3)
            ok2, info2 = start_daemon()
            info = f"vic: {info} | killed {killed} | daemon start ok={ok2}"
            ok = ok and ok2
        _log("vector_heal", action=d["action"], why=d["why"], ok=ok, info=info, ledger=aid)
        self._pending_verify.append((now + 240, "vector", aid))
        d.update(ok=ok, info=info, ledger=aid)
        return d

    # E
    def _wirepod(self, st: dict, now: float) -> dict:
        d = decide_wirepod(observe_wirepod(), st, now)
        if d["action"] == "none":
            st.pop("wirepod_stood_down", None)
            return d
        if d["action"] == "stand_down":
            if not st.get("wirepod_stood_down"):
                st["wirepod_stood_down"] = now
                _log("wirepod_stand_down", why=d["why"])
                dm_zeke("⚠ Vector's server (wire-pod) keeps dying and my restart budget is spent. "
                        "His senses still work over the SDK; voice commands to him won't until it's back.")
            return d
        st.setdefault("wirepod_heals", []).append(now)
        aid = _ledger_open("wirepod_heal", d["why"], "wire-pod answers on :8080 again",
                           {"type": "port", "host": "127.0.0.1", "port": 8080})
        ok, info = start_chipper()
        _log("wirepod_heal", why=d["why"], ok=ok, info=info, ledger=aid)
        self._pending_verify.append((now + 60, "wirepod", aid))
        d.update(ok=ok, info=info, ledger=aid)
        return d

    # F
    def _presence(self, st: dict, now: float) -> dict:
        d = decide_presence(observe_presence(), st, now)
        if d["action"] == "none":
            st.pop("presence_stood_down", None)
            return d
        if d["action"] == "stand_down":
            if not st.get("presence_stood_down"):       # log only — Zeke 10-07: no alert spam
                st["presence_stood_down"] = now
                _log("presence_stand_down", why=d["why"])
            return d
        st.setdefault("presence_heals", []).append(now)
        aid = _ledger_open("presence_heal", d["action"] + ": " + d["why"],
                           "state/zeke_presence.json written again after the heal",
                           {"type": "file", "path": str(PRESENCE_STATE), "newer_than_open": True})
        killed = kill_presence() if d["action"] == "restart" else 0
        if killed:
            time.sleep(2)
        ok, info = start_presence()
        _log("presence_heal", action=d["action"], why=d["why"], killed=killed, ok=ok, info=info, ledger=aid)
        self._pending_verify.append((now + 150, "presence", aid))
        d.update(ok=ok, info=info, killed=killed, ledger=aid)
        return d

    # B
    def _orb(self, st: dict, now: float) -> dict:
        d = decide_orb(port_ok(), st, now)
        if d["action"] == "none":
            st.pop("orb_stood_down", None)
            return d
        if d["action"] == "stand_down":
            if not st.get("orb_stood_down"):
                st["orb_stood_down"] = now
                _log("orb_stand_down", why=d["why"])
                dm_zeke("⚠ My orb app's connection (:5876) is down and my auto-restart budget is spent. "
                        "Discord still works; the orb needs a stack restart when someone can.")
            return d
        st.setdefault("orb_heals", []).append(now)
        falsifier = self._orb_falsifier()
        aid = _ledger_open("orb_listener_heal", d["why"], ":5876 accepts connections again",
                           {"type": "port", "host": "127.0.0.1", "port": 5876})
        steps = []
        try:
            from tools.tool_registry import _REGISTRY
            for name, params in (("restart_orb_http", {}), ("orb_http_bounce", {"action": "restart", "force": True})):
                td = _REGISTRY.get(name)
                if td is None:
                    steps.append(f"{name}: missing")
                    continue
                r = td.handler(params, self.g)
                steps.append(f"{name}: ok={r.get('ok')}")
                time.sleep(5)
                if port_ok():
                    break
        except Exception as e:  # noqa: BLE001
            steps.append(f"error {e!r}"[:160])
        healed = port_ok()
        st["orb_fails"] = 0 if healed else st.get("orb_fails", 0)
        _log("orb_heal", why=d["why"], steps=steps, healed=healed, falsifier=falsifier, ledger=aid)
        self._record_orb_goal_step(falsifier, healed)
        self._pending_verify.append((now + 30, "orb", aid))
        d.update(steps=steps, healed=healed, falsifier=falsifier, ledger=aid)
        return d

    def _orb_falsifier(self) -> dict:
        try:
            tail = ORB_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
            hits = [l for l in tail if "Accept failed on a socket" in l]
            return {"accept_failed_lines": len(hits), "last": hits[-1][:240] if hits else None}
        except Exception as e:  # noqa: BLE001
            return {"error": repr(e)[:120]}

    def _record_orb_goal_step(self, falsifier: dict, healed: bool) -> None:
        try:
            from tools.tool_registry import _REGISTRY
            td = _REGISTRY.get("goal")
            if td is not None:
                td.handler({"action": "step", "id": GOAL_ORB,
                            "note": f"AUTO (resilience supervisor): :5876 listener lost and "
                                    f"{'healed' if healed else 'NOT healed'}; falsifier = {falsifier}"}, self.g)
        except Exception:
            pass

    # C
    def _pairing(self, st: dict, now: float) -> list[dict]:
        try:
            pending = json.loads(ACCESS_JSON.read_text(encoding="utf-8")).get("pending") or {}
        except Exception:
            return []
        new = decide_pairing(pending, st, now)
        for n in new:
            who = discord_username(n["sender_id"]) if n["sender_id"] else "someone"
            mins = n.get("minutes_left")
            sent = dm_zeke(
                f"🔔 **{who}** is trying to message me on Discord and needs your approval. "
                f"Pairing code **{n['code']}** (expires in ~{mins} min). If it's your mom, approve it from "
                f"my terminal with /discord:access (Parsec works). If you don't know them, ignore it and the "
                f"code dies on its own.")
            st["pairing_alerted"][n["code"]] = now
            _log("pairing_alert", code=n["code"], sender=n["sender_id"], who=who, sent=sent)
        return new

    # D (2026-10-05): the tower host only POLLS ZEKE'S DM channel; the plugin's gateway
    # notifications are not surfaced to the SDK session. So an approved second person
    # (his mother) would reach the bot but never reach ME. Until the host itself polls
    # every allowlisted DM (restart-gated fix), relay their messages through the chat
    # bridge, clearly labelled NOT Zeke, with the chat_id to answer on.
    def _relay_other_dms(self, st: dict, now: float) -> list:
        try:
            allow = json.loads(ACCESS_JSON.read_text(encoding="utf-8")).get("allowFrom") or []
        except Exception:
            return []
        others = [str(u) for u in allow if str(u) != ZEKE_USER_ID]
        if not others:
            return []
        chans = st.setdefault("relay_channels", {})
        curs = st.setdefault("relay_cursor", {})
        names = st.setdefault("relay_names", {})
        relayed = []
        for uid in others:
            if uid not in chans:
                ch = _discord("POST", "/users/@me/channels", {"recipient_id": uid}) or {}
                if not ch.get("id"):
                    continue
                chans[uid] = ch["id"]
                names[uid] = discord_username(uid)
            cid = chans[uid]
            q = f"/channels/{cid}/messages?limit=20" + (f"&after={curs[uid]}" if curs.get(uid) else "")
            msgs = _discord("GET", q)
            if not isinstance(msgs, list):
                continue
            todo, newest = decide_relay(msgs, uid, curs.get(uid), now)
            if newest:
                curs[uid] = newest
            for m in todo:
                header = (f"[DISCORD DM from {names.get(uid, uid)} (user {uid}) — NOT Zeke; relayed by "
                          f"the resilience supervisor because the host only polls Zeke's DM. Reply to them "
                          f"with the discord reply tool, chat_id {cid}"
                          + (f" (message {m['id']} has {m['attachments']} attachment(s): "
                             f"download_attachment chat_id {cid} message_id {m['id']})" if m["attachments"] else "")
                          + ". Then chat_reply this request with a one-line log. If this is Zeke's mother, "
                            "open her card under profiles/ first; Zeke's whereabouts are HIS to share.]" + chr(10))
                try:
                    from brain import iris_chat
                    rid = iris_chat.submit(header + (m["text"] or "(no text — attachment only)"))
                    relayed.append({"uid": uid, "msg": m["id"], "request": rid})
                    _log("dm_relayed", uid=uid, msg=m["id"], request=rid)
                except Exception as e:  # noqa: BLE001
                    _log("dm_relay_error", uid=uid, msg=m["id"], error=repr(e)[:160])
        return relayed

    def _verify_due(self, now: float) -> None:
        keep = []
        for due, kind, aid in self._pending_verify:
            if now < due:
                keep.append((due, kind, aid))
                continue
            r = _ledger_verify(aid)
            _log("heal_verify", kind=kind, ledger=aid,
                 outcome=((r or {}).get("row") or {}).get("outcome") or ("open" if r else None))
        self._pending_verify = keep


_SUP: Optional[Supervisor] = None


def get(g: dict | None = None) -> Supervisor:
    global _SUP
    if _SUP is None:
        _SUP = Supervisor(g)
    elif g is not None and not _SUP.g:
        _SUP.g = g
    return _SUP


def status() -> dict:
    s = get()
    st = _load_state()
    now = time.time()
    return {"running": s.running(), "last": s.last,
            "vector_heals_6h": sum(1 for t in st.get("vector_heals", []) if now - t < VECTOR_BUDGET[1]),
            "orb_heals_1h": sum(1 for t in st.get("orb_heals", []) if now - t < ORB_BUDGET[1]),
            "pairing_alerted": st.get("pairing_alerted", {}),
            "wirepod_heals_6h": sum(1 for t in st.get("wirepod_heals", []) if now - t < WIREPOD_BUDGET[1]),
            "presence_heals_6h": sum(1 for t in st.get("presence_heals", []) if now - t < PRESENCE_BUDGET[1]),
            "stood_down": {k: st[k] for k in ("vector_stood_down", "orb_stood_down", "wirepod_stood_down",
                                              "presence_stood_down")
                           if st.get(k)}}
