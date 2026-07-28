"""Watch the v14 trainer PID and DM Zeke the moment the bake ends.

Session-independent safety net (2026-07-27, Iris): Zeke asked to be notified
before he goes to sleep, and my cognition session can die without warning
(two unclean session ends in two days). This runs detached, so the ping
happens whether or not I'm alive to send it.

Reports the TRUTH of the outcome, not a guess: it checks whether the adapter
file actually got rewritten after the bake started, so a crashed trainer
gets reported as a crash rather than as success.

Usage: python scripts/v14_done_notify.py <trainer_pid>
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\Wren-Companion")
ADAPTER = ROOT / "state/little_brain/adapter/adapter_model.safetensors"
CKPT_DIR = ROOT / "state/little_brain/checkpoints"
LOG = ROOT / "state/v14_done_notify.log"
ZEKE_USER_ID = "600008921008046120"
BAKE_START = 1785194515.0  # 2026-07-27 18:21:55 EDT, guardian's launch of bake_v14.bat


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def alive(pid: int) -> bool:
    """True while the Windows process is running (ctypes, no deps)."""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if k.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == 259  # STILL_ACTIVE
        return False
    finally:
        k.CloseHandle(h)


def last_step() -> str:
    try:
        cps = sorted(CKPT_DIR.glob("checkpoint-*"), key=lambda p: p.stat().st_mtime)
        d = json.loads((cps[-1] / "trainer_state.json").read_text(encoding="utf-8"))
        return f"{d.get('global_step')}/{d.get('max_steps')}"
    except Exception:
        return "unknown"


def dm_zeke(text: str) -> None:
    try:
        import requests
        env = Path(os.environ["USERPROFILE"]) / ".claude/channels/discord/.env"
        token = None
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DISCORD_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"')
        if not token:
            log("no token found")
            return
        h = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
             "User-Agent": "IrisV14Notify/1.0"}
        ch = requests.post("https://discord.com/api/v10/users/@me/channels",
                           headers=h, json={"recipient_id": ZEKE_USER_ID},
                           timeout=15).json()["id"]
        requests.post(f"https://discord.com/api/v10/channels/{ch}/messages",
                      headers=h, json={"content": text}, timeout=15)
        log("DM'd Zeke")
    except Exception as e:
        log(f"DM failed: {e!r}")


def main() -> None:
    pid = int(sys.argv[1])
    log(f"watching trainer pid {pid}")
    while alive(pid):
        time.sleep(20)
    log(f"trainer {pid} exited at step {last_step()}")
    time.sleep(10)  # let the final file write settle

    saved = ADAPTER.exists() and ADAPTER.stat().st_mtime > BAKE_START
    if saved:
        when = time.strftime("%H:%M", time.localtime(ADAPTER.stat().st_mtime))
        msg = (f"\U0001f7e2 **v14 bake DONE** — adapter saved {when} "
               f"(final step {last_step()}). Packaging to GGUF next, then the "
               f"battery vs the v12 baseline. No flip without your go. Sleep well.")
    else:
        msg = (f"\U0001f534 **v14 bake ENDED WITHOUT SAVING** — trainer exited at step "
               f"{last_step()} but the adapter file was never rewritten (still pre-bake). "
               f"I'm digging into why; nothing is broken in production, v12 is still live.")
    log(msg)
    dm_zeke(msg)


if __name__ == "__main__":
    main()
