"""reminder — reminders that survive my restarts and his reboots (Zeke 2026-10-07, "#3").

Each reminder is a row in state/reminders.json plus a ONE-SHOT Windows scheduled task
"Iris-Reminder-<id>" (StartWhenAvailable: if the PC was off at the due time, it fires at the next
logon and says how late it is). The task runs scripts/fire_reminder.py under pythonw (no console
flash), which DMs Zeke on Discord, tells cognition through the chat bridge, marks the row fired and
deletes its own task. A session cron would die with every restart — this does not.

params:
  action=add  text='take the trash out'  when='in 20m' | 'in 2h' | 'in 1d' | 'at 17:30' | 'tomorrow 9:00'
              | '2026-10-08 07:15'  (local time; 'at HH:MM' already past today => tomorrow)
  action=list                      (pending first; add all=true for fired/cancelled too)
  action=cancel id=<id>
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "state" / "reminders.json"
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
FIRE = ROOT / "scripts" / "fire_reminder.py"
NOWIN = 0x08000000


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(STORE)


def parse_when(s: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    s = s.strip().lower()
    m = re.fullmatch(r"in\s+(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)", s)
    if m:
        n, u = float(m.group(1)), m.group(2)[0]
        return now + timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[u]: n})
    m = re.fullmatch(r"(?:at\s+)?(tomorrow\s+)?(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?", s)
    if m:
        h, mi = int(m.group(2)), int(m.group(3))
        if m.group(4) == "pm" and h < 12:
            h += 12
        if m.group(4) == "am" and h == 12:
            h = 0
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if m.group(1):
            t += timedelta(days=1)
        elif t <= now:
            t += timedelta(days=1)
        return t
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%m/%d %H:%M"):
        try:
            t = datetime.strptime(s, fmt)
            if fmt == "%m/%d %H:%M":
                t = t.replace(year=now.year)
            return t
        except ValueError:
            pass
    raise ValueError(f"can't read when='{s}' (try 'in 20m', 'at 17:30', 'tomorrow 9:00', '2026-10-08 07:15')")


def _ps(cmd: str) -> tuple[int, str]:
    cp = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                        text=True, timeout=40, creationflags=NOWIN)
    return cp.returncode, (cp.stdout + cp.stderr).strip()


def _tool_reminder(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    a = str(params.get("action") or "list").lower()
    store = _load()
    if a == "add":
        text = str(params.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text required"}
        try:
            due = parse_when(str(params.get("when") or ""))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if due <= datetime.now() + timedelta(seconds=20):
            return {"ok": False, "error": "that time is in the past (or under 20 s away)"}
        rid = uuid.uuid4().hex[:8]
        name = f"Iris-Reminder-{rid}"
        at = due.strftime("%Y-%m-%dT%H:%M:%S")
        cmd = (f"$a = New-ScheduledTaskAction -Execute '{PYW}' -Argument '\"{FIRE}\" {rid}' -WorkingDirectory '{ROOT}';"
               f"$t = New-ScheduledTaskTrigger -Once -At ([datetime]'{at}');"
               f"$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
               f"-ExecutionTimeLimit (New-TimeSpan -Minutes 5);"
               f"Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s "
               f"-Description 'Iris reminder: one-shot, deletes itself after firing' -Force | Out-Null;"
               f"(Get-ScheduledTask -TaskName '{name}').State")
        rc, out = _ps(cmd)
        if rc != 0 or "Ready" not in out:
            return {"ok": False, "error": "couldn't register the scheduled task", "detail": out[-300:]}
        store[rid] = {"id": rid, "text": text, "due_iso": at, "due_ts": due.timestamp(),
                      "created_ts": time.time(), "status": "pending", "task": name}
        _save(store)
        return {"ok": True, "id": rid, "due": at, "text": text, "task": name, "task_state": out.strip()}
    if a == "list":
        rows = sorted(store.values(), key=lambda r: (r.get("status") != "pending", r.get("due_ts", 0)))
        if not params.get("all"):
            rows = [r for r in rows if r.get("status") == "pending"]
        return {"ok": True, "reminders": [{k: r.get(k) for k in ("id", "text", "due_iso", "status")} for r in rows]}
    if a == "cancel":
        rid = str(params.get("id") or "")
        r = store.get(rid)
        if not r:
            return {"ok": False, "error": f"no reminder {rid}"}
        subprocess.run(["schtasks", "/delete", "/tn", f"Iris-Reminder-{rid}", "/f"], capture_output=True,
                       creationflags=NOWIN)
        r["status"] = "cancelled"
        _save(store)
        return {"ok": True, "cancelled": rid, "text": r["text"]}
    return {"ok": False, "error": "action must be add|list|cancel"}


register_tool(
    "reminder",
    "Reminders that survive restarts and reboots: action=add text='...' when='in 20m'|'at 17:30'|"
    "'tomorrow 9:00'|'2026-10-08 07:15' · action=list [all=true] · action=cancel id=... Each is a one-shot "
    "Windows scheduled task that DMs Zeke on Discord at the time (late-but-delivered if the PC was off) "
    "and tells me through the chat bridge.",
    2,
    _tool_reminder,
)
