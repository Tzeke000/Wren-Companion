"""pc — direct control of Zeke's PC without screenshot-and-click (Zeke 2026-10-07, "#2": faster,
surer PC control). Talks to Windows itself instead of steering the mouse:

  volume  : master get/set/mute (Core Audio via pycaw) + PER-APP volume/mute (Spotify, Discord,
            a game, Chrome...) and a list of what is making sound right now
  media   : play_pause / next / prev / stop  (system media keys — Spotify, browsers, players)
  now_playing : what Spotify is playing (from its window title; "Spotify" alone = paused)
  window  : focus / minimize / maximize / restore / close  by title substring; list visible windows
  launch  : start a program / file / URL (os.startfile)

Every action returns what it observed AFTER acting (volume read back, window state read back), so a
success dict is evidence, not a wish. ⚠ He games full-screen: never fire focus/window actions at him
mid-game unasked — focus changes knock a game out of fullscreen.

params: action = volume_get | volume_set | mute | unmute | app_volume | sessions | media | now_playing |
        windows | window | launch   (+ the per-action args documented in register_tool below)
"""
from __future__ import annotations

import ctypes
import os
import time
from typing import Any

from tools.tool_registry import register_tool

user32 = ctypes.windll.user32
VK = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
      "vol_up": 0xAF, "vol_down": 0xAE, "vol_mute": 0xAD}


def _com():
    import comtypes
    try:
        comtypes.CoInitialize()
    except OSError:
        pass


def _master():
    _com()
    from pycaw.pycaw import AudioUtilities
    dev = AudioUtilities.GetSpeakers()
    return dev.EndpointVolume, getattr(dev, "FriendlyName", "default output")


def _sessions():
    _com()
    from pycaw.pycaw import AudioUtilities
    out = []
    for s in AudioUtilities.GetAllSessions():
        name = s.Process.name() if s.Process else "System sounds"
        vol = s.SimpleAudioVolume
        out.append((s, name, vol))
    return out


def _vol_state() -> dict:
    ep, name = _master()
    return {"device": name, "volume_pct": round(ep.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(ep.GetMute())}


def _windows() -> list[dict]:
    import win32gui
    wins = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h):
            t = win32gui.GetWindowText(h)
            if t.strip():
                pl = win32gui.GetWindowPlacement(h)
                wins.append({"hwnd": h, "title": t,
                             "state": {1: "normal", 2: "minimized", 3: "maximized"}.get(pl[1], str(pl[1]))})
        return True
    win32gui.EnumWindows(cb, None)
    return wins


def _find(title: str) -> dict | None:
    t = title.lower()
    ws = [w for w in _windows() if t in w["title"].lower()]
    ws.sort(key=lambda w: (w["title"].lower() != t, len(w["title"])))
    return ws[0] if ws else None


