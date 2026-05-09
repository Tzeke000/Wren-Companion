"""
Background daemons.

Heartbeat is the one thing that intentionally ticks on a timer — it's Ava's
internal rhythm, not a response to external events. Everything else is
event-driven via brain.signal_bus:

  - clipboard changes → Win32 AddClipboardFormatListener → fires CLIPBOARD_CHANGED
  - foreground window switches → Win32 SetWinEventHook → fires WINDOW_CHANGED
  - new app installs → Win32 ReadDirectoryChangesW → fires APP_INSTALLED
  - face transitions → InsightFace per-frame loop → fires FACE_*
  - reminders due → reminder tool → fires REMINDER_DUE (urgent)

Polling fallbacks exist where the Win32 path is unavailable, but on the
target Windows machine the watchers run with effectively zero CPU when
nothing is happening.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from brain.signal_bus import (
    SIGNAL_ACTIVE_WINDOW_CHANGED,
    SIGNAL_CLIPBOARD_CHANGED,
    SIGNAL_EXPRESSION_CHANGED,
    SIGNAL_FACE_APPEARED,
    SIGNAL_FACE_CHANGED,
    SIGNAL_FACE_LOST,
    SIGNAL_NEW_APP_INSTALLED,
)


_HB_INTERVAL = 30.0
_VIDEO_INTERVAL = 1.0 / 15.0  # 15 fps
_CLIPBOARD_FALLBACK_INTERVAL = 3.0

_CODE_HINTS = (
    "def ", "class ", "import ", "function ", "const ", "var ", "let ",
    "return ", "if (", "{\n", "}\n",
)


# ── Heartbeat (timed, intentional) ────────────────────────────────────────────

def _heartbeat_loop(g: dict[str, Any]) -> None:
    while True:
        time.sleep(_HB_INTERVAL)
        try:
            from brain.heartbeat import run_heartbeat_tick_safe, apply_heartbeat_to_perception_state
            from brain.perception import PerceptionState
            workspace = g.get("workspace")
            hb = run_heartbeat_tick_safe(
                g=g, user_text="",
                selftests=None, workbench=None, strategic_continuity=None,
                curiosity=None, outcome_learning=None, improvement_loop=None,
                social_continuity=None, model_routing=None, memory_refinement=None,
            )
            if workspace is not None:
                state = getattr(workspace, "_state", None)
                if state is None:
                    state = type("WS", (), {})()
                    state.perception = PerceptionState()
                if hasattr(state, "perception"):
                    try:
                        apply_heartbeat_to_perception_state(state.perception, type("Bundle", (), {"heartbeat": hb})())
                    except Exception:
                        pass
            g["_heartbeat_last_mode"] = str(getattr(hb, "heartbeat_mode", "") or "")
            g["_heartbeat_last_summary"] = str(getattr(hb, "heartbeat_summary", "") or "")
            g["_heartbeat_last_tick_id"] = int(getattr(hb, "heartbeat_tick_id", 0) or 0)
            g["_heartbeat_last_ts"] = time.time()
        except Exception as e:
            print(f"[background_tick] heartbeat error: {e}")


# ── Video capture (timed, throttled — necessary for camera) ──────────────────

def _video_frame_capture_thread(g: dict[str, Any]) -> None:
    import cv2
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[video_capture] camera not available")
        cm = g.get("camera_manager")
        if cm is not None:
            cm.running = False
        return
    print("[video_capture] camera opened, streaming at 15fps")
    # Publish capture-running flag so subsystem_health.camera.running reflects
    # actual streaming state. Reader: operator_server.py:1087.
    cm = g.get("camera_manager")
    if cm is not None:
        cm.running = True

    _frame_idx = 0
    _insight_every_n = 3  # ~5fps face detection at 15fps capture

    # Per-thread state for face transition signals.
    _last_seen_pid: Optional[str] = None
    _last_seen_expr: Optional[str] = None
    _had_face = False

    while True:
        try:
            ret, frame = cap.read()
            if ret and frame is not None:
                _frame_idx += 1

                bus = g.get("_signal_bus")

                # InsightFace on every Nth frame.
                insight = g.get("_insight_face")
                if insight is not None and getattr(insight, "available", False):
                    if _frame_idx % _insight_every_n == 0:
                        try:
                            face_results = insight.analyze_frame(frame)
                            g["_face_results"] = face_results
                            if face_results:
                                best = max(face_results, key=lambda r: float(r.get("confidence") or 0.0))
                                pid = str(best.get("person_id") or "unknown")
                                conf = float(best.get("confidence") or 0.0)
                                g["_recognized_person_id"] = pid
                                g["_recognized_confidence"] = conf
                                g["_recognized_age"] = best.get("age", 0)
                                g["_face_age"] = best.get("age", 0)
                                g["_recognized_gender"] = best.get("gender", "?")
                                g["_face_gender"] = best.get("gender", "?")

                                # ── Face transition signals ──────────────
                                if bus is not None:
                                    if not _had_face:
                                        bus.fire(
                                            SIGNAL_FACE_APPEARED,
                                            data={"person_id": pid, "confidence": conf},
                                            priority="medium",
                                        )
                                    elif _last_seen_pid is not None and pid != _last_seen_pid and conf > 0.5:
                                        bus.fire(
                                            SIGNAL_FACE_CHANGED,
                                            data={"from": _last_seen_pid, "to": pid, "confidence": conf},
                                            priority="medium",
                                        )
                                _had_face = True
                                _last_seen_pid = pid

                                # Expression calibration + detection per recognized person.
                                cal = g.get("_expression_calibrator")
                                if cal is not None:
                                    lm = best.get("landmarks")
                                    if lm is not None:
                                        try:
                                            if pid != "unknown":
                                                cal.calibrate_baseline(pid, lm)
                                            expr = cal.detect_expression(pid, lm)
                                            g["_current_expression"] = expr
                                            if bus is not None and expr and expr != _last_seen_expr:
                                                bus.fire(
                                                    SIGNAL_EXPRESSION_CHANGED,
                                                    data={"from": _last_seen_expr, "to": expr, "person_id": pid},
                                                    priority="low",
                                                )
                                            _last_seen_expr = expr
                                        except Exception as _ce:
                                            print(f"[video_capture] calibrator error: {_ce}")
                            else:
                                # Face lost transition.
                                if _had_face and bus is not None:
                                    bus.fire(
                                        SIGNAL_FACE_LOST,
                                        data={"last_person_id": _last_seen_pid or "unknown"},
                                        priority="low",
                                    )
                                _had_face = False
                                _last_seen_pid = None
                                g["_recognized_person_id"] = "unknown"
                                g["_recognized_confidence"] = 0.0

                            # ── Temporal filter (2026-05-04) — promotes
                            # persistent unknown faces to "new person detected"
                            # only after 12s of continuous unknown visibility.
                            # Filters out brief look-aways and recognition jitter.
                            try:
                                from brain import face_tracking
                                _ft_pid = g.get("_recognized_person_id") or None
                                _ft_pid = None if _ft_pid in ("", "unknown") else _ft_pid
                                face_tracking.update(g, recognized_person_id=_ft_pid,
                                                     similarity=g.get("_recognized_confidence"))
                            except Exception as _fte:
                                # Don't let face_tracking break the camera loop.
                                pass
                        except Exception as _ie:
                            print(f"[video_capture] insight analyze error: {_ie}")

                # Annotate the frame with whatever face_results we currently have.
                annotated = frame
                try:
                    from brain.camera_annotator import annotate_frame as _annotate
                    annotated = _annotate(frame, g.get("_face_results"), g)
                except Exception as _ae:
                    print(f"[video_capture] annotate error: {_ae}")

                try:
                    from brain.frame_store import push_frame as _push_frame
                    _push_frame(annotated)
                except Exception:
                    pass

                vm = g.get("_video_memory")
                et = g.get("_expression_detector")
                ez = g.get("_eye_tracker")
                expression = ""
                gaze = ""
                if et and getattr(et, "available", False):
                    try:
                        expr = et.detect_expression(frame)
                        if expr:
                            expression = expr.get("dominant", "")
                    except Exception:
                        pass
                if ez and getattr(ez, "available", False) and getattr(ez, "calibrated", False):
                    try:
                        gaze = ez.get_gaze_region(frame) or ""
                    except Exception:
                        pass
                if vm:
                    vm.add_frame(frame, expression=expression, gaze=gaze)
            time.sleep(_VIDEO_INTERVAL)
        except Exception as e:
            print(f"[video_capture] error: {e}")
            time.sleep(2)
            try:
                cap.release()
            except Exception:
                pass
            cap = cv2.VideoCapture(0)


# ── Clipboard watcher (Win32 event-driven, polling fallback) ─────────────────

def _classify_clipboard(content: str) -> str:
    if not isinstance(content, str):
        return "text"
    low = content.lower().lstrip()
    if low.startswith(("http://", "https://")):
        return "url"
    if any(kw in content for kw in _CODE_HINTS):
        return "code"
    if "@" in content and "." in content and len(content) < 320:
        return "email"
    return "text"


def _record_clipboard_change(g: dict[str, Any], content: str) -> None:
    snippet = content[:500]
    ctype = _classify_clipboard(content)
    g["_clipboard_content"] = snippet
    g["_clipboard_type"] = ctype
    g["_clipboard_changed_ts"] = time.time()
    bus = g.get("_signal_bus")
    if bus is not None:
        bus.fire(
            SIGNAL_CLIPBOARD_CHANGED,
            data={
                "type": ctype,
                "preview": snippet[:80],
                "length": len(content),
            },
            priority="low",
        )
    print(f"[clipboard] changed: {ctype} ({len(content)} chars)")


def _read_current_clipboard() -> Optional[str]:
    try:
        import pyperclip  # type: ignore
        v = pyperclip.paste()
        if isinstance(v, str) and v.strip():
            return v
    except Exception:
        pass
    return None


def _clipboard_poll_fallback(g: dict[str, Any]) -> None:
    """Used if the Win32 message-window watcher fails to install."""
    print(f"[clipboard] running poll fallback ({_CLIPBOARD_FALLBACK_INTERVAL:.1f}s interval)")
    last = ""
    while True:
        current = _read_current_clipboard()
        if current is not None and current != last:
            last = current
            try:
                _record_clipboard_change(g, current)
            except Exception as e:
                print(f"[clipboard] handle error: {e}")
        time.sleep(_CLIPBOARD_FALLBACK_INTERVAL)


def _clipboard_watcher_thread(g: dict[str, Any]) -> None:
    """Win32 AddClipboardFormatListener → instant notification, zero CPU when idle."""
    try:
        import ctypes
        import ctypes.wintypes as wt
    except Exception:
        _clipboard_poll_fallback(g)
        return

    HWND_MESSAGE = -3
    WM_CLIPBOARDUPDATE = 0x031D
    WM_DESTROY = 0x0002

    user32 = ctypes.windll.user32
    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
    ]
    user32.AddClipboardFormatListener.restype = wt.BOOL
    user32.AddClipboardFormatListener.argtypes = [wt.HWND]

    # Register a real WindowProc so we can catch WM_CLIPBOARDUPDATE on this
    # message-only window. STATIC's default proc would also pump messages but
    # we want explicit handling.
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM,
    )

    def _wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_CLIPBOARDUPDATE:
            try:
                v = _read_current_clipboard()
                if v is not None:
                    _record_clipboard_change(g, v)
            except Exception as e:
                print(f"[clipboard] handle error: {e}")
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(_wnd_proc)

    # Use STATIC class — doesn't require RegisterClass. The CreateWindowExW
    # call returns a window handle that can subscribe to clipboard events.
    hwnd = user32.CreateWindowExW(
        0, "STATIC", "AvaClipboardWatcher", 0, 0, 0, 0, 0,
        HWND_MESSAGE, None, None, None,
    )
    if not hwnd:
        print("[clipboard] CreateWindowExW failed — using poll fallback")
        _clipboard_poll_fallback(g)
        return

    # Install our WndProc via SetWindowLongPtrW. The 64-bit signature requires
    # a LONG_PTR (c_ssize_t) argument and return value — without these argtypes
    # ctypes defaults to c_long which can't hold a 64-bit function pointer.
    GWLP_WNDPROC = -4
    try:
        SetWindowLongPtrW = user32.SetWindowLongPtrW
        SetWindowLongPtrW.restype = ctypes.c_ssize_t
        SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
        proc_ptr = ctypes.cast(proc, ctypes.c_void_p).value or 0
        SetWindowLongPtrW(hwnd, GWLP_WNDPROC, proc_ptr)
    except Exception as e:
        # 32-bit fallback uses SetWindowLongW with c_long.
        try:
            SetWindowLongW = user32.SetWindowLongW
            SetWindowLongW.restype = ctypes.c_long
            SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
            proc_ptr = ctypes.cast(proc, ctypes.c_void_p).value or 0
            SetWindowLongW(hwnd, GWLP_WNDPROC, proc_ptr & 0xFFFFFFFF)
        except Exception as e2:
            print(f"[clipboard] WndProc install failed ({e!r} / {e2!r}) — using poll fallback")
            _clipboard_poll_fallback(g)
            return

    if not user32.AddClipboardFormatListener(hwnd):
        print("[clipboard] AddClipboardFormatListener failed — using poll fallback")
        _clipboard_poll_fallback(g)
        return

    print("[clipboard] watching via Win32 AddClipboardFormatListener (zero-poll)")

    msg = wt.MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
        if ret <= 0:  # quit or error
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# ── Foreground window watcher (Win32 SetWinEventHook) ────────────────────────

def _detect_screen_context(title: str) -> str:
    t = (title or "").lower()
    if any(x in t for x in ["code", "visual studio", "pycharm", "intellij", "sublime", "notepad++", "vim", "rider"]):
        return "coding"
    if any(x in t for x in ["chrome", "firefox", "edge", "opera", "safari", "brave"]):
        return "browsing"
    if any(x in t for x in ["youtube", "netflix", "twitch", "plex", "vlc", "mpv", "media player"]):
        return "watching"
    if any(x in t for x in ["spotify", "music", "itunes", "winamp", "musicbee"]):
        return "listening"
    if any(x in t for x in ["steam", "epic", "minecraft", "fortnite", "valorant", "league of legends"]):
        return "gaming"
    if any(x in t for x in ["explorer", "finder", "files", "downloads"]):
        return "file_management"
    if any(x in t for x in ["word", "excel", "powerpoint", "outlook", "notion", "docs", "sheets"]):
        return "productivity"
    if t.strip() == "" or "desktop" in t:
        return "idle"
    return "general"


def _window_watcher_thread(g: dict[str, Any]) -> None:
    """Win32 SetWinEventHook on EVENT_SYSTEM_FOREGROUND. Zero-poll."""
    try:
        import ctypes
        import ctypes.wintypes as wt
    except Exception as e:
        print(f"[window_watcher] unavailable: {e}")
        return

    EVENT_SYSTEM_FOREGROUND = 0x0003
    WINEVENT_OUTOFCONTEXT = 0x0000

    user32 = ctypes.windll.user32

    WinEventProcType = ctypes.WINFUNCTYPE(
        None,
        wt.HANDLE, wt.DWORD, wt.HWND,
        ctypes.c_long, ctypes.c_long,
        wt.DWORD, wt.DWORD,
    )

    def callback(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            if not title:
                return
            old_title = g.get("_active_window_title") or ""
            old_context = g.get("_screen_context") or ""
            context = _detect_screen_context(title)
            g["_active_window_title"] = title
            g["_screen_context"] = context
            g["_window_changed_ts"] = time.time()
            if context != old_context or title != old_title:
                bus = g.get("_signal_bus")
                if bus is not None:
                    bus.fire(
                        SIGNAL_ACTIVE_WINDOW_CHANGED,
                        data={
                            "title": title[:120],
                            "context": context,
                            "old_context": old_context,
                            "old_title": old_title[:120],
                        },
                        priority="low",
                    )
                if context != old_context:
                    print(f"[window] {old_context or '?'} → {context}: {title[:60]}")
        except Exception as e:
            print(f"[window_watcher] callback error: {e}")

    proc = WinEventProcType(callback)
    hook = user32.SetWinEventHook(
        EVENT_SYSTEM_FOREGROUND,
        EVENT_SYSTEM_FOREGROUND,
        None,
        proc,
        0, 0,
        WINEVENT_OUTOFCONTEXT,
    )
    if not hook:
        print("[window_watcher] SetWinEventHook failed")
        return
    print("[window_watcher] hook installed (zero-poll foreground tracking)")

    # Hold a reference so the WinEventProc isn't GC'd.
    g["_window_watcher_proc_ref"] = proc

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# ── App install watcher (Win32 ReadDirectoryChangesW per path) ───────────────

def _watch_directory(path: str, g: dict[str, Any], debounce_sec: float = 10.0) -> None:
    try:
        import ctypes
        import ctypes.wintypes as wt
    except Exception:
        return

    FILE_LIST_DIRECTORY = 0x0001
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OVERLAPPED = 0x40000000  # not used — synchronous wait
    FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
    FILE_SHARE_ALL = 0x07
    INVALID_HANDLE_VALUE = -1

    kernel32 = ctypes.windll.kernel32

    handle = kernel32.CreateFileW(
        path,
        FILE_LIST_DIRECTORY,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        print(f"[app_watcher] CreateFileW failed for {path}")
        return

    print(f"[app_watcher] watching {path}")

    buf = ctypes.create_string_buffer(8192)
    bytes_returned = wt.DWORD(0)

    while True:
        ok = kernel32.ReadDirectoryChangesW(
            handle,
            buf, len(buf),
            True,  # watch subtree
            FILE_NOTIFY_CHANGE_DIR_NAME,
            ctypes.byref(bytes_returned),
            None, None,
        )
        if not ok:
            print(f"[app_watcher] ReadDirectoryChangesW failed for {path}")
            break

        # A new directory was created/renamed — likely an install.
        bus = g.get("_signal_bus")
        if bus is not None:
            bus.fire(
                SIGNAL_NEW_APP_INSTALLED,
                data={"path": path},
                priority="low",
            )

        # Debounce: rescan once per debounce_sec at most.
        time.sleep(debounce_sec)
        # Wait for any active voice turn to finish before kicking the rescan
        # so disk + CPU stay free for the LLM during conversation.
        while bool(g.get("_turn_in_progress")):
            time.sleep(0.5)
        try:
            disc = g.get("_app_discoverer")
            if disc is not None:
                def _gated_rescan():
                    while bool(g.get("_turn_in_progress")):
                        time.sleep(0.5)
                    try:
                        disc.discover_new_since_last(g)
                    except Exception as _re:
                        print(f"[app_watcher] gated rescan error: {_re}")
                threading.Thread(
                    target=_gated_rescan,
                    daemon=True,
                    name="ava-disc-incremental",
                ).start()
        except Exception as e:
            print(f"[app_watcher] discoverer trigger error: {e}")


def _app_install_watcher_thread(g: dict[str, Any]) -> None:
    home = Path(os.path.expanduser("~"))
    paths = [
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        str(home / "Desktop"),
        str(home / "AppData" / "Local"),
    ]
    started = 0
    for p in paths:
        if not Path(p).is_dir():
            continue
        threading.Thread(
            target=_watch_directory, args=(p, g),
            daemon=True, name=f"app-watch-{Path(p).name[:20]}",
        ).start()
        started += 1
    print(f"[app_watcher] launched {started} install watchers")


# ── bootstrap ─────────────────────────────────────────────────────────────────

def bootstrap_background_ticks(g: dict[str, Any]) -> None:
    """Start heartbeat + video capture + signal-driven Win32 watchers."""
    if not g.get("_background_hb_thread_started"):
        threading.Thread(
            target=_heartbeat_loop, args=(g,),
            daemon=True, name="ava-bg-heartbeat",
        ).start()
        g["_background_hb_thread_started"] = True
        print("[background_ticks] heartbeat tick thread started (every 30s)")

    if not g.get("_background_video_thread_started"):
        threading.Thread(
            target=_video_frame_capture_thread, args=(g,),
            daemon=True, name="ava-bg-video-capture",
        ).start()
        g["_background_video_thread_started"] = True
        print("[background_ticks] video frame capture thread started (~15 fps)")

    if not g.get("_background_clipboard_thread_started"):
        threading.Thread(
            target=_clipboard_watcher_thread, args=(g,),
            daemon=True, name="ava-bg-clipboard",
        ).start()
        g["_background_clipboard_thread_started"] = True
        print("[background_ticks] clipboard watcher thread started (event-driven)")

    if not g.get("_background_window_watcher_started"):
        threading.Thread(
            target=_window_watcher_thread, args=(g,),
            daemon=True, name="ava-bg-window-watch",
        ).start()
        g["_background_window_watcher_started"] = True
        print("[background_ticks] window watcher thread started (event-driven)")

    if not g.get("_background_app_watcher_started"):
        threading.Thread(
            target=_app_install_watcher_thread, args=(g,),
            daemon=True, name="ava-bg-app-watch",
        ).start()
        g["_background_app_watcher_started"] = True
        print("[background_ticks] app install watcher thread started (event-driven)")
