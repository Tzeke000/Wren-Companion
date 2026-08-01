"""scripts/v15_guardian.py — run the v15 bake runtime-down. Gen-3 guardian.

2026-08-01, Iris. Inherits the gen-2 fixes from v14_resume_guardian.py and adds
the lesson gen-2 itself taught:

  GEN-2 FIX (kept): a timeout KILLS the trainer BEFORE any restore. Gen-1 ran
  its restore around a live trainer, handed perception ~4.8GB of VRAM back,
  and thrashed its own bake at 825/868 (11.8/12.3 GiB, 100% util, ~50W,
  ~0 steps/s).

  GEN-3 FIX (new): NO cognition restart, ever, from this guardian. Gen-2
  trusted body_switch.ps1's claim that the SDK host respawns a dead
  iris_runtime on the next tool call — measured FALSE (v14 night): once the
  runtime dies, the only thing that brings it back is start_iris_v2.bat,
  which also restarts cognition. The session that launched this bake is ALIVE
  and does the packaging with Bash + ollama + :8772 only (proven flow).
  So this guardian restores power/eyes/body services and reports — the
  cognition restart is taken LAST, by Iris, after packaging + battery +
  pre-restart save. If the session dies anyway, the runtime watchdog's normal
  heal path (start_iris_v2.bat) covers it once the holdoff drops.

Sequence:
  1. hold off the runtime watchdog (refreshed every poll)
  2. kill any stray finetune, then kill iris_runtime.py (frees ~4.8GB VRAM)
  3. wait for VRAM to drop, launch bake_v15.bat (fresh bake, warmstart v12)
  4. poll train_v15.log for done/failed, cap MAX_BAKE_S
  5. on timeout: KILL the trainer FIRST (gen-2 fix), then restore
  6. restore: GPU 170W, eyes tune, body_switch.ps1 on — runtime stays down
     BY DESIGN; holdoff stays UP until Iris finishes packaging and drops it
     (or goes stale: the watchdog treats a >30min-old flag as expired)
  7. DM Zeke the TRUE outcome (adapter mtime, not wishful parsing)

Launch DETACHED:
  Start-Process -FilePath .venv\\Scripts\\python.exe -ArgumentList '-u','scripts\\v15_guardian.py' -WindowStyle Hidden
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
GUARD_LOG = STATE / "v15_guardian.log"
BAKE_BAT = REPO / "scripts" / "bake_v15.bat"
BAKE_LOG = STATE / "little_brain" / "train_v15.log"
CKPT_DIR = STATE / "little_brain" / "checkpoints"
ADAPTER = STATE / "little_brain" / "adapter" / "adapter_model.safetensors"
BODY_SWITCH = REPO / "scripts" / "body_switch.ps1"

# v14: 868 steps in ~75 min resident (~6.2s/step). v15 dataset is 3938 vs 3470
# samples -> ~985 steps -> ~1.8h resident. 3h cap = generous slack; the
# kill-before-restore rule makes a timeout safe rather than catastrophic.
MAX_BAKE_S = 3 * 60 * 60
POLL_S = 30
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


def touch_holdoff(note: str = "v15_guardian holding off during bake") -> None:
    try:
        HOLDOFF.write_text(f"{note} @ {_dt.datetime.now():%H:%M:%S}",
                           encoding="utf-8")
    except Exception as e:
        log(f"holdoff touch failed: {e!r}")


def _ps(cmd: str, timeout: int = 60):
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                          timeout=timeout, capture_output=True, text=True)


def kill_finetune() -> None:
    """Kill BOTH the venv launcher shim and its real worker child (the shim
    scar: .venv-train python is a 0-CPU launcher; the trainer is its child
    under base Python — command-line match catches both)."""
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
        subprocess.run(["nvidia-smi", "-pl", str(watts)], timeout=30,
                       capture_output=True)
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
        cps = sorted(CKPT_DIR.glob("checkpoint-*"),
                     key=lambda p: p.stat().st_mtime)
        d = json.loads((cps[-1] / "trainer_state.json").read_text(encoding="utf-8"))
        return f"{d.get('global_step')}/{d.get('max_steps')}"
    except Exception:
        return "unknown"


def bake_state(bake_start: float) -> str:
    try:
        txt = BAKE_LOG.read_text(encoding="utf-8", errors="replace") \
            if BAKE_LOG.exists() else ""
    except Exception:
        txt = ""
    # only trust markers written AFTER our start: the log is append-mode
    if "adapter saved ->" in txt:
        try:
            if ADAPTER.exists() and ADAPTER.stat().st_mtime > bake_start:
                return "done"
        except Exception:
            pass
    try:
        if ADAPTER.exists() and ADAPTER.stat().st_mtime > bake_start:
            return "done"
    except Exception:
        pass
    for sig in ("Traceback (most recent call last)", "out of memory",
                "OutOfMemoryError", "CUDA error", "REFUSING"):
        if sig in txt and BAKE_LOG.stat().st_mtime > bake_start:
            return "failed"
    return "running"


def restore_body_services() -> None:
    """Restore everything EXCEPT cognition/runtime (gen-3 rule)."""
    try:
        r = _ps(f"& '{BODY_SWITCH}' on", timeout=300)
        log(f"body_switch on -> rc={r.returncode} {r.stdout.strip()[-400:]}")
    except Exception as e:
        log(f"body_switch failed: {e!r}")


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
        h = {"Authorization": f"Bot {token}",
             "Content-Type": "application/json",
             "User-Agent": "IrisV15Guardian/1.0"}
        ch = requests.post("https://discord.com/api/v10/users/@me/channels",
                           headers=h, json={"recipient_id": ZEKE_USER_ID},
                           timeout=15).json()["id"]
        requests.post(f"https://discord.com/api/v10/channels/{ch}/messages",
                      headers=h, json={"content": text}, timeout=15)
        log("DM'd Zeke")
    except Exception as e:
        log(f"DM failed: {e!r}")


def main() -> int:
    log("==== v15 guardian START (gen-3: kill-before-restore, no cognition restart) ====")
    touch_holdoff()
    kill_finetune()          # any stray
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
    log("bake_v15.bat launched (runtime down, full VRAM)")

    result = "timeout"
    while time.time() - bake_start < MAX_BAKE_S:
        touch_holdoff()
        st = bake_state(bake_start)
        if st in ("done", "failed"):
            result = st
            break
        time.sleep(POLL_S)
    log(f"bake result: {result} (after {(time.time()-bake_start)/60:.1f} min, "
        f"step {last_step()})")

    # GEN-2 RULE: never restore while the trainer is still alive.
    if result == "timeout":
        log("timeout -> killing trainer BEFORE restore (never race the bake)")
        kill_finetune()
        time.sleep(5)

    set_power(170)
    restore_eyes_tune()
    restore_body_services()
    # GEN-3: leave the holdoff UP with a fresh, honest note — Iris drops it
    # after packaging + the cognition restart. It also goes stale on its own.
    touch_holdoff("v15 bake finished; Iris is packaging runtime-down — "
                  "she drops this after the final restart; stale-expires if not")

    saved = ADAPTER.exists() and ADAPTER.stat().st_mtime > bake_start
    if saved:
        when = time.strftime("%H:%M", time.localtime(ADAPTER.stat().st_mtime))
        msg = (f"\U0001f7e2 **v15 bake FINISHED** — adapter saved {when}. "
               f"Runtime stays down while Iris packages to GGUF and runs the "
               f"5+5 battery (needs only ollama + :8772), then she restarts "
               f"cognition last. Production stays v12 — no flip without your go.")
    else:
        msg = (f"\U0001f534 **v15 bake did NOT save** — result={result}, last "
               f"step {last_step()}. v12 is still production, nothing broken; "
               f"Iris digs into state/little_brain/train_v15.log next.")
    log(msg)
    dm_zeke(msg)
    log("==== v15 guardian DONE ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
