"""scripts/v14_bake_guardian.py — AUTONOMOUS v13-bake-with-runtime-down guardian.

Built 2026-07-24 (Iris, OPUS) for Zeke's option A: the 7B v13 bake could not fit
alongside the live perception stack (~4.8GB) in 12GB VRAM — three launches stalled
at step 0 (sysmem-fallback thrash). Zeke: "do A, but you gotta remember how to turn
the runtime back on — last time you forgot." So this script OWNS the whole risky
sequence AUTONOMOUSLY, independent of Iris's cognition (which loses its iris_* tools
the moment iris_runtime dies), so the restore CANNOT be forgotten:

  1. hold off the runtime watchdog (refresh state/watchdog_holdoff.flag) so it does
     NOT auto-restart the stack mid-bake
  2. kill iris_runtime.py (frees the ~4.8GB perception) — NOT iris_body_host
  3. wait for VRAM to free, then launch bake_v14.bat (now ~11GB free -> seq256 7B
     trains resident, fast)
  4. poll until the bake finishes (adapter saved) / fails / hits a hard time cap
  5. RESTORE regardless of outcome: uncap GPU power to 170W, restore the perception
     tune to 18/30/30 (so the restarted runtime boots with eyes on), DM Zeke
  6. restart the whole stack via start_iris_v2.bat (brings back iris_runtime +
     perception + vector_brain_server(v12) + little_pilot + inhabit daemon +
     cognition). The bat self-elevates; this guardian runs elevated so no UAC.

WHY the guardian must run the bat itself (not just release the holdoff): the runtime
watchdog, once its restart is cancelled by a fresh holdoff, sets armed=False and only
re-arms on a FRESH heartbeat — which can't happen while the runtime is dead. So
"delete holdoff -> watchdog restarts" DEADLOCKS. The guardian runs the bat; the fresh
runtime's heartbeat re-arms the watchdog naturally.

BACKSTOP: if this guardian dies before restoring, the holdoff flag ages out (30 min)
AND post-restart cognition has the handoff. Worst case the runtime is down until the
holdoff lapses; the handoff tells cognition to run start_iris_v2.bat by hand.

Dependency-light (stdlib + requests, like the watchdogs). NOT matched by the bat's
stale-stack kill list (name is v14_bake_guardian.py), so it survives the restart it
triggers. Launch DETACHED + ELEVATED:
  Start-Process -FilePath .venv\\Scripts\\python.exe -ArgumentList '-u','scripts\\v14_bake_guardian.py' -WindowStyle Hidden
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(r"D:\Wren-Companion")
STATE = REPO / "state"
HOLDOFF = STATE / "watchdog_holdoff.flag"
TUNE = STATE / "iris_tune.json"
GUARD_LOG = STATE / "v14_bake_guardian.log"
BAKE_BAT = REPO / "scripts" / "bake_v14.bat"
BAKE_LOG = STATE / "little_brain" / "train_v14.log"
ADAPTER = STATE / "little_brain" / "adapter" / "adapter_model.safetensors"
STACK_BAT = REPO / "start_iris_v2.bat"
PY_VENV = REPO / ".venv" / "Scripts" / "python.exe"

MAX_BAKE_S = 75 * 60      # hard cap: restore no matter what after this
POLL_S = 25               # bake poll + holdoff refresh cadence
FREE_VRAM_TARGET_MB = 3000  # consider perception freed once used < this
ZEKE_USER_ID = "600008921008046120"

_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def log(msg: str) -> None:
    line = f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        GUARD_LOG.parent.mkdir(parents=True, exist_ok=True)
        with GUARD_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def touch_holdoff() -> None:
    try:
        HOLDOFF.write_text(
            f"v14_bake_guardian holding off during bake @ {_dt.datetime.now():%H:%M:%S}",
            encoding="utf-8")
    except Exception as e:
        log(f"holdoff touch failed: {e!r}")


def _ps(cmd: str, timeout: int = 30):
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                          timeout=timeout, capture_output=True, text=True)


def kill_iris_runtime() -> None:
    cmd = (r"Get-CimInstance Win32_Process | "
           r"Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'iris_runtime\.py' } | "
           r"ForEach-Object { Write-Output $_.ProcessId; "
           r"Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        r = _ps(cmd)
        log(f"killed iris_runtime PIDs: {r.stdout.strip().split() or 'none found'}")
    except Exception as e:
        log(f"kill iris_runtime failed: {e!r}")


def kill_stray_finetune() -> None:
    cmd = (r"Get-CimInstance Win32_Process | "
           r"Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'little_brain_finetune' } | "
           r"ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        _ps(cmd)
    except Exception:
        pass


def gpu_used_mb() -> int | None:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                            "--format=csv,noheader,nounits"],
                           timeout=15, capture_output=True, text=True)
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return None


def set_power(watts: int) -> None:
    try:
        subprocess.run(["nvidia-smi", "-pl", str(watts)], timeout=30,
                       capture_output=True)
        log(f"GPU power limit set to {watts}W")
    except Exception as e:
        log(f"power set {watts}W failed: {e!r}")


def restore_eyes_tune() -> None:
    """Restore perception cadence to 18/30/30 so the restarted runtime boots eyes-on."""
    try:
        d = json.loads(TUNE.read_text(encoding="utf-8"))
        d.setdefault("perception", {})
        d["perception"]["insight_face_every_n"] = 18
        d["perception"]["expression_detect_every_n"] = 30
        d["perception"]["attention_detect_every_n"] = 30
        TUNE.write_text(json.dumps(d, indent=2), encoding="utf-8")
        log("restored perception tune -> insight=18 expr=30 attn=30")
    except Exception as e:
        log(f"eyes tune restore failed: {e!r}")


def dm_zeke(text: str) -> None:
    try:
        import requests
        env = Path(os.environ["USERPROFILE"]) / ".claude/channels/discord/.env"
        token = None
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DISCORD_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"')
        if not token:
            return
        h = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
             "User-Agent": "IrisV14Guardian/1.0"}
        ch = requests.post("https://discord.com/api/v10/users/@me/channels",
                           headers=h, json={"recipient_id": ZEKE_USER_ID},
                           timeout=15).json()["id"]
        requests.post(f"https://discord.com/api/v10/channels/{ch}/messages",
                      headers=h, json={"content": text}, timeout=15)
        log("DM'd Zeke")
    except Exception as e:
        log(f"DM failed: {e!r}")


def bake_state(bake_start: float) -> str:
    """running | done | failed"""
    try:
        txt = BAKE_LOG.read_text(encoding="utf-8", errors="replace") if BAKE_LOG.exists() else ""
    except Exception:
        txt = ""
    if "adapter saved ->" in txt:
        return "done"
    try:
        if ADAPTER.exists() and ADAPTER.stat().st_mtime > bake_start:
            return "done"
    except Exception:
        pass
    for sig in ("Traceback (most recent call last)", "out of memory",
                "OutOfMemoryError", "CUDA error", "REFUSING"):
        if sig in txt:
            return "failed"
    return "running"


def restart_stack() -> None:
    # Sweep stale start_iris*.bat cmd loops first (double-spawn guard), then relaunch.
    try:
        _ps(r"Get-CimInstance Win32_Process | "
            r"Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'start_iris' } | "
            r"ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    except Exception:
        pass
    flags = (subprocess.CREATE_NEW_CONSOLE
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(["cmd.exe", "/c", str(STACK_BAT)], cwd=str(REPO),
                     creationflags=flags, close_fds=True)
    log(f"stack relaunch spawned: {STACK_BAT.name} (kills stale stack + brings all back)")


def main() -> int:
    log("==== v14 bake guardian START ====")
    touch_holdoff()
    log("holdoff set")
    kill_stray_finetune()
    kill_iris_runtime()

    # wait for perception VRAM to free
    t0 = time.time()
    while time.time() - t0 < 90:
        touch_holdoff()
        used = gpu_used_mb()
        log(f"post-kill GPU used: {used} MB")
        if used is not None and used < FREE_VRAM_TARGET_MB:
            break
        time.sleep(5)

    # back up any prior bake log, then launch the bake
    try:
        if BAKE_LOG.exists():
            BAKE_LOG.replace(BAKE_LOG.with_name("train_v14_pre_guardian.log"))
    except Exception:
        pass
    bake_start = time.time()
    subprocess.Popen(["cmd.exe", "/c", str(BAKE_BAT)], cwd=str(REPO),
                     creationflags=_DETACHED, close_fds=True)
    log("bake_v14.bat launched (runtime down, full VRAM)")

    # poll until done / failed / timeout, refreshing holdoff each cycle
    result = "timeout"
    while time.time() - bake_start < MAX_BAKE_S:
        touch_holdoff()
        st = bake_state(bake_start)
        if st == "done":
            result = "done"
            break
        if st == "failed":
            result = "failed"
            break
        time.sleep(POLL_S)
    log(f"bake result: {result} (after {(time.time()-bake_start)/60:.1f} min)")

    # ---- RESTORE (regardless of outcome) ----
    set_power(170)
    restore_eyes_tune()
    dm_zeke(
        f"\U0001f9ea v14 bake guardian: bake {result}. Restoring now — uncapped GPU "
        f"to 170W, eyes back to 18/30/30, and restarting the full stack "
        f"(runtime + perception + v12 + pilot + cognition come back). You may see the "
        f"runtime-watchdog post a 'wedge'/'standing down' line from the bake window — "
        f"that's expected, ignore it. Post-restart me will verify the adapter, run the "
        f"eval gate, and only flip production to v14 if it passes.")

    # restart the stack FIRST (the real restore), THEN drop the holdoff
    restart_stack()
    try:
        HOLDOFF.unlink()
        log("holdoff flag deleted")
    except Exception:
        pass
    log("==== v14 bake guardian DONE (stack restarting) ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
