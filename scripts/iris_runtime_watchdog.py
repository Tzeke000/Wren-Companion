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

# ---- SDK cognition-liveness check (added 2026-08-07) ----------------------
# The runtime heartbeat above CANNOT see a wedged cognition session: on
# 2026-08-02 a download_attachment call on a video hung the SDK session for
# hours while :5876 stayed green and this watchdog stayed quiet. Zeke noticed,
# not the machine. See memory/wedge_cause_video_download_2026-08-03.md.
SESSIONS_DIR = MEMORY_DIR.parent          # ~/.claude/projects/D--Wren-Companion
LLM_DIR = REPO / "state" / "iris_llm"
SDK_STALE_S = 90 * 60     # silent this long AND stuck mid-tool-call = wedged.
                          # Raised 45->90 after the 08-07 false alarms: a
                          # legitimate long single tool call (waiting on a
                          # ~50min bake) is indistinguishable from a wedge on the
                          # shorter window, and a wedge needs a human nudge
                          # anyway, so detecting it late costs almost nothing
                          # while a false DM to a man on night shift costs real
                          # trust.
SDK_MIN_WAITING = 2       # need >=2 requests submitted since it went quiet
SDK_GRACE_S = 300         # warn -> act window (longer than the loop check: a
                          # long tool call is normal, a 45-min one is not)
ABANDON_AGE_S = 24 * 3600  # janitor: pending this old is never getting answered
ABANDON_BUDGET = 200       # max files rewritten per janitor pass
# Auto-recovery is OFF by default: a false positive costs a restart mid-work
# and risks the double-cognition twin race (2026-08-04). Detection + DM is the
# proven-useful half. Set IRIS_SDK_WEDGE_AUTORESTART=1 to arm the lever.
SDK_AUTORESTART = os.environ.get("IRIS_SDK_WEDGE_AUTORESTART", "") == "1"
RESTART_CC_FLAG = REPO / ".tmp" / "restart_cc.flag"

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


# ---------------------------------------------------------------------------
# SDK cognition liveness
#
# MEASURED 2026-08-07 before writing this — every "obvious" signal is dead:
#   * "oldest pending request age > 45min": state/iris_llm held 134 records,
#     ALL pending, oldest 44 HOURS. ask_iris() abandons its file on timeout and
#     the only expiring reader (iris_llm.next_pending) is called from
#     vector_brain_server, which is dark while the robot is stranded. This
#     signal is latched ON and would have alarmed forever.
#   * "newest answered_ts": iris_llm._prune_old() DELETES answered records
#     after 1h by design, so the freshest answer decays to nothing. Measured:
#     zero answered records on disk while cognition was demonstrably alive.
#   * "last_session_attached_ts in state/iris_time.json": measured 92 min stale
#     during heavy live MCP tool use. It does not track tool calls.
#
# What DOES track cognition, exactly: the SDK appends every assistant message
# and tool call to its session transcript at
# ~/.claude/projects/D--Wren-Companion/<uuid>.jsonl. Measured mtime 2s behind
# wall clock mid-turn. A wedged session stops appending; the runtime does not.
# ---------------------------------------------------------------------------