def _tool_pc(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    a = str(params.get("action") or "").lower()
    try:
        if a == "volume_get":
            return {"ok": True, **_vol_state()}
        if a == "volume_set":
            pct = max(0, min(100, float(params.get("percent"))))
            ep, _ = _master()
            ep.SetMasterVolumeLevelScalar(pct / 100.0, None)
            return {"ok": True, **_vol_state()}
        if a in ("mute", "unmute"):
            ep, _ = _master()
            ep.SetMute(1 if a == "mute" else 0, None)
            return {"ok": True, **_vol_state()}
        if a == "sessions":
            return {"ok": True, "sessions": [{"app": n, "volume_pct": round(v.GetMasterVolume() * 100),
                                              "muted": bool(v.GetMute())} for _, n, v in _sessions()]}
        if a == "app_volume":
            app = str(params.get("app") or "").lower().removesuffix(".exe")
            hits = [(n, v) for _, n, v in _sessions() if app and app in n.lower()]
            if not hits:
                return {"ok": False, "error": f"no audio session matching '{app}' (is it playing/open?)",
                        "sessions": [n for _, n, _ in _sessions()]}
            for _, v in hits:
                if params.get("percent") is not None:
                    v.SetMasterVolume(max(0.0, min(1.0, float(params["percent"]) / 100.0)), None)
                if params.get("mute") is not None:
                    v.SetMute(1 if params["mute"] in (True, "true", 1, "1") else 0, None)
            return {"ok": True, "apps": [{"app": n, "volume_pct": round(v.GetMasterVolume() * 100),
                                          "muted": bool(v.GetMute())} for n, v in hits]}
        if a == "media":
            key = str(params.get("key") or "play_pause").lower()
            if key not in ("play_pause", "next", "prev", "stop"):
                return {"ok": False, "error": "key must be play_pause|next|prev|stop"}
            app = str(params.get("app") or "").strip() or None
            via = "smtc"
            try:
                r = _smtc_control(key, app)
            except Exception as e:  # noqa: BLE001 — no winrt / no session: fall back to the global media key
                r = {"ok": False, "error": repr(e)[:120]}
            if not r.get("ok") and not app:
                user32.keybd_event(VK[key], 0, 0, 0)
                user32.keybd_event(VK[key], 0, 2, 0)
                via, r = "media_key", {"ok": True}
            time.sleep(0.6)
            return {**r, "sent": key, "via": via, "now_playing": _now_playing()}
        if a == "now_playing":
            return {"ok": True, "now_playing": _now_playing()}
        if a == "windows":
            return {"ok": True, "windows": [{"title": w["title"][:90], "state": w["state"]} for w in _windows()]}
        if a == "window":
            import win32con
            import win32gui
            w = _find(str(params.get("title") or ""))
            if not w:
                return {"ok": False, "error": f"no visible window matching '{params.get('title')}'"}
            op = str(params.get("op") or "focus").lower()
            h = w["hwnd"]
            if op == "focus":
                win32gui.ShowWindow(h, win32con.SW_RESTORE if w["state"] == "minimized" else win32con.SW_SHOW)
                user32.keybd_event(0x12, 0, 0, 0)          # ALT tap: lets SetForegroundWindow through
                user32.keybd_event(0x12, 0, 2, 0)
                win32gui.SetForegroundWindow(h)
            elif op in ("minimize", "maximize", "restore"):
                win32gui.ShowWindow(h, {"minimize": win32con.SW_MINIMIZE, "maximize": win32con.SW_MAXIMIZE,
                                        "restore": win32con.SW_RESTORE}[op])
            elif op == "close":
                win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
            else:
                return {"ok": False, "error": "op must be focus|minimize|maximize|restore|close"}
            time.sleep(0.4)
            after = _find(w["title"])
            fg = win32gui.GetWindowText(win32gui.GetForegroundWindow())
            return {"ok": True, "window": w["title"][:90], "op": op,
                    "state_after": after["state"] if after else "gone", "foreground_now": fg[:90]}
        if a == "launch":
            target = str(params.get("target") or "").strip()
            if not target:
                return {"ok": False, "error": "target required (program path, file, or URL)"}
            os.startfile(target)
            return {"ok": True, "launched": target}
        return {"ok": False, "error": "unknown action", "actions": [
            "volume_get", "volume_set", "mute", "unmute", "sessions", "app_volume", "media",
            "now_playing", "windows", "window", "launch"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:240], "action": a}


def _now_playing() -> dict:
    try:
        sess = _smtc_sessions()
        if sess:
            cur = next((x for x in sess if x.get("status") == "playing"), None) or                 next((x for x in sess if x.get("current")), sess[0])
            return {"source": "smtc", "app": cur.get("app"), "title": cur.get("title"), "artist": cur.get("artist"),
                    "status": cur.get("status"), "track": cur.get("title"), "sessions": sess,
                    "spotify": "playing" if "spotify" in (cur.get("app") or "").lower() and cur.get("status") == "playing" else None}
    except Exception:
        pass
    return _now_playing_spotify_title()


def _now_playing_spotify_title() -> dict:
    try:
        import psutil
        import win32gui
        import win32process
        pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "spotify.exe"}
        if not pids:
            return {"spotify": "not running"}
        titles = []

        def cb(h, _):
            if win32gui.IsWindowVisible(h) or True:
                try:
                    if win32process.GetWindowThreadProcessId(h)[1] in pids:
                        t = win32gui.GetWindowText(h)
                        if t and t not in ("Default IME", "MSCTFIME UI"):
                            titles.append(t)
                except Exception:
                    pass
            return True
        win32gui.EnumWindows(cb, None)
        t = next((x for x in titles if " - " in x), None)
        if t:
            artist, _, song = t.partition(" - ")
            return {"spotify": "playing", "artist": artist, "track": song}
        return {"spotify": "paused or idle", "window": titles[:1]}
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)[:120]}


# ── Windows media sessions (SMTC) — what ANY player is playing (Spotify, YouTube in Chrome, VLC…), and
#    control of a SPECIFIC app's session. (Research 2026-10-07: borrowed from SecretiveShell/mcp-windows.)
_STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}


def _run_async(coro_fn, timeout: float = 8.0):
    """Run a winrt coroutine on its OWN loop in its own thread — safe even when called from inside a
    running event loop (the app's async routes)."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_fn())).result(timeout=timeout)


def _smtc_sessions() -> list[dict]:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M

    async def go():
        mgr = await M.request_async()
        cur = mgr.get_current_session()
        cur_id = cur.source_app_user_model_id if cur else None
        ss = mgr.get_sessions()
        out = []
        for i in range(ss.size):
            s = ss.get_at(i)
            row = {"app": s.source_app_user_model_id, "current": s.source_app_user_model_id == cur_id}
            try:
                pr = await s.try_get_media_properties_async()
                row.update(title=pr.title, artist=pr.artist, album=pr.album_title)
            except Exception:
                pass
            try:
                row["status"] = _STATUS.get(int(s.get_playback_info().playback_status), "unknown")
            except Exception:
                row["status"] = "unknown"
            out.append(row)
        return out
    return _run_async(go)


def _smtc_control(key: str, app: str | None) -> dict:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M

    async def go():
        mgr = await M.request_async()
        target = None
        if app:
            ss = mgr.get_sessions()
            for i in range(ss.size):
                s = ss.get_at(i)
                if app.lower() in (s.source_app_user_model_id or "").lower():
                    target = s
                    break
        else:
            target = mgr.get_current_session()
        if target is None:
            return {"ok": False, "error": f"no media session{' for ' + app if app else ''}"}
        fn = {"play_pause": target.try_toggle_play_pause_async, "next": target.try_skip_next_async,
              "prev": target.try_skip_previous_async, "stop": target.try_stop_async}[key]
        ok = await fn()
        return {"ok": bool(ok), "app": target.source_app_user_model_id}
    return _run_async(go)


register_tool(
    "pc",
    "Direct PC control (no screenshots/clicks). action=volume_get | volume_set percent=0-100 | mute | "
    "unmute | sessions (apps making sound + their volume) | app_volume app='spotify' percent=30 "
    "[mute=true|false] | media key=play_pause|next|prev|stop [app='chrome'] (Windows media sessions; global "
    "media key fallback) | now_playing (any player: title/artist/status) | windows | "
    "window title='discord' op=focus|minimize|maximize|restore|close | launch target='<path|file|url>'. "
    "Reads state back after acting. Don't focus/close windows on him mid-game unasked.",
    2,
    _tool_pc,
)
