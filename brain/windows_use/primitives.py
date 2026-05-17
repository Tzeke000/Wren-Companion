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
    """Bring window forward and send keystrokes.

    Uses AttachThreadInput-based focus when an HWND is resolvable, falling
    back to uiautomation's SetActive otherwise. SendKeys still uses
    uiautomation here because that path delivers individual character keys
    correctly; the broken case was specifically chord keystrokes like Ctrl+V
    (handled by paste_into_window).
    """
    if not text:
        return False
    auto = _ui_auto()
    win = find_window_by_title_substring(window_title_substring, timeout=1.5)
    if win is None:
        return False
    hwnd = _resolve_hwnd(win)
    if hwnd is not None:
        _force_foreground(hwnd)
    else:
        try:
            win.SetActive()
        except Exception:
            pass
    try:
        time.sleep(0.05)  # settle
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


def _resolve_hwnd(win: Any) -> int | None:
    """Pull a Win32 HWND out of whatever the lib exposed on its WindowControl."""
    for attr in ("NativeWindowHandle", "Handle", "native_handle", "handle"):
        v = getattr(win, attr, None)
        if isinstance(v, int) and v > 0:
            return int(v)
    return None


def _force_foreground(hwnd: int) -> bool:
    """Steal foreground reliably using AttachThreadInput. Plain
    SetForegroundWindow fails silently under Windows' foreground-lock unless
    we attach to the current foreground thread first.
    """
    try:
        import ctypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        fg = u32.GetForegroundWindow()
        fg_tid = u32.GetWindowThreadProcessId(fg, None)
        my_tid = k32.GetCurrentThreadId()
        u32.AttachThreadInput(my_tid, fg_tid, True)
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        u32.BringWindowToTop(hwnd)
        ok = bool(u32.SetForegroundWindow(hwnd))
        u32.AttachThreadInput(my_tid, fg_tid, False)
        return ok
    except Exception:
        return False


def paste_into_window(window_title_substring: str) -> bool:
    """Bring the matching window to foreground, then send Ctrl+V via
    System.Windows.Forms.SendKeys (PowerShell-style) which delivers the
    modifier correctly. Returns True if the window was found and the
    keystroke was sent (does not verify the paste actually landed — caller
    can re-read the window if needed).

    History 2026-05-11: the original implementation used uiautomation's
    `SendKeys("^v")` after `win.SetActive()`. On this Windows 10 build both
    sides of that failed silently — SetActive didn't reliably steal focus,
    and even when it did the SendKeys delivery showed up as literal "^v"
    characters in Notepad's Edit control. Replaced with AttachThreadInput +
    SetForegroundWindow + SendInput (via WSCRIPT shell or PowerShell as the
    Ctrl+V dispatcher).
    """
    win = find_window_by_title_substring(window_title_substring, timeout=1.5)
    if win is None:
        return False
    hwnd = _resolve_hwnd(win)
    if hwnd is None:
        # Fall back to uiautomation focus if HWND isn't exposed.
        try:
            win.SetActive()
        except Exception:
            pass
    else:
        _force_foreground(hwnd)
    try:
        time.sleep(0.08)  # small settle so the foreground swap completes
        # Send Ctrl+V via Win32 SendInput directly. Using virtual-key codes
        # so we don't depend on layout-specific scan codes.
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32

        VK_CONTROL = 0x11
        VK_V = 0x56
        KEYEVENTF_KEYUP = 0x0002
        INPUT_KEYBOARD = 1

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class _INPUT_UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

        def _send(vk: int, up: bool) -> None:
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
            u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

        _send(VK_CONTROL, False)
        _send(VK_V, False)
        _send(VK_V, True)
        _send(VK_CONTROL, True)
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


# ── Atomic input primitives ───────────────────────────────────────────
# These hold foreground through a click+keystroke sequence in one process,
# avoiding the focus race that breaks separate MCP-tool calls. The pattern:
# focus target window, click at coords, immediately send keystroke combo,
# all inside one function call. No round-trip back to the MCP layer means
# CC's terminal can't reclaim foreground mid-sequence.