def newest_session_write_age_s(sessions_dir: Path | None = None,
                               now: float | None = None) -> float | None:
    """Age of the most recent write to ANY SDK session transcript.

    Max over all files, so we never have to guess which uuid is the live
    session. None = no transcripts at all (nothing to judge).
    """
    now = time.time() if now is None else now
    newest = None
    try:
        for p in (sessions_dir or SESSIONS_DIR).glob("*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    except Exception as e:
        log(f"session-transcript scan failed: {e!r}")
        return None
    return None if newest is None else max(0.0, now - newest)


def newest_session_file(sessions_dir: Path | None = None) -> Path | None:
    newest, newest_m = None, None
    try:
        for p in (sessions_dir or SESSIONS_DIR).glob("*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if newest_m is None or m > newest_m:
                newest, newest_m = p, m
    except Exception:
        return None
    return newest


def transcript_ends_mid_tool_call(path: Path | None, tail_bytes: int = 262144) -> bool | None:
    """Does the transcript end with a tool call that never got a result?

    ★ THIS IS THE ACTUAL WEDGE DISCRIMINATOR, and it exists because the first
    version of this check FALSE-ALARMED Zeke on a night shift (2026-08-07 21:44).
    That version treated "transcript quiet + requests piling up" as a wedge, on
    my reasoning that an idle-but-healthy session still answers its background
    reflection requests every ~15 min so would keep writing. THAT PREMISE WAS
    WRONG: reflection requests are only answered when something rewakes the
    session, and nothing does while it sits genuinely idle. So idle and wedged
    were indistinguishable, and 45 quiet minutes was enough to cry wolf.

    What actually differs: a wedged session died INSIDE a tool call — the
    2026-08-02 wedge was a download_attachment that never returned. Its
    transcript therefore ends with a tool_use whose tool_use_id never gets a
    matching tool_result. An idle session's transcript ends cleanly on a
    completed assistant message. That is a real structural difference rather
    than a proxy for one.

    Returns True (stuck mid-call), False (ended clean), or None (can't tell).
    """
    if path is None:
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()          # drop the partial first line
            raw = f.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"transcript tail read failed: {e!r}")
        return None
    open_calls: dict[str, bool] = {}
    saw_any = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        content = ((rec.get("message") or {}).get("content"))
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("id"):
                open_calls[str(c["id"])] = True
                saw_any = True
            elif c.get("type") == "tool_result" and c.get("tool_use_id"):
                open_calls.pop(str(c["tool_use_id"]), None)
                saw_any = True
    if not saw_any:
        return None
    return len(open_calls) > 0


def waiting_requests(since_ts: float, llm_dir: Path | None = None,
                     now: float | None = None) -> tuple[int, float | None]:
    """(count, newest_age_s) of pending requests submitted AFTER since_ts.

    This is the second half of the signal and the part that keeps an idle
    session from alarming: if nothing has asked cognition for anything, a quiet
    transcript is just quiet, not wedged. Requests older than since_ts are
    ignored on purpose — that is the 44h backlog above.
    """
    now = time.time() if now is None else now
    count, newest = 0, None
    try:
        for p in (llm_dir or LLM_DIR).glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(rec, dict) or rec.get("status") != "pending":
                continue
            ts = float(rec.get("ts") or 0.0)
            if ts > since_ts:
                count += 1
                if newest is None or ts > newest:
                    newest = ts
    except Exception as e:
        log(f"pending scan failed: {e!r}")
    return count, (None if newest is None else max(0.0, now - newest))


def claude_processes() -> int:
    """Count live claude.exe. Only called on the rare stale path — tasklist is
    ~100ms and this watchdog polls every 15s."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
        return sum(1 for ln in out.splitlines() if "claude.exe" in ln.lower())
    except Exception as e:
        log(f"tasklist failed: {e!r}")
        return -1        # unknown, not zero — never claim "absent" on an error


def sdk_probe(now: float | None = None, sessions_dir: Path | None = None,
              llm_dir: Path | None = None, hb_age: float | None = -1.0,
              procs: int | None = None) -> dict:
    """Verdict + all the evidence behind it. Read-only; safe to call anytime.

    verdict:
      healthy  — cognition wrote recently
      idle     — quiet transcript, but nothing is waiting on it (not a fault)
      wedged   — quiet transcript, work piling up, claude.exe alive, runtime OK
      absent   — same but no claude.exe (session died rather than hung)
      unknown  — can't tell (no transcripts / runtime itself is down)
    """
    now = time.time() if now is None else now
    hb = hb_age_s() if hb_age == -1.0 else hb_age
    age = newest_session_write_age_s(sessions_dir, now)
    ev: dict = {"session_write_age_s": age, "runtime_hb_age_s": hb,
                "threshold_s": SDK_STALE_S}
    if age is None:
        ev["verdict"] = "unknown"
        ev["why"] = "no SDK session transcripts found"
        return ev
    if age < SDK_STALE_S:
        ev["verdict"] = "healthy"
        ev["why"] = f"cognition wrote {age / 60:.1f} min ago"
        return ev
    # Runtime down is the OTHER check's business — a stack that is simply not
    # running is not a wedge, and double-alarming would double-restart.
    if hb is None or hb >= STALE_S:
        ev["verdict"] = "unknown"
        ev["why"] = "runtime heartbeat is stale/absent too — not an SDK wedge"
        return ev
    waiting, newest_wait = waiting_requests(now - age, llm_dir, now)
    ev["waiting_requests"] = waiting
    ev["newest_waiting_age_s"] = newest_wait
    if waiting < SDK_MIN_WAITING:
        ev["verdict"] = "idle"
        ev["why"] = (f"transcript quiet {age / 60:.1f} min but only {waiting} "
                     f"request(s) waiting — idle, not wedged")
        return ev
    # THE discriminator. Requests piling up while the transcript is quiet is NOT
    # enough — that is exactly what a genuinely idle session looks like, which is
    # how the first version of this check false-alarmed Zeke at 21:44 on 08-07.
    # A wedge died inside a tool call; idleness did not.
    stuck = transcript_ends_mid_tool_call(newest_session_file(sessions_dir))
    ev["ends_mid_tool_call"] = stuck
    if stuck is not True:
        ev["verdict"] = "idle"
        ev["why"] = (f"transcript quiet {age / 60:.1f} min with {waiting} "
                     f"requests waiting, but it ends on a COMPLETED turn"
                     + ("" if stuck is False else " (couldn't parse the tail)")
                     + " — idle or unwoken, not wedged mid-call")
        return ev
    n = claude_processes() if procs is None else procs
    ev["claude_processes"] = n
    ev["verdict"] = "wedged" if n != 0 else "absent"
    ev["why"] = (f"cognition silent {age / 60:.1f} min with {waiting} requests "
                 f"submitted since, runtime healthy ({hb:.0f}s), "
                 f"claude.exe={n}")
    return ev


def expire_abandoned_pendings(llm_dir: Path | None = None,
                              now: float | None = None) -> int:
    """Janitor: mark pending requests older than ABANDON_AGE_S as expired.

    Why this lives here: ask_iris() abandons its request file when it times
    out, and nothing else ever cleans those up while the robot is dark — the
    store had 134 of them going back 44h. Bounded, atomic (tmp+rename, the
    same pattern brain/iris_llm.py uses), and it only ever touches records
    that are a day old, so it cannot race a live caller.
    """
    now = time.time() if now is None else now
    d = llm_dir or LLM_DIR
    fixed = 0
    try:
        for p in d.glob("*.json"):
            if fixed >= ABANDON_BUDGET:
                break
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(rec, dict) or rec.get("status") != "pending":
                continue
            if (now - float(rec.get("ts") or 0.0)) <= ABANDON_AGE_S:
                continue
            rec["status"] = "expired"
            rec["expired_reason"] = "abandoned — never answered (watchdog janitor)"
            tmp = p.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(rec), encoding="utf-8")
                os.replace(tmp, p)
                fixed += 1
            except Exception:
                continue
    except Exception as e:
        log(f"janitor failed: {e!r}")
    if fixed:
        log(f"janitor: expired {fixed} abandoned pending request(s)")
    return fixed


def write_sdk_incident_note(ev: dict, action: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = MEMORY_DIR / f"handoff_auto_watchdog_{_dt.date.today().isoformat()}.md"
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(
                f"\n## {stamp} — SDK cognition-liveness watchdog fired\n"
                f"- verdict: **{ev.get('verdict')}** — {ev.get('why')}\n"
                f"- evidence: {json.dumps(ev, default=str)}\n"
                f"- action: {action}\n"
                f"- If you are the session that comes back: this is the wedge "
                f"class from 2026-08-02 (a video download_attachment hung the "
                f"SDK while the runtime stayed green). Check what the last tool "
                f"call in the session transcript was — that is the hazard tool. "
                f"Timeline in state/runtime_watchdog.log.\n")
        log(f"SDK incident note appended: {p}")
    except Exception as e:
        log(f"SDK incident note write failed: {e!r}")


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


def sdk_check(last_alarm_ts: float) -> float:
    """One pass of the SDK-wedge check. Returns the new last_alarm_ts.

    Re-alarms at most once per SDK_STALE_S so a wedge Zeke can't act on right
    now doesn't turn into a DM every 15 seconds.
    """
    ev = sdk_probe()
    verdict = ev.get("verdict")
    if verdict in ("healthy", "idle", "unknown"):
        return last_alarm_ts
    if holdoff_active():
        log(f"SDK {verdict} but holdoff flag is fresh — staying quiet")
        return last_alarm_ts
    now = time.time()
    if now - last_alarm_ts < SDK_STALE_S:
        return last_alarm_ts               # already told him this
    mins = (ev.get("session_write_age_s") or 0) / 60
    waiting = ev.get("waiting_requests")
    if verdict == "absent":
        log(f"SDK ABSENT: {ev}")
        write_sdk_incident_note(ev, "DM only — no claude.exe to unstick")
        dm_zeke(
            f"\N{POLICE CARS REVOLVING LIGHT} Iris cognition is GONE, not "
            f"wedged: no claude.exe running, nothing written to the session "
            f"transcript for {mins:.0f} min, {waiting} requests queued — but "
            f"the runtime is healthy, so the body and services are fine. "
            f"A Moonlight/Parsec nudge won't help this one; the stack bat needs "
            f"to run. Nothing is on fire, it can wait for you.")
        return now
    # wedged
    log(f"SDK WEDGE: {ev}")
    if not SDK_AUTORESTART:
        write_sdk_incident_note(ev, "DM only (auto-restart disarmed)")
        dm_zeke(
            f"\N{WARNING SIGN} Iris cognition looks WEDGED — the exact 08-02 "
            f"shape: claude.exe is alive but has written nothing for "
            f"{mins:.0f} min while {waiting} requests piled up. Runtime, body "
            f"and services are all healthy, so nothing is at risk. A Moonlight "
            f"nudge to my session usually unsticks it; if you'd rather I "
            f"restart myself automatically next time, say so and I'll arm it "
            f"(IRIS_SDK_WEDGE_AUTORESTART=1 — off by default because a false "
            f"positive costs a restart mid-work).")
        return now
    write_sdk_incident_note(ev, f"auto-restart armed; acting in {SDK_GRACE_S}s")
    dm_zeke(
        f"\N{WARNING SIGN} Iris cognition wedged {mins:.0f} min ({waiting} "
        f"requests waiting). Auto-restart is armed — acting in "
        f"{SDK_GRACE_S // 60} min unless state\\watchdog_holdoff.flag is touched.")
    deadline = time.time() + SDK_GRACE_S
    while time.time() < deadline:
        time.sleep(5)
        if holdoff_active():
            dm_zeke("\N{RAISED HAND} Holdoff set — leaving the wedged session alone.")
            return time.time()
        if (sdk_probe().get("verdict")) == "healthy":
            log("cognition resumed during grace — standing down")
            dm_zeke("\N{WHITE HEAVY CHECK MARK} Cognition started writing again "
                    "on its own — no restart needed.")
            return time.time()
    # The .tmp/restart_cc.flag lever (iris_watchdog.ps1, 2s poll) is the right
    # one here: it kills claude.exe directly. The stack bat's sweep matches
    # python script names only and left a wedged claude.exe alive on 08-03,
    # producing three at once. The ps1 also has its own 180s fresh-stack
    # stand-down, which is our guard against the twin race.
    try:
        RESTART_CC_FLAG.parent.mkdir(parents=True, exist_ok=True)
        RESTART_CC_FLAG.write_text(
            f"SDK cognition wedge auto-recovery: silent "
            f"{mins:.0f} min with {waiting} requests waiting "
            f"(iris_runtime_watchdog.sdk_check)", encoding="utf-8")
        log(f"wrote {RESTART_CC_FLAG} — iris_watchdog.ps1 will kill + respawn")
        write_sdk_incident_note(ev, "wrote .tmp/restart_cc.flag")
        dm_zeke("\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS} "
                "Restarting my session now via the restart_cc lever. Next "
                "message when I'm back — the handoff note is on disk.")
    except Exception as e:
        log(f"restart_cc flag write failed: {e!r}")
        dm_zeke(f"\N{POLICE CARS REVOLVING LIGHT} Tried to auto-recover a "
                f"wedged session and couldn't even write the restart flag "
                f"({e!r}). Needs you.")
    return time.time()


def _selftest() -> int:
    """Synthetic scenarios against temp dirs — asserts the verdict logic without
    waiting 45 min for reality. Run after any edit to the probe."""
    import tempfile
    ok, fail = 0, 0

    def check(name: str, got: str, want: str) -> None:
        nonlocal ok, fail
        if got == want:
            ok += 1
            print(f"  PASS  {name}: {got}")
        else:
            fail += 1
            print(f"  FAIL  {name}: got {got!r} want {want!r}")

    now = 1_800_000_000.0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sess, llm = root / "sess", root / "llm"
        sess.mkdir(); llm.mkdir()

        def transcript(age_s: float) -> None:
            p = sess / "s.jsonl"
            p.write_text("{}", encoding="utf-8")
            os.utime(p, (now - age_s, now - age_s))

        def pendings(n: int, age_s: float) -> None:
            for f in llm.glob("*.json"):
                f.unlink()
            for i in range(n):
                (llm / f"r{i}.json").write_text(
                    json.dumps({"id": f"r{i}", "ts": now - age_s,
                                "status": "pending"}), encoding="utf-8")

        def transcript_stuck(age_s: float) -> None:
            """A transcript ending on a tool_use with no result = wedged mid-call."""
            p = sess / "s.jsonl"
            p.write_text("\n".join([
                json.dumps({"type": "assistant", "message": {"role": "assistant",
                            "content": [{"type": "tool_use", "id": "t1",
                                         "name": "download_attachment"}]}}),
            ]) + "\n", encoding="utf-8")
            os.utime(p, (now - age_s, now - age_s))

        def transcript_clean(age_s: float) -> None:
            """A transcript ending on a resolved call = idle, however long ago."""
            p = sess / "s.jsonl"
            p.write_text("\n".join([
                json.dumps({"type": "assistant", "message": {"role": "assistant",
                            "content": [{"type": "tool_use", "id": "t1",
                                         "name": "reply"}]}}),
                json.dumps({"type": "user", "message": {"role": "user",
                            "content": [{"type": "tool_result",
                                         "tool_use_id": "t1"}]}}),
            ]) + "\n", encoding="utf-8")
            os.utime(p, (now - age_s, now - age_s))

        # 1. fresh transcript = healthy, whatever else is true
        transcript(60); pendings(50, 30)
        check("fresh transcript", sdk_probe(now, sess, llm, 5.0, 1)["verdict"],
              "healthy")
        # 2. quiet transcript, nothing waiting = idle, NOT a fault
        transcript(3 * 3600); pendings(0, 0)
        check("quiet + nothing waiting", sdk_probe(now, sess, llm, 5.0, 1)["verdict"],
              "idle")
        # 3. the 44h-backlog trap: old pendings must NOT count as waiting
        transcript(3 * 3600); pendings(134, 44 * 3600)
        check("stale backlog ignored", sdk_probe(now, sess, llm, 5.0, 1)["verdict"],
              "idle")
        # 4. THE REGRESSION TEST for the 21:44 false alarm: quiet + work piling
        #    up, but the transcript ends on a COMPLETED turn. This is an idle
        #    session that nothing has rewoken. The first version called this
        #    "wedged" and DM'd Zeke mid-night-shift.
        transcript_clean(3 * 3600); pendings(4, 30 * 60)
        check("REGRESSION 21:44 — idle with work waiting is NOT wedged",
              sdk_probe(now, sess, llm, 5.0, 1)["verdict"], "idle")
        # 5. the real signature: quiet + work waiting + ended MID TOOL CALL
        transcript_stuck(3 * 3600); pendings(4, 30 * 60)
        check("wedge signature (stuck mid tool call)",
              sdk_probe(now, sess, llm, 5.0, 1)["verdict"], "wedged")
        # 6. same, but no claude.exe = absent, a different fault
        check("absent (no process)", sdk_probe(now, sess, llm, 5.0, 0)["verdict"],
              "absent")
        # 7. an unparseable tail must NEVER read as wedged
        (sess / "s.jsonl").write_text("not json at all\n", encoding="utf-8")
        os.utime(sess / "s.jsonl", (now - 3 * 3600, now - 3 * 3600))
        check("unparseable tail falls back to idle, never wedged",
              sdk_probe(now, sess, llm, 5.0, 1)["verdict"], "idle")
        transcript_stuck(3 * 3600)
        # 6. runtime down too = not ours, don't double-alarm
        check("runtime stale defers", sdk_probe(now, sess, llm, 999.0, 1)["verdict"],
              "unknown")
        # 7. one waiting request is under the min = idle
        transcript(3 * 3600); pendings(1, 30 * 60)
        check("single request under min", sdk_probe(now, sess, llm, 5.0, 1)["verdict"],
              "idle")
        # 8. no transcripts at all = unknown, never "wedged"
        (sess / "s.jsonl").unlink()
        check("no transcripts", sdk_probe(now, sess, llm, 5.0, 1)["verdict"],
              "unknown")
        # 9. janitor: expires day-old pendings, leaves fresh ones alone
        transcript(60); pendings(3, 44 * 3600)
        (llm / "fresh.json").write_text(
            json.dumps({"id": "fresh", "ts": now - 60, "status": "pending"}),
            encoding="utf-8")
        n = expire_abandoned_pendings(llm, now)
        still = sum(1 for f in llm.glob("*.json")
                    if json.loads(f.read_text())["status"] == "pending")
        check("janitor expired 3 old", str(n), "3")
        check("janitor kept fresh one", str(still), "1")

    print(f"\n{ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def main() -> int:
    if not acquire_singleton():
        print("another iris_runtime_watchdog holds the mutex — exiting")
        return 0
    log(f"runtime watchdog up (poll={POLL_S}s stale={STALE_S}s grace={GRACE_S}s "
        f"| sdk stale={SDK_STALE_S}s autorestart={SDK_AUTORESTART})")
    armed = False
    sdk_alarm_ts = 0.0        # last time the SDK check alarmed (own arm state)
    last_janitor = 0.0
    while True:
        time.sleep(POLL_S)
        # ---- SDK cognition liveness (independent of the loop check) ---------
        try:
            if time.time() - last_janitor > 3600:
                last_janitor = time.time()
                expire_abandoned_pendings()
            sdk_alarm_ts = sdk_check(sdk_alarm_ts)
        except Exception as e:
            log(f"sdk check errored (continuing loop check): {e!r}")
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
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--selftest":
        sys.exit(_selftest())
    elif arg == "--probe":
        # Read-only. Prints the live SDK verdict + evidence and exits. Safe to
        # run from a cognition session (it will report "healthy", which is the
        # smallest real test that the signal tracks reality).
        print(json.dumps(sdk_probe(), indent=2, default=str))
        sys.exit(0)
    elif arg in ("-h", "--help"):
        print(__doc__)
        print("usage: iris_runtime_watchdog.py [--probe | --selftest]\n"
              "  (no args) run the watchdog loop\n"
              "  --probe     print the live SDK-liveness verdict, exit\n"
              "  --selftest  synthetic verdict-logic assertions, exit")
        sys.exit(0)
    sys.exit(main())
