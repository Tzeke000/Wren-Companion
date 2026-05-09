# SELF_ASSESSMENT: I open, close, and list applications on the user's Windows PC.
"""
App launcher — tier 1 tools for opening/closing apps and listing windows.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool


# ── Known app paths ───────────────────────────────────────────────────────────

def _find_in_paths(*candidates: str) -> str | None:
    for c in candidates:
        if Path(c).is_file():
            return c
    return None


_APP_MAP: dict[str, list[str]] = {
    "notepad":     ["notepad.exe"],
    "calculator":  ["calc.exe"],
    "explorer":    ["explorer.exe"],
    "paint":       ["mspaint.exe"],
    "cmd":         ["cmd.exe"],
    "powershell":  ["powershell.exe"],
    "wordpad":     ["wordpad.exe"],
    "snipping":    ["SnippingTool.exe"],
    "taskmgr":     ["taskmgr.exe"],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "vscode": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        r"C:\Program Files\Microsoft VS Code\Code.exe",
    ],
    "spotify": [
        os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe"),
    ],
    "discord": [
        os.path.expandvars(r"%LOCALAPPDATA%\Discord\Update.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Discord\app-{}\Discord.exe"),  # version placeholder
    ],
    "steam": [
        r"C:\Program Files (x86)\Steam\steam.exe",
        r"C:\Program Files\Steam\steam.exe",
    ],
    "slack": [
        os.path.expandvars(r"%LOCALAPPDATA%\slack\slack.exe"),
    ],
    "obsidian": [
        os.path.expandvars(r"%LOCALAPPDATA%\Obsidian\Obsidian.exe"),
    ],
    "obs": [
        r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
        r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
    ],
    "cursor": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\cursor\Cursor.exe"),
    ],
}

_ALIASES: dict[str, str] = {
    "browser": "chrome",
    "google chrome": "chrome",
    "internet": "chrome",
    "internet explorer": "edge",
    "ie": "edge",
    "vs code": "vscode",
    "visual studio code": "vscode",
    "file explorer": "explorer",
    "files": "explorer",
    "folder": "explorer",
    "calc": "calculator",
    "snip": "snipping",
    "task manager": "taskmgr",
    # Notes is macOS-native; on Windows the closest equivalent is Notepad.
    # Without this alias, "Open Notes and type X" hangs the open cascade
    # because Windows doesn't ship a "Notes" app — Phase B Session B
    # turn 3 caught this 2026-05-05.
    "notes": "notepad",
    "note": "notepad",
    "obs studio": "obs",
    "open broadcaster software": "obs",
    "open broadcaster": "obs",
}


def _resolve_app(name: str) -> tuple[str | None, str]:
    """Return (exe_path_or_None, canonical_name). Falls back to None for shell=True launch."""
    key = name.lower().strip()
    key = _ALIASES.get(key, key)
    candidates = _APP_MAP.get(key)
    if candidates:
        # Try each candidate path
        for c in candidates:
            if "{}" in c:
                continue  # skip version-placeholder paths
            if Path(c).is_file():
                return c, key
            if not os.sep in c:  # simple executable name → let OS find it
                return c, key
        # No candidate found as file; first candidate might be a bare exe name
        if candidates and os.sep not in candidates[0]:
            return candidates[0], key
    return None, key


def _learned_apps_path(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".") / "state" / "learned_apps.json"


def _record_learned_app(name: str, exe_path: str, g: dict[str, Any]) -> None:
    """Persist a phrase → exe_path mapping so future calls hit known list directly."""
    p = _learned_apps_path(g)
    try:
        import json
        existing: dict[str, str] = {}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    existing = {str(k): str(v) for k, v in data.items()}
            except Exception:
                pass
        existing[name.lower().strip()] = exe_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[app_launcher] learned-apps save error: {e}")


def _check_learned(name: str, g: dict[str, Any]) -> str | None:
    p = _learned_apps_path(g)
    if not p.is_file():
        return None
    try:
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            v = data.get(name.lower().strip())
            if v:
                return str(v)
    except Exception:
        pass
    return None


def _filesystem_glob_search(name: str) -> str | None:
    """Last-resort: glob desktop + Program Files for a substring match."""
    needle = name.lower().strip()
    if not needle:
        return None
    home = Path(os.path.expanduser("~"))
    roots = [
        home / "Desktop",
        home / "OneDrive" / "Desktop",  # OneDrive-redirected Desktop holds .lnk shortcuts to many Steam apps and ML tools (CLAUDE.md rule 11)
        Path(r"C:\Users\Public\Desktop"),
        Path(r"C:\Program Files"),
        Path(r"C:\Program Files (x86)"),
        home / "AppData" / "Local",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            # Limit depth — bail after enough hits.
            for p in root.rglob("*.exe"):
                try:
                    if needle in p.stem.lower() and p.is_file():
                        candidates.append(p)
                        if len(candidates) >= 8:
                            break
                except OSError:
                    continue
            if len(candidates) >= 8:
                break
        except Exception:
            continue
    if not candidates:
        return None
    # Prefer shortest path (top-level installs).
    candidates.sort(key=lambda p: len(str(p)))
    return str(candidates[0])


def _tool_open_app(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    app_name = str(params.get("app_name") or "").strip()
    if not app_name:
        return {"ok": False, "error": "app_name required"}

    args = params.get("args") or []
    if isinstance(args, str):
        args = args.split()

    # Step 1: hardcoded known list.
    exe, canonical = _resolve_app(app_name)
    if exe:
        try:
            cmd = [exe] + list(args)
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE if exe == "cmd.exe" else 0)
            return {"ok": True, "launched": canonical, "exe": exe, "source": "known"}
        except Exception:
            pass  # Fall through to shell start at the end.

    # Step 2: previously learned mapping.
    learned = _check_learned(app_name, g)
    if learned and Path(learned).is_file():
        try:
            subprocess.Popen([learned] + list(args))
            return {"ok": True, "launched": app_name, "exe": learned, "source": "learned"}
        except Exception:
            pass

    # Step 3: discoverer fuzzy match.
    disc = g.get("_app_discoverer")
    if disc is not None:
        try:
            entry = disc.fuzzy_match(app_name)
        except Exception:
            entry = None
        if entry:
            path = str(entry.get("exe_path") or "")
            try:
                if path.startswith("steam://") or path.startswith("epic://"):
                    os.startfile(path)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen([path] + list(args))
                disc.record_launch(path)
                _record_learned_app(app_name, path, g)
                return {
                    "ok": True,
                    "launched": entry.get("name") or app_name,
                    "exe": path,
                    "source": "discoverer",
                }
            except Exception as e:
                print(f"[app_launcher] discoverer launch error: {e}")

    # Step 4: filesystem glob fallback.
    found = _filesystem_glob_search(app_name)
    if found:
        try:
            subprocess.Popen([found] + list(args))
            _record_learned_app(app_name, found, g)
            return {"ok": True, "launched": app_name, "exe": found, "source": "glob_search"}
        except Exception:
            pass

    # Step 5: helpful error with suggestions BEFORE the shell-start
    # wildcard. The shell start tries to launch whatever string the user
    # gave (which may be malformed and pop up a Windows search dialog),
    # so prefer giving the user a clear "I don't know that app" with
    # suggestions when we have a usable app catalog.
    if disc is not None:
        try:
            suggestions = disc.top_matches(app_name, limit=5) or []
        except Exception:
            suggestions = []
        if suggestions:
            names = [str(s.get("name") or s.get("exe_path") or "") for s in suggestions]
            names = [n for n in names if n][:5]
            if names:
                return {
                    "ok": False,
                    "error": (
                        f"I don't know an app called {app_name!r}. "
                        f"Apps I know that might match: {', '.join(names)}."
                    ),
                    "suggestions": names,
                    "source": "no_match_with_suggestions",
                }

    # Step 6: shell start as the very last resort. Reached only when the
    # discoverer either isn't loaded or has zero candidates — shell start
    # might still find a Windows-registered app via PATH.
    try:
        subprocess.Popen(f'start "" "{app_name}"', shell=True)
        return {"ok": True, "launched": app_name, "method": "shell_start", "source": "shell"}
    except Exception as e:
        return {"ok": False, "error": f"Could not launch {app_name!r}: {e}"}


def _tool_close_app(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    app_name = str(params.get("app_name") or "").strip()
    if not app_name:
        return {"ok": False, "error": "app_name required"}
    try:
        import psutil
    except ImportError:
        # Fallback: taskkill
        try:
            result = subprocess.run(
                ["taskkill", "/IM", app_name if "." in app_name else f"{app_name}.exe", "/F"],
                capture_output=True, text=True, timeout=10,
            )
            return {"ok": result.returncode == 0, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    key = app_name.lower().strip()
    key = _ALIASES.get(key, key)
    # Try to find the exe name
    exe_name = None
    candidates = _APP_MAP.get(key)
    if candidates:
        bare = [c for c in candidates if os.sep not in c]
        if bare:
            exe_name = bare[0]
        elif candidates:
            exe_name = Path(candidates[0]).name
    if not exe_name:
        exe_name = app_name if "." in app_name else f"{app_name}.exe"

    killed = 0
    pids_to_kill = []
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == exe_name.lower():
                pids_to_kill.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # Try graceful terminate first
    for proc in pids_to_kill:
        try:
            proc.terminate()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # Wait briefly for processes to exit
    if pids_to_kill:
        psutil.wait_procs(pids_to_kill, timeout=2.0)
    # If anything is still alive (Chrome's confirmation dialogs, multi-process
    # apps that ignore SIGTERM), force-kill with /F via taskkill which Windows
    # honors more aggressively than psutil's terminate.
    still_alive = [p for p in pids_to_kill if p.is_running()]
    if still_alive:
        try:
            subprocess.run(
                ["taskkill", "/IM", exe_name, "/F"],
                capture_output=True, text=True, timeout=8,
            )
        except Exception:
            pass
        # Re-check after taskkill
        time.sleep(1.0)
        still_alive = [p for p in pids_to_kill if p.is_running()]
    # `ok` reflects ACTUAL termination, not just attempted termination. Per
    # Zeke's framing 2026-05-04: "what's the point of telling her to do
    # something if she doesn't actually do it" — if no processes were killed
    # OR processes are still alive after the cascade, that's a failure even
    # if we tried. Vault: decisions/voice-text-pipeline-equivalence.md.
    actually_closed = (killed > 0 and len(still_alive) == 0)
    return {
        "ok": actually_closed,
        "terminated": killed,
        "still_alive": len(still_alive),
        "target": exe_name,
    }


def _tool_get_open_apps(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Return list of visible top-level window titles using Windows user32."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        titles: list[str] = []

        def enum_callback(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    t = buf.value.strip()
                    if t and t not in titles:
                        titles.append(t)
            return True

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
        user32.EnumWindows(EnumWindowsProc(enum_callback), 0)
        return {"ok": True, "windows": titles[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


register_tool(
    "open_app",
    (
        "Open an application on the user's PC. Known apps: notepad, calculator, explorer, paint, "
        "cmd, chrome, firefox, edge, vscode, spotify, discord, steam, slack, obsidian. "
        "Pass app_name as a string, e.g. 'notepad', 'chrome', 'vscode'. "
        "Optional args list for command-line arguments."
    ),
    1,
    _tool_open_app,
)

register_tool(
    "close_app",
    "Close a running application by name. E.g. close_app('notepad') or close_app('chrome').",
    1,
    _tool_close_app,
)

register_tool(
    "get_open_apps",
    "List all currently visible application windows on the desktop.",
    1,
    _tool_get_open_apps,
)