def _send_key_combo(vks: list[int]) -> None:
    """Press all virtual-keys in `vks` in order, then release in reverse.
    Used for chord keystrokes like Ctrl+V, Ctrl+A, Ctrl+S, Alt+Tab, etc.
    """
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32

    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

    def _send(vk: int, up: bool) -> None:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
        u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    for vk in vks:
        _send(vk, False)
    for vk in reversed(vks):
        _send(vk, True)


def _click_at_atomic(x: int, y: int) -> None:
    """SetCursorPos + mouse_event left-click. Called inline inside the
    atomic functions to avoid an MCP round-trip."""
    import ctypes
    u32 = ctypes.windll.user32
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    u32.mouse_event(0x02, 0, 0, 0, 0)  # LEFTDOWN
    u32.mouse_event(0x04, 0, 0, 0, 0)  # LEFTUP


# Virtual-key codes used by the atomic helpers
_VK = {
    "ctrl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46,
    "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C,
    "m": 0x4D, "n": 0x4E, "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52,
    "s": 0x53, "t": 0x54, "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58,
    "y": 0x59, "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "insert": 0x2D,
}


def paste_at(window_title_substring: str, x: int, y: int, text: str) -> bool:
    """Atomic paste: set clipboard, focus target window, click at (x,y),
    Ctrl+V. All in one process — no MCP round-trip between steps, so
    CC's terminal can't reclaim foreground mid-sequence.

    Returns True if window was found and the sequence ran. Caller should
    verify with a screen_grab; this does not confirm the paste landed.

    Args:
        window_title_substring: substring match for the target window title.
        x, y: virtual-desktop coords to click before pasting.
        text: content to set on the clipboard and paste.
    """
    if text is None:
        return False
    # Save prior clipboard for restore.
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

    win = find_window_by_title_substring(window_title_substring, timeout=1.5)
    if win is None:
        return False
    hwnd = _resolve_hwnd(win)
    if hwnd is not None:
        _force_foreground(hwnd)
    else:
        try:
            win.SetActive()
        except Exception:
            pass

    try:
        time.sleep(0.08)  # settle after foreground swap
        _click_at_atomic(x, y)
        time.sleep(0.05)  # let the click register
        _send_key_combo([_VK["ctrl"], _VK["v"]])
        ok = True
    except Exception:
        ok = False

    # Best-effort clipboard restore.
    if prior is not None:
        try:
            time.sleep(0.08)
            set_clipboard(prior)
        except Exception:
            pass
    return ok


def select_all_clear(window_title_substring: str, x: int, y: int) -> bool:
    """Atomic select-all-delete: focus target window, click at (x,y),
    Ctrl+A, Delete. Clears the active edit control's content. One process,
    no MCP round-trip — same reason as paste_at.
    """
    win = find_window_by_title_substring(window_title_substring, timeout=1.5)
    if win is None:
        return False
    hwnd = _resolve_hwnd(win)
    if hwnd is not None:
        _force_foreground(hwnd)
    else:
        try:
            win.SetActive()
        except Exception:
            pass
    try:
        time.sleep(0.08)
        _click_at_atomic(x, y)
        time.sleep(0.05)
        _send_key_combo([_VK["ctrl"], _VK["a"]])
        time.sleep(0.04)
        _send_key_combo([_VK["delete"]])
        return True
    except Exception:
        return False


def hotkey_at(window_title_substring: str, x: int, y: int, combo: str) -> bool:
    """Atomic click-then-hotkey: focus window, click at (x,y), send a key
    combo. `combo` is a "+"-joined string of keys, e.g. "ctrl+s",
    "ctrl+shift+t", "alt+f4", "f5", "enter". One process, no race.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return False
    vks: list[int] = []
    for p in parts:
        vk = _VK.get(p)
        if vk is None:
            return False
        vks.append(vk)

    win = find_window_by_title_substring(window_title_substring, timeout=1.5)
    if win is None:
        return False
    hwnd = _resolve_hwnd(win)
    if hwnd is not None:
        _force_foreground(hwnd)
    else:
        try:
            win.SetActive()
        except Exception:
            pass
    try:
        time.sleep(0.08)
        _click_at_atomic(x, y)
        time.sleep(0.05)
        _send_key_combo(vks)
        return True
    except Exception:
        return False


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
