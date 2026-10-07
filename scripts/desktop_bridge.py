"""scripts/desktop_bridge.py — let an SSH session reach Zeke's REAL desktop (2026-10-07).

Zeke: "for the server you should have access to my PC and be able to do things" — "can you do it like
how you reach the server now? maybe make that better?" SSH already gives files/commands/processes, but
Windows runs SSH sessions apart from the logged-in desktop, so they can't see the screen, click, or
show a window. This bridge closes that gap WITHOUT a resident agent:

  over SSH:  python desktop_bridge.py submit <action> '<json args>'
             -> writes a request into state/desktop_bridge/, runs the on-demand scheduled task
                "Iris-Desktop-Bridge" (Interactive-only = runs INSIDE his desktop session), waits for
                the result and prints it as JSON.
  the task:  pythonw desktop_bridge.py serve   (processes every pending request, then exits)

Actions: screenshot [max_px] (-> JPEG path; scp it back) · pc {..pc tool params..} (volume, media,
windows, launch — the same code as the `pc` tool) · open target · click x y [button] · type text ·
keys combo ('ctrl+shift+esc') · ping.
Install the task once (from his desktop, or over SSH):  python desktop_bridge.py install
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
Q = ROOT / "state" / "desktop_bridge"
OUT = Q / "out"
TASK = "Iris-Desktop-Bridge"
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
NOWIN = 0x08000000


def _do(action: str, args: dict) -> dict:
    import ctypes
    u32 = ctypes.windll.user32
    if action == "ping":
        return {"ok": True, "session": os.environ.get("SESSIONNAME"), "user": os.environ.get("USERNAME")}
    if action == "screenshot":
        from PIL import ImageGrab
        im = ImageGrab.grab(all_screens=bool(args.get("all_screens"))).convert("RGB")
        im.thumbnail((int(args.get("max_px") or 1280),) * 2)
        OUT.mkdir(parents=True, exist_ok=True)
        p = OUT / f"screen_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        im.save(p, "JPEG", quality=70)
        return {"ok": True, "path": str(p), "size": list(im.size)}
    if action == "pc":
        sys.path.insert(0, str(ROOT))
        from tools.system.pc_control_tool import _tool_pc
        return _tool_pc(args, {})
    if action == "open":
        os.startfile(str(args["target"]))
        return {"ok": True, "opened": args["target"]}
    if action == "click":
        x, y = int(args["x"]), int(args["y"])
        u32.SetCursorPos(x, y)
        down, up = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}[str(args.get("button") or "left")]
        u32.mouse_event(down, 0, 0, 0, 0)
        u32.mouse_event(up, 0, 0, 0, 0)
        return {"ok": True, "clicked": [x, y]}
    if action in ("type", "keys"):
        sys.path.insert(0, str(ROOT))
        import pyautogui  # noqa: PLC0415 — only in the desktop session
        if action == "type":
            pyautogui.write(str(args["text"]), interval=0.01)
        else:
            pyautogui.hotkey(*[k.strip() for k in str(args["combo"]).split("+")])
        return {"ok": True}
    return {"ok": False, "error": f"unknown action {action}"}


def serve() -> int:
    Q.mkdir(parents=True, exist_ok=True)
    for req in sorted(Q.glob("req_*.json")):
        rid = req.stem[4:]
        try:
            r = json.loads(req.read_text(encoding="utf-8"))
            if time.time() - float(r.get("ts", 0)) > 120:
                res = {"ok": False, "error": "request expired before the desktop session picked it up"}
            else:
                res = _do(r["action"], r.get("args") or {})
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": repr(e)[:300]}
        (Q / f"res_{rid}.json").write_text(json.dumps(res), encoding="utf-8")
        req.unlink(missing_ok=True)
    return 0


def submit(action: str, args: dict, timeout: float = 30.0) -> dict:
    Q.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex[:10]
    (Q / f"req_{rid}.json").write_text(json.dumps({"ts": time.time(), "action": action, "args": args}),
                                       encoding="utf-8")
    cp = subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True, text=True, creationflags=NOWIN)
    if cp.returncode != 0:
        return {"ok": False, "error": f"couldn't start {TASK} (installed? is he logged in?)",
                "detail": (cp.stdout + cp.stderr).strip()[-200:]}
    res = Q / f"res_{rid}.json"
    end = time.time() + timeout
    while time.time() < end:
        if res.is_file():
            out = json.loads(res.read_text(encoding="utf-8"))
            res.unlink(missing_ok=True)
            return out
        time.sleep(0.25)
    return {"ok": False, "error": "no answer from the desktop session (nobody logged in, or the task is busy)"}


def install() -> int:
    cmd = (f"$a = New-ScheduledTaskAction -Execute '{PYW}' -Argument '\"{Path(__file__).resolve()}\" serve' "
           f"-WorkingDirectory '{ROOT}';"
           f"$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited;"
           f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
           f"-MultipleInstances Queue -ExecutionTimeLimit (New-TimeSpan -Minutes 2);"
           f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Principal $p -Settings $s "
           f"-Description 'Iris desktop bridge: on-demand, runs SSH requests inside the logged-in desktop' "
           f"-Force | Out-Null; (Get-ScheduledTask -TaskName '{TASK}').State")
    cp = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                        text=True, creationflags=NOWIN)
    print((cp.stdout + cp.stderr).strip())
    return cp.returncode


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "serve":
        sys.exit(serve())
    if mode == "install":
        sys.exit(install())
    if mode == "submit" and len(sys.argv) >= 3:
        a = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(submit(sys.argv[2], a)))
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
