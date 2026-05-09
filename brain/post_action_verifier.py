"""brain/post_action_verifier.py — Self-awareness of failure (A1).

After Ava claims to perform an action (open app, close app, type text),
this module verifies that the side-effect ACTUALLY happened. If it
didn't, she gets back a structured "this failed" so she can reply
honestly ("wait, that didn't work, let me try again") instead of
silently false-success.

The verifier is process-strict (matches by exe name), foreground-aware
(distinguishes "window exists" from "window is visible/focused"), and
time-bounded (waits up to a few seconds for slow apps to spawn windows).

Why this exists: today's Phase B + Zeke's hardware testing surfaced
several silent-failure bugs:
- "Opening Chrome" → Discord with embedded Chromium matched dedup → false
- "Opening Microsoft Edge" → .lnk shortcut launched via Popen silently
  failed → no window appeared but reply said "Opening"
Without self-verification, these failures look identical to successes
from Ava's side. With it, she notices and can retry or honestly admit
she couldn't.

This is the Tier-A foundation for honest action confirmation in voice
loop. Action-tag router and voice command handlers wrap their action
calls through verify_after_open / verify_after_close / verify_after_type.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any


# How long to wait for an app to spawn its window after launch.
_OPEN_WAIT_SECONDS_DEFAULT = 5.0
_OPEN_POLL_INTERVAL = 0.4

# How long to wait for an app to actually exit after close.
_CLOSE_WAIT_SECONDS_DEFAULT = 3.0
_CLOSE_POLL_INTERVAL = 0.3


def _resolve_target_exe(name: str) -> str:
    """Return canonical exe filename for `name` (e.g. 'chrome' → 'chrome.exe').

    Empty string if we don't know — caller treats as "any window match
    counts" (lenient mode).
    """
    try:
        from tools.system.app_launcher import _resolve_app
        exe_path, canonical = _resolve_app(name)
        if exe_path and isinstance(exe_path, str):
            return Path(exe_path).name.lower()
        if canonical:
            return f"{canonical}.exe"
    except Exception:
        pass
    return ""


def _matching_windows(name: str, target_exe: str) -> list[dict[str, Any]]:
    """All visible top-level windows for the target. Process-strict if we
    have a canonical exe; lenient if not."""
    try:
        from brain.windows_use.primitives import find_window_candidates
        cands = find_window_candidates(name) or []
    except Exception:
        return []
    if not target_exe:
        return cands
    return [
        c for c in cands
        if str(c.get("process_name") or "").lower() == target_exe
    ]


def _foreground_window_info() -> dict[str, Any]:
    """Return info about the currently focused window: title, hwnd, pid, exe.

    Empty dict if Win32 calls fail.
    """
    try:
        import ctypes
        import ctypes.wintypes as _wt
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow())
        if not hwnd:
            return {}
        # Title
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        # PID
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        # Exe name from pid
        exe = ""
        try:
            import psutil
            proc = psutil.Process(int(pid.value))
            exe = (proc.name() or "").lower()
        except Exception:
            pass
        return {
            "hwnd": hwnd,
            "title": title,
            "pid": int(pid.value),
            "process_name": exe,
        }
    except Exception:
        return {}


def verify_after_open(
    name: str,
    *,
    wait_seconds: float = _OPEN_WAIT_SECONDS_DEFAULT,
    require_foreground: bool = False,
) -> dict[str, Any]:
    """Verify a `open <name>` action actually produced a visible window.

    Polls for up to `wait_seconds` to allow slow apps to spawn. Returns
    a structured result:

      {
        "verified": bool,
        "windows_found": int,
        "foreground_match": bool,
        "elapsed_seconds": float,
        "target_exe": str,
        "explanation": str,    # human-readable, used for honest reply
      }
    """
    target_exe = _resolve_target_exe(name)
    t0 = time.time()
    deadline = t0 + max(0.5, wait_seconds)
    windows: list[dict[str, Any]] = []
    while time.time() < deadline:
        windows = _matching_windows(name, target_exe)
        if windows:
            break
        time.sleep(_OPEN_POLL_INTERVAL)
    elapsed = time.time() - t0

    if not windows:
        return {
            "verified": False,
            "windows_found": 0,
            "foreground_match": False,
            "elapsed_seconds": elapsed,
            "target_exe": target_exe,
            "explanation": (
                f"I said I'd open {name}, but I don't see any "
                f"{target_exe or name} window after waiting "
                f"{int(elapsed)} seconds. Something's off."
            ),
        }

    foreground_match = False
    if require_foreground:
        fg = _foreground_window_info()
        if fg and target_exe:
            foreground_match = fg.get("process_name", "").lower() == target_exe
        elif fg:
            # Lenient mode without target_exe — match by name in title
            foreground_match = name.lower() in (fg.get("title") or "").lower()

    return {
        "verified": True if not require_foreground else foreground_match,
        "windows_found": len(windows),
        "foreground_match": foreground_match,
        "elapsed_seconds": elapsed,
        "target_exe": target_exe,
        "explanation": "" if (foreground_match or not require_foreground) else (
            f"I opened {name}, but it's not in front. "
            f"It might have opened behind something."
        ),
    }


def verify_after_close(
    name: str,
    *,
    wait_seconds: float = _CLOSE_WAIT_SECONDS_DEFAULT,
) -> dict[str, Any]:
    """Verify a `close <name>` action actually removed all windows.

    Polls for up to `wait_seconds` to allow apps to drain. Returns same
    shape as verify_after_open with semantics inverted (verified=True
    means windows are all gone).
    """
    target_exe = _resolve_target_exe(name)
    t0 = time.time()
    deadline = t0 + max(0.5, wait_seconds)
    windows: list[dict[str, Any]] = []
    while time.time() < deadline:
        windows = _matching_windows(name, target_exe)
        if not windows:
            break
        time.sleep(_CLOSE_POLL_INTERVAL)
    elapsed = time.time() - t0

    if windows:
        return {
            "verified": False,
            "windows_remaining": len(windows),
            "elapsed_seconds": elapsed,
            "target_exe": target_exe,
            "explanation": (
                f"I tried to close {name}, but {len(windows)} "
                f"{target_exe or name} window(s) are still visible. "
                f"It might not have closed cleanly."
            ),
        }
    return {
        "verified": True,
        "windows_remaining": 0,
        "elapsed_seconds": elapsed,
        "target_exe": target_exe,
        "explanation": "",
    }


def verify_after_type(
    expected_text: str,
    *,
    sample_chars: int = 30,
) -> dict[str, Any]:
    """Verify a `type <text>` action by reading the clipboard.

    The TYPE_TEXT path in action_tag_router uses Ctrl+V from the
    clipboard. So a successful "type" leaves `expected_text` on the
    clipboard at minimum (regardless of whether the window accepted
    the paste). We can't directly verify that the focused window
    received the text without OCR / app-specific introspection — but
    we CAN verify the clipboard payload matches what we intended.

    Returns:
      {"verified": bool, "clipboard_chars": int,
       "marker_match": bool, "explanation": str}
    """
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            clip = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception as e:
        return {
            "verified": False,
            "clipboard_chars": 0,
            "marker_match": False,
            "explanation": f"I couldn't read the clipboard to verify the paste: {e!r}",
        }

    clip_str = str(clip or "")
    expected = (expected_text or "").strip()
    # Use a sample marker — the first N chars of the expected text — so
    # we don't need exact equality (line-ending normalization, etc).
    marker = expected[: max(8, min(sample_chars, len(expected)))]
    marker_match = marker.lower() in clip_str.lower() if marker else False

    return {
        "verified": marker_match,
        "clipboard_chars": len(clip_str),
        "marker_match": marker_match,
        "explanation": "" if marker_match else (
            f"The clipboard doesn't have what I tried to paste — "
            f"the text might not have made it through."
        ),
    }


def humanize_failure(action: str, name: str, verify_result: dict[str, Any]) -> str:
    """Produce an honest spoken reply for a verification failure.

    Used when an action's apparent reply ("Opening Chrome.") would have
    been false. Replaces it with a self-aware "actually that didn't
    work" reply.
    """
    explanation = str(verify_result.get("explanation") or "")
    if explanation:
        return explanation
    if action == "open":
        return f"I tried to open {name}, but I don't think it actually opened."
    if action == "close":
        return f"I tried to close {name}, but it's still there."
    if action == "type":
        return "I tried to paste that, but I don't think it went through."
    return "Something didn't work the way I said it did."


def wrap_open_with_verification(
    name: str,
    open_fn,
    *,
    wait_seconds: float = _OPEN_WAIT_SECONDS_DEFAULT,
) -> tuple[bool, str]:
    """Run `open_fn()` (which returns its own (ok, msg)), then verify.

    Returns (final_ok, final_msg). final_msg is honest about failure if
    verification disagrees with the apparent success.
    """
    try:
        ok, msg = open_fn()
    except Exception as e:
        return False, f"I had an error trying to open {name}: {e!r}"

    if not ok:
        # The opener itself reported failure. Trust it; no verification needed.
        return False, msg

    # Apparent success — verify.
    result = verify_after_open(name, wait_seconds=wait_seconds)
    if result.get("verified"):
        return True, msg

    return False, humanize_failure("open", name, result)


def wrap_close_with_verification(
    name: str,
    close_fn,
    *,
    wait_seconds: float = _CLOSE_WAIT_SECONDS_DEFAULT,
) -> tuple[bool, str]:
    """Run `close_fn()` (returns its own (ok, msg)), then verify."""
    try:
        ok, msg = close_fn()
    except Exception as e:
        return False, f"I had an error trying to close {name}: {e!r}"

    if not ok:
        return False, msg

    result = verify_after_close(name, wait_seconds=wait_seconds)
    if result.get("verified"):
        return True, msg

    return False, humanize_failure("close", name, result)
