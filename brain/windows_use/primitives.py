"""brain/windows_use/primitives.py — Tier 1 mechanical layer.

Pure operations on top of pywinauto + uiautomation. No event emission,
no temporal_sense calls, no deny-list checks. Just: open this app,
click that control, type this text.

Each primitive returns a simple result (bool / str / None). Per-call
budget <500ms or returns False. The orchestrator (agent.py) composes
these into multi-strategy operations with retry + narration.

Heavy library imports are lazy so module import is cheap.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
from pathlib import Path
from typing import Any

# Win32 SendMessageTimeoutW for responsiveness probes.
_SMTO_ABORTIFHUNG = 0x0002
_WM_NULL = 0x0000


# ── Lazy library handles ─────────────────────────────────────────────


def _ui_auto():
    """Return the uiautomation module, lazy-loaded."""
    import uiautomation as auto
    return auto


def _pywin():
    """Return the pywinauto module, lazy-loaded with backend=uia default."""
    from pywinauto import Application, Desktop, keyboard
    return Application, Desktop, keyboard


# ── Open-app primitives ──────────────────────────────────────────────


def open_via_powershell(name: str, args: list[str] | None = None) -> bool:
    """Strategy 1: PowerShell `Start-Process`. Returns True if subprocess
    didn't immediately fail. Does NOT verify the app actually started —
    that's the orchestrator's job (via slow_app_detector).
    """
    if not name:
        return False
    parts = ["powershell.exe", "-NoProfile", "-Command",
             f"Start-Process -FilePath '{name.replace(chr(39), chr(39)*2)}'"]
    if args:
        argstr = ", ".join(f"'{a.replace(chr(39), chr(39) * 2)}'" for a in args)
        parts[-1] += f" -ArgumentList @({argstr})"
    try:
        result = subprocess.run(
            parts, capture_output=True, text=True, timeout=4.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        return False


def open_via_search(name: str) -> bool:
    """Strategy 2: send Win key, type the name, wait for indexing,
    press Enter. Visually noisy but works for any indexed app.
    """
    if not name:
        return False
    auto = _ui_auto()
    try:
        # Open Start menu via Win key.
        auto.SendKeys("{Win}")
        time.sleep(0.4)
        # Type name. SendKeys treats some chars as modifiers; keep it simple
        # and use auto.SendKeys with curly-brace escaping if needed.
        # Safer to use SetClipboard+paste, but Start menu doesn't accept
        # paste reliably — type directly.
        for ch in name:
            if ch in "{}()+^%~":
                auto.SendKeys("{" + ch + "}")
            else:
                auto.SendKeys(ch)
        time.sleep(0.8)  # Indexing/UI catch-up window.
        auto.SendKeys("{Enter}")
        return True
    except Exception:
        # Best-effort recovery: close any partially-open Start menu.
        try:
            auto.SendKeys("{Esc}")
        except Exception:
            pass
        return False


def open_via_direct_path(exe_path: str, args: list[str] | None = None) -> bool:
    """Strategy 3: pywinauto Application.start(). Used when we have a
    canonical path (from app_discoverer or APP_MAP).
    """
    if not exe_path:
        return False
    try:
        Application, _, _ = _pywin()
        cmd = exe_path
        if args:
            cmd = f'"{exe_path}" ' + " ".join(f'"{a}"' for a in args)
        Application(backend="uia").start(cmd, wait_for_idle=False)
        return True
    except Exception as e:
        # Common cause: app already running and pywinauto can't double-launch.
        # That's still a "success" from our POV — the app is up.
        msg = str(e).lower()
        if "already" in msg or "is running" in msg:
            return True
        return False


# ── Window discovery / responsiveness ────────────────────────────────


def find_window_by_title_substring(needle: str, timeout: float = 1.0) -> Any:
    """Walk the desktop accessibility tree looking for a top-level
    window whose title contains the substring. Case-insensitive.
    Returns a uiautomation Control or None.
    """
    if not needle:
        return None
    auto = _ui_auto()
    needle_l = needle.lower()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            root = auto.GetRootControl()
            for child in root.GetChildren():
                try:
                    name = (child.Name or "")
                    if needle_l in name.lower():
                        return child
                except Exception:
                    continue
        except Exception:
            return None
        time.sleep(0.1)
    return None


def is_app_responsive(window_handle: int) -> bool:
    """Win32 SendMessageTimeoutW(WM_NULL, 500ms). Returns True if the
    app's message loop is alive, False if it's hung.
    """
    if not window_handle:
        return False
    try:
        user32 = ctypes.windll.user32
        result = ctypes.c_ulong()
        rv = user32.SendMessageTimeoutW(
            ctypes.c_void_p(window_handle), _WM_NULL, 0, 0,
            _SMTO_ABORTIFHUNG, 500, ctypes.byref(result),
        )
        return bool(rv)
    except Exception:
        return False


def list_visible_windows() -> list[dict[str, Any]]:
    """Return [{'title': ..., 'handle': hwnd}, ...] for visible top-level
    windows. HWND is exposed as an int so callers can probe responsiveness
    via SendMessageTimeoutW.
    """
    out: list[dict[str, Any]] = []
    try:
        user32 = ctypes.windll.user32

        # WINFUNCTYPE signature: BOOL CALLBACK EnumWindowsProc(HWND, LPARAM).
        # Use c_void_p for the HWND so it auto-converts to a Python int,
        # and LPARAM is signed int-ptr-sized.
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
        )

        def cb(hwnd, _lparam):
            try:
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        title = buf.value.strip()
                        if title:
                            out.append({"title": title, "handle": int(hwnd or 0)})
            except Exception:
                # Don't propagate; ctypes callbacks must return cleanly
                # or EnumWindows aborts with an opaque error.
                pass
            return True

        user32.EnumWindows(EnumWindowsProc(cb), 0)
    except Exception:
        return out
    return out[:200]


# ── Click / type ─────────────────────────────────────────────────────


def click_in_window(window_title_substring: str, control_criteria: dict[str, Any]) -> bool:
    """Bring the matching window forward, find a control by criteria, click it.

    control_criteria keys (any subset, all must match):
        - "title": substring match on Name
        - "control_type": uiautomation control type name (e.g. "ButtonControl")
        - "automation_id": exact match on AutomationId
    """
    auto = _ui_auto()
    win = find_window_by_title_substring(window_title_substring, timeout=1.5)
    if win is None:
        return False
    try:
        win.SetActive()
    except Exception:
        pass
    try:
        ctrl = _find_control_in(win, control_criteria, timeout=2.0)
        if ctrl is None:
            return False
        # Prefer InvokePattern (proper accessibility activation) over a
        # raw mouse click — it works on hidden / off-screen / scroll-out
        # controls too.
        try:
            ip = ctrl.GetInvokePattern()
            if ip is not None:
                ip.Invoke()
                return True
        except Exception:
            pass
        # Fallback: mouse click on the control's center.
        try:
            ctrl.Click()
            return True
        except Exception:
            return False
    except Exception:
        return False


def _find_control_in(parent, criteria: dict[str, Any], timeout: float = 2.0):
    """BFS the accessibility tree under `parent` for a control matching
    all keys in `criteria`. Returns the first match or None.
    """
    deadline = time.time() + timeout
    title = str(criteria.get("title") or "").lower()
    ctype = str(criteria.get("control_type") or "")
    aid = str(criteria.get("automation_id") or "")

    while time.time() < deadline:
        queue = [parent]
        while queue:
            node = queue.pop(0)
            try:
                if title and title not in (node.Name or "").lower():
                    pass
                elif ctype and node.ControlTypeName != ctype:
                    pass
                elif aid and (node.AutomationId or "") != aid:
                    pass
                else:
                    # All provided criteria matched.
                    return node
                queue.extend(node.GetChildren())
            except Exception:
                continue
        time.sleep(0.15)
    return None


def type_text_in_window(window_title_substring: str, text: str) -> bool:
    """Bring window forward and send keystrokes."""
    if not text:
        return False
    auto = _ui_auto()
    win = find_window_by_title_substring(window_title_substring, timeout=1.5)
    if win is None:
        return False
    try:
        win.SetActive()
    except Exception:
        pass
    try:
        # uiautomation.SendKeys treats {} () + ^ % ~ as special. Escape them.
        out = []
        for ch in text:
            if ch in "{}()+^%~":
                out.append("{" + ch + "}")
            else:
                out.append(ch)
        auto.SendKeys("".join(out))
        return True
    except Exception:
        return False


def read_window_text(window_title_substring: str, max_chars: int = 4000) -> str:
    """Walk the accessibility tree of the matching window and return a
    text summary (concatenation of Name + ValuePattern values).
    """
    win = find_window_by_title_substring(window_title_substring, timeout=1.0)
    if win is None:
        return ""
    parts: list[str] = []
    queue = [win]
    while queue and sum(len(p) for p in parts) < max_chars:
        node = queue.pop(0)
        try:
            name = (node.Name or "").strip()
            if name and name not in parts:
                parts.append(name)
            try:
                vp = node.GetValuePattern()
                if vp is not None:
                    val = str(vp.Value or "").strip()
                    if val and val not in parts:
                        parts.append(val)
            except Exception:
                pass
            queue.extend(node.GetChildren())
        except Exception:
            continue
    text = "\n".join(parts)
    return text[:max_chars]


def navigate_explorer(path: str) -> bool:
    """Open a File Explorer window at the given path. Cheap path:
    `Start-Process explorer.exe <path>`.
    """
    if not path or not Path(path).exists():
        return False
    try:
        subprocess.Popen(
            ["explorer.exe", os.path.normpath(path)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception:
        return False


def back_out_explorer_window(target_path: str) -> bool:
    """If a File Explorer window is at the given path, send Alt+Up to it
    so it backs out to the parent directory. Returns True if a matching
    window was found (regardless of whether the keystroke landed).
    """
    if not target_path:
        return False
    auto = _ui_auto()
    target_l = target_path.lower().replace("/", "\\")
    target_basename = Path(target_path).name.lower()
    found = False
    for w in list_visible_windows():
        title_l = w["title"].lower()
        if target_basename and target_basename in title_l:
            found = True
            try:
                # Activate then Alt+Up.
                user32 = ctypes.windll.user32
                user32.SetForegroundWindow(w["handle"])
                time.sleep(0.05)
                auto.SendKeys("%{Up}")
            except Exception:
                pass
    return found


# ── Clipboard primitives ──────────────────────────────────────────────


def set_clipboard(text: str) -> bool:
    """Write `text` to the Windows clipboard via pywin32. Returns True on
    success. Used by cu_clipboard_write and as the first half of the
    atomic-paste alternative to per-character keystroke typing.
    """
    if text is None:
        return False
    try:
        import win32clipboard  # type: ignore
        import win32con  # type: ignore
    except ImportError:
        # Fallback: shell out to clip.exe (built-in on Windows since Vista).
        try:
            proc = subprocess.run(
                ["clip.exe"], input=str(text), text=True, encoding="utf-16le",
                timeout=2.0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return proc.returncode == 0
        except Exception:
            return False
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, str(text))
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


def paste_into_window(window_title_substring: str) -> bool:
    """Bring the matching window to foreground, then send Ctrl+V. Returns
    True if the window was found and the keystroke was sent (does not
    verify the paste actually landed — caller can re-read the window if
    needed).
    """
    auto = _ui_auto()
    win = find_window_by_title_substring(window_title_substring, timeout=1.5)
    if win is None:
        return False
    try:
        win.SetActive()
    except Exception:
        pass
    try:
        time.sleep(0.05)  # tiny settle so SetActive completes
        auto.SendKeys("^v")
        return True
    except Exception:
        return False


def type_text_via_clipboard(window_title_substring: str, text: str) -> bool:
    """Atomic alternative to per-character keystroke typing. Set clipboard,
    focus window, send Ctrl+V. Restores prior clipboard content best-effort.
    """
    if text is None:
        return False
    # Save prior clipboard content so we can restore it after the paste.
    prior: str | None = None
    try:
        import win32clipboard  # type: ignore
        import win32con  # type: ignore
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    prior = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            pass
    except ImportError:
        pass

    if not set_clipboard(text):
        return False
    ok = paste_into_window(window_title_substring)
    # Best-effort restore.
    if prior is not None:
        try:
            time.sleep(0.05)
            set_clipboard(prior)
        except Exception:
            pass
    return ok


# ── Window candidates / close primitives ──────────────────────────────


# Browser executable names — used when "close X" might be a tab in a browser
# rather than a desktop app.
BROWSER_PROCESS_NAMES = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe")


def find_window_candidates(name: str) -> list[dict[str, Any]]:
    """Return all visible top-level windows whose title contains `name`
    (case-insensitive). Each entry: {title, handle, process_name, kind}.
    `kind` is "desktop" if the owning process is not a browser, "browser_tab"
    if the matching window's title is in a browser process (likely the active
    tab). Used by close_app and similar disambiguating operations.
    """
    if not name:
        return []
    needle = name.lower()
    candidates: list[dict[str, Any]] = []
    try:
        proc_names_by_pid = _process_names_snapshot()
    except Exception:
        proc_names_by_pid = {}
    for w in list_visible_windows():
        if needle not in w["title"].lower():
            continue
        hwnd = int(w.get("handle") or 0)
        pid = _hwnd_pid(hwnd)
        proc_name = proc_names_by_pid.get(pid, "")
        kind = "browser_tab" if proc_name.lower() in BROWSER_PROCESS_NAMES else "desktop"
        candidates.append({
            "title": w["title"],
            "handle": hwnd,
            "pid": pid,
            "process_name": proc_name,
            "kind": kind,
        })
    return candidates


def _process_names_snapshot() -> dict[int, str]:
    """Map pid → process exe name. One snapshot per call (cheap, ~5–20ms)."""
    out: dict[int, str] = {}
    try:
        import psutil  # type: ignore
    except ImportError:
        return out
    try:
        for p in psutil.process_iter(["pid", "name"]):
            try:
                out[int(p.info["pid"])] = str(p.info["name"] or "")
            except Exception:
                continue
    except Exception:
        pass
    return out


def _hwnd_pid(hwnd: int) -> int:
    """GetWindowThreadProcessId → return the pid for an HWND."""
    if not hwnd:
        return 0
    try:
        user32 = ctypes.windll.user32
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def close_window_by_handle(hwnd: int) -> bool:
    """Send WM_CLOSE to the window. Returns True if the message was sent
    (does not verify the window actually closed — caller can re-list).
    Uses PostMessage so an unresponsive app doesn't block this primitive.
    """
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        WM_CLOSE = 0x0010
        return bool(user32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, 0, 0))
    except Exception:
        return False


def close_app_by_pid(pid: int, force: bool = False) -> bool:
    """Close all windows of a process by pid, or force-terminate.
    `force=False`: post WM_CLOSE to all the process's windows (graceful).
    `force=True`: TerminateProcess via psutil (last-resort).
    """
    if not pid:
        return False
    if force:
        try:
            import psutil  # type: ignore
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=2.0)
            except Exception:
                p.kill()
            return True
        except Exception:
            return False
    # Graceful: WM_CLOSE to every top-level window of the pid.
    closed_any = False
    for w in list_visible_windows():
        if _hwnd_pid(int(w.get("handle") or 0)) == pid:
            if close_window_by_handle(int(w["handle"])):
                closed_any = True
    return closed_any


def close_browser_tab_by_title(needle: str, last_n: int | None = None) -> int:
    """Close browser tabs whose title contains `needle`. Sends Ctrl+W to
    the focused tab once it's been brought forward. Returns count of tabs
    closed.

    `last_n`: if set, close at most this many of the matching tabs. Useful
    for "close my last 3 google tabs" — the caller has filtered to the
    candidate set already; this closes up to last_n of them in MRU order.
    """
    auto = _ui_auto()
    needle_l = (needle or "").lower()
    candidates = [w for w in list_visible_windows()
                  if needle_l in w["title"].lower()
                  and _process_names_snapshot().get(_hwnd_pid(int(w.get("handle") or 0)), "").lower() in BROWSER_PROCESS_NAMES]
    # MRU = list_visible_windows order (Windows returns top-of-Z first); newest first
    if last_n is not None:
        candidates = candidates[:max(0, int(last_n))]
    closed = 0
    user32 = ctypes.windll.user32
    for cand in candidates:
        hwnd = int(cand.get("handle") or 0)
        if not hwnd:
            continue
        try:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
            auto.SendKeys("^w")
            closed += 1
            time.sleep(0.05)
        except Exception:
            continue
    return closed
