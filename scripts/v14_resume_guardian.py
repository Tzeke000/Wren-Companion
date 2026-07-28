"""scripts/v14_resume_guardian.py — resume the stalled v14 bake, runtime-down.

2026-07-28 ~00:0x, Iris. Second-generation of v14_bake_guardian.py, fixing the two
flaws that broke the first bake:

  FLAW 1 (the one that actually killed it): the original's MAX_BAKE_S=75min timeout
  fired at 19:36 and ran the RESTORE — bringing the whole runtime back — while the
  trainer was still alive and training. That handed ~4.8GB of VRAM back to
  perception, pushed the card to 11.8/12.3 GiB, and dropped the trainer into driver
  sysmem-fallback thrash (100% util, ~50W, ~0 steps/s; the documented failure at
  little_brain_finetune.py:58-62). It crawled 725->825 at 36s/step, then made zero
  progress for 2h20m. A timeout must KILL the bake before restoring, never race it.

  FLAW 2: the restore used start_iris_v2.bat, which sweeps and restarts cognition
  too. That is correct for an unattended bake, but it means the session that ordered
  the bake can never do the packaging. This one restores with body_switch.ps1 (the
  idempotent ON SWITCH), so the runtime comes back WITHOUT restarting cognition.

Sequence (owns the whole thing, cognition-independent so the restore can't be lost):
  1. hold off the runtime watchdog (refresh every poll)
  2. kill the stalled trainer, then kill iris_runtime.py (frees perception VRAM)
  3. wait for VRAM to drop, then launch bake_v14_resume.bat (IRIS_LB_RESUME=auto)
  4. poll for "adapter saved ->" / adapter mtime / failure, cap 60 min
  5. on timeout: KILL the trainer FIRST, then restore (flaw-1 fix)
  6. restore: GPU 170W, eyes tune 18/30/30, body_switch.ps1 on, drop holdoff
  7. DM Zeke the TRUE outcome (checks adapter mtime, so a crash reports as a crash)

Launch DETACHED + ELEVATED:
  Start-Process -FilePath .venv\\Scripts\\python.exe -ArgumentList '-u','scripts\\v14_resume_guardian.py' -WindowStyle Hidden
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
GUARD_LOG = STATE / "v14_resume_guardian.log"
BAKE_BAT = REPO / "scripts" / "bake_v14_resume.bat"
BAKE_LOG = STATE / "little_brain" / "train_v14_resume.log"
CKPT_DIR = STATE / "little_brain" / "checkpoints"
ADAPTER = STATE / "little_brain" / "adapter" / "adapter_model.safetensors"
BODY_SWITCH = REPO / "scripts" / "body_switch.ps1"

MAX_BAKE_S = 60 * 60        # 43 steps should take ~10-25 min resident; 60 is slack
POLL_S = 25
FREE_VRAM_TARGET_MB = 3000
ZEKE_USER_ID = "600008921008046120"

_DETACHED = 0x00000008 | 0x00000200


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
            f"v14_resume_guardian holding off during bake @ {_dt.datetime.now():%H:%M:%S}",
            encoding="utf-8")
    except Exception as e:
        log(f"holdoff touch failed: {e!r}")


def _ps(cmd: str, timeout: int = 60):
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                          timeout=timeout, capture_output=True, text=True)


def kill_finetune() -> None:
    """Kill BOTH the venv launcher shim and its real worker child.

    Scar (bit me tonight): the .venv-train python.exe is only a shim with 0 CPU;
    the process actually training is its child under the base Python. Matching on
    the command line catches both.
    """
    cmd = (r"Get-CimInstance Win32_Process | "
           r"Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'little_brain_finetune' } | "
           r"ForEach-Object { Write-Output $_.ProcessId; "
           r"Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        r = _ps(cmd)
        log(f"killed finetune PIDs: {r.stdout.strip().split() or 'none found'}")
    except Exception as e:
        log(f"kill finetune failed: {e!r}")


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


def gpu_used_mb():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                            "--format=csv,noheader,nounits"],
                           timeout=15, capture_output=True, text=True)
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return None


def set_power(watts: int) -> None:
    try:
        subprocess.run(["nvidia-smi", "-pl", str(watts)], timeout=30, capture_output=True)
        log(f"GPU power limit set to {watts}W")
    except Exception as e:
        log(f"power set {watts}W failed: {e!r}")


def restore_eyes_tune() -> None:
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


def last_step() -> str:
    try:
        cps = sorted(CKPT_DIR.glob("checkpoint-*"), key=lambda p: p.stat().st_mtime)
        d = json.loads((cps[-1] / "trainer_state.json").read_text(encoding="utf-8"))
        return f"{d.get('global_step')}/{d.get('max_steps')}"
    except Exception:
        return "unknown"


def bake_state(bake_start: float) -> str:
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


def runtime_alive() -> bool:
    try:
        r = _ps(r"@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
                r"-and $_.CommandLine -match 'iris_runtime\.py' }).Count")
        return int(r.stdout.strip() or 0) > 0
    except Exception:
        return False


def restore_body() -> str:
    """Bring the body back WITHOUT restarting cognition.

    body_switch.ps1 deliberately will NOT start iris_runtime.py when it's absent
    (its own comment: "it would kill a live Iris") — instead the Agent-SDK body
    host respawns the runtime as its MCP child on Iris's next iris_* tool call.
    So: run the switch for orb/postoffice/vector/watchdog, then give the host a
    window to respawn the runtime, and report honestly if it didn't.
    """
    try:
        r = _ps(f"& '{BODY_SWITCH}' on", timeout=300)
        log(f"body_switch on -> rc={r.returncode} {r.stdout.strip()[-400:]}")
    except Exception as e:
        log(f"body_switch failed: {e!r}")
    t0 = time.time()
    while time.time() - t0 < 150:
        if runtime_alive():
            log("iris_runtime respawned by the body host")
            return "runtime back"
        time.sleep(10)
    log("iris_runtime did NOT respawn within 150s — needs an iris_* tool call "
        "(body host respawns on demand) or start_iris_v2.bat")
    return ("⚠ iris_runtime did NOT come back on its own — make an iris_* tool "
            "call to make the body host respawn it, or run start_iris_v2.bat")


def dm_zeke(text: str) -> None:
    try:
        import requests
        env = Path(os.environ["USERPROFILE"]) / ".claude/channels/discord/.env"
        token = None
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DISCORD_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"')
        if not token:
            log("no discord token")
            return
        h = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
             "User-Agent": "IrisV14Resume/1.0"}
        ch = requests.post("https://discord.com/api/v10/users/@me/channels",
                           headers=h, json={"recipient_id": ZEKE_USER_ID},
                           timeout=15).json()["id"]
        requests.post(f"https://discord.com/api/v10/channels/{ch}/messages",
                      headers=h, json={"content": text}, timeout=15)
        log("DM'd Zeke")
    except Exception as e:
        log(f"DM failed: {e!r}")


def main() -> int:
    log("==== v14 RESUME guardian START ====")
    log(f"resuming from last checkpoint at step {last_step()}")
    touch_holdoff()
    kill_finetune()          # the stalled one
    kill_iris_runtime()      # free perception VRAM

    t0 = time.time()
    while time.time() - t0 < 120:
        touch_holdoff()
        used = gpu_used_mb()
        log(f"post-kill GPU used: {used} MB")
        if used is not None and used < FREE_VRAM_TARGET_MB:
            break
        time.sleep(5)

    bake_start = time.time()
    subprocess.Popen(["cmd.exe", "/c", str(BAKE_BAT)], cwd=str(REPO),
                     creationflags=_DETACHED, close_fds=True)
    log("bake_v14_resume.bat launched (runtime down, full VRAM)")

    result = "timeout"
    while time.time() - bake_start < MAX_BAKE_S:
        touch_holdoff()
        st = bake_state(bake_start)
        if st in ("done", "failed"):
            result = st
            break
        time.sleep(POLL_S)
    log(f"resume bake result: {result} (after {(time.time()-bake_start)/60:.1f} min, "
        f"step {last_step()})")

    # FLAW-1 FIX: never restore while the trainer is still alive.
    if result == "timeout":
        log("timeout -> killing trainer BEFORE restore (never race the bake)")
        kill_finetune()
        time.sleep(5)

    set_power(170)
    restore_eyes_tune()
    body_note = restore_body()
    try:
        HOLDOFF.unlink()
        log("holdoff flag deleted")
    except Exception:
        pass

    saved = ADAPTER.exists() and ADAPTER.stat().st_mtime > bake_start
    if saved:
        when = time.strftime("%H:%M", time.localtime(ADAPTER.stat().st_mtime))
        msg = (f"\U0001f7e2 **v14 bake FINISHED** (resumed from step 825) — adapter "
               f"saved {when}. Runtime restored without restarting cognition. "
               f"Packaging to GGUF next, then the battery vs the v12 baseline. "
               f"No production flip without your go.")
    else:
        msg = (f"\U0001f534 **v14 resume did NOT save** — result={result}, last step "
               f"{last_step()}. Runtime is restored and v12 is still production, so "
               f"nothing is broken; I'm digging into the log "
               f"(state/little_brain/train_v14_resume.log).")
    if body_note != "runtime back":
        msg += f"\n{body_note}"
    log(msg)
    dm_zeke(msg)
    log("==== v14 RESUME guardian DONE ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
