"""Power-event ledger — when did the tower go down uncleanly, and for how long?

Reads the Windows System log (no admin needed):
  * Kernel-Power 41  — "rebooted without cleanly shutting down" (boot time + BugcheckCode,
                        PowerButtonTimestamp, LongPowerButtonPressDetected)
  * EventLog 6008    — "previous system shutdown at <time> on <date> was unexpected"
                        (<time> = Windows' last-alive heartbeat before the loss)
  * User32 1074      — planned shutdown/restart (who/why), for context

Pairs each 41 with the nearest following 6008 and classifies the event:
  bugcheck  — BugcheckCode != 0                     (a crash / BSOD, not power)
  button    — PowerButtonTimestamp != 0 or long press (someone held the button)
  power_or_hang — neither                          (power loss OR a hard hang + reset;
                                                     Windows alone cannot tell these apart)
gap_min = boot time − last-alive time. A long gap means the box sat dark (power out, or
off until someone pressed power); a short gap is consistent with a quick blip or a reset.

Writes state/power_events.json (full ledger) and prints a summary. Read-only; safe to re-run.
Usage:  .venv\\Scripts\\python.exe scripts\\power_event_ledger.py [days=180]

Zeke 2026-10-02: base power blips "about once a week now" — this is the measurement.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "state" / "power_events.json"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW — never flash a console over his game

PS = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$since=(Get-Date).AddDays(-%DAYS%)
$ev=Get-WinEvent -FilterHashtable @{LogName='System'; Id=41,6008,1074; StartTime=$since} -ErrorAction SilentlyContinue
$ev | ForEach-Object {
  $d=@{}
  if ($_.Id -eq 41) { ([xml]$_.ToXml()).Event.EventData.Data | ForEach-Object { $d[$_.Name]=$_.'#text' } }
  [pscustomobject]@{ id=$_.Id; ts=$_.TimeCreated.ToString('s'); msg=$_.Message; data=$d }
} | ConvertTo-Json -Depth 4 -Compress
"""

_6008 = re.compile(r"shutdown at (?P<t>[\d:]+)\W*(?P<ap>[AP]M) on (?P<d>\S+) was unexpected", re.I)


def _read(days: int) -> list[dict]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS.replace("%DAYS%", str(days))],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW, timeout=120,
    ).stdout.strip()
    if not out:
        return []
    rows = json.loads(out)
    return rows if isinstance(rows, list) else [rows]


def _last_alive(msg: str) -> datetime | None:
    # Windows wraps date parts in LRM marks (U+200E); a codepage mismatch turns them into '?'.
    m = _6008.search(msg)
    if not m:
        return None
    date = re.sub(r"[^\d/]", "", m["d"])
    try:
        return datetime.strptime(f"{date} {m['t']} {m['ap'].upper()}", "%m/%d/%Y %I:%M:%S %p")
    except ValueError:
        return None


def build(days: int = 180) -> dict:
    rows = sorted(_read(days), key=lambda r: r["ts"])
    events, planned = [], []
    for i, r in enumerate(rows):
        if r["id"] == 1074:
            planned.append({"ts": r["ts"], "what": r["msg"].splitlines()[0][:160]})
            continue
        if r["id"] != 41:
            continue
        boot = datetime.fromisoformat(r["ts"])
        d = r.get("data") or {}
        alive = None
        for nxt in rows[i + 1:i + 6]:  # the 6008 lands seconds after its 41
            if nxt["id"] == 6008 and datetime.fromisoformat(nxt["ts"]) - boot < timedelta(minutes=5):
                alive = _last_alive(nxt["msg"])
                break
        bug = str(d.get("BugcheckCode", "0"))
        button = str(d.get("PowerButtonTimestamp", "0")) not in ("0", "") or \
            str(d.get("LongPowerButtonPressDetected", "false")).lower() == "true"
        kind = "bugcheck" if bug not in ("0", "") else ("button" if button else "power_or_hang")
        gap = round((boot - alive).total_seconds() / 60, 1) if alive else None
        events.append({
            "boot": boot.isoformat(timespec="seconds"),
            "last_alive": alive.isoformat(timespec="seconds") if alive else None,
            "gap_min": gap, "kind": kind, "bugcheck": bug,
        })
    # per-week counts of unclean events (ISO week)
    weeks: dict[str, int] = {}
    for e in events:
        y, w, _ = datetime.fromisoformat(e["boot"]).isocalendar()
        weeks[f"{y}-W{w:02d}"] = weeks.get(f"{y}-W{w:02d}", 0) + 1
    ledger = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "window_days": days,
        "unclean_count": len(events),
        "by_kind": {k: sum(1 for e in events if e["kind"] == k) for k in ("power_or_hang", "bugcheck", "button")},
        "per_iso_week": weeks,
        "events": events,
        "planned_shutdowns": planned,
        "note": "power_or_hang = no bugcheck, no power button: power loss OR hard hang+reset; "
                "Windows alone cannot separate them. Long gap_min = box sat dark.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    return ledger


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    L = build(days)
    print(f"unclean boots in {days} d: {L['unclean_count']}  by kind: {L['by_kind']}")
    for e in L["events"]:
        print(f"  {e['boot']}  last-alive {e['last_alive']}  gap {e['gap_min']} min  [{e['kind']}]")
    print("per ISO week:", L["per_iso_week"])
    print(f"planned shutdowns/restarts (1074): {len(L['planned_shutdowns'])}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
