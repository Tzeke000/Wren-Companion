"""scripts/fire_reminder.py <id> — fired by a Windows scheduled task (Iris-Reminder-<id>) at the reminder's
time. Survives my restarts and his reboots (the task has StartWhenAvailable, so a reminder that came due
while the PC was off fires at the next logon). Zeke 2026-10-07: reminders, "#3".

It DMs Zeke on Discord (reaches his phone anywhere), drops a note into my chat bridge so cognition
knows it fired (I may also say it aloud if he's in the room), marks the reminder fired, and deletes
its own scheduled task. Runs under pythonw — no console window over his game.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "state" / "reminders.json"
LOG = ROOT / "state" / "reminders_log.jsonl"
sys.path.append(str(Path(__file__).resolve().parent))
sys.path.append(str(ROOT))


def log(**kw):
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 1), **kw}) + "\n")
    except Exception:
        pass


def main() -> int:
    rid = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        store = json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        store = {}
    r = store.get(rid)
    if not r or r.get("status") != "pending":
        log(event="skip", id=rid, why="missing or not pending")
        return 0
    late_min = max(0, round((time.time() - float(r["due_ts"])) / 60))
    text = f"⏰ Reminder: {r['text']}" + (f"  (was due {late_min} min ago — the PC was off/asleep)" if late_min >= 5 else "")
    sent = False
    for attempt in range(4):                       # network blips after a wake-from-sleep: retry ~2.5 min
        try:
            import discord_dm_user as dm
            from _private import priv
            import requests
            token = dm.load_token()
            uid = priv("zeke_discord_user_id")
            h = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
            ch = requests.post("https://discord.com/api/v10/users/@me/channels", headers=h,
                               json={"recipient_id": uid}, timeout=15).json()["id"]
            sent = requests.post(f"https://discord.com/api/v10/channels/{ch}/messages", headers=h,
                                 json={"content": text}, timeout=15).status_code in (200, 201)
        except Exception as e:  # noqa: BLE001
            log(event="dm_error", id=rid, attempt=attempt, error=repr(e)[:200])
        if sent:
            break
        time.sleep(15 * (attempt + 1))
    try:
        from brain import iris_chat
        iris_chat.configure(ROOT) if hasattr(iris_chat, "configure") else None
        iris_chat.submit(f"[REMINDER FIRED — scheduled task, not Zeke typing] \"{r['text']}\" — Discord DM "
                         f"{'sent' if sent else 'FAILED'}. If a live frame shows him in the room you may also say it "
                         f"aloud. Reply with chat_reply (one short line ok — it's a log).")
    except Exception as e:  # noqa: BLE001
        log(event="bridge_error", id=rid, error=repr(e)[:200])
    from tools.system.reminder_task_tool import update_row     # same cross-process lock + atomic write
    update_row(rid, status="fired" if sent else "fire_failed", fired_ts=time.time(), dm_sent=sent)
    subprocess.run(["schtasks", "/delete", "/tn", f"Iris-Reminder-{rid}", "/f"], capture_output=True,
                   creationflags=0x08000000)
    log(event="fired", id=rid, sent=sent, late_min=late_min)
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
