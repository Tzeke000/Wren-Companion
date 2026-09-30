"""orb_http_bounce_tool — FORCE-bounce the in-process orb HTTP server (:5876) without a stack restart.

NOT the same as `restart_orb_http` (tools/system/runtime_repair_tool.py, 2026-07-17): that one is a
no-op whenever something answers the port and only re-runs start() after the thread has DIED.
This one handles the other shape — thread ALIVE, server wedged or its listener gone — by setting
uvicorn should_exit/force_exit on the live Server object (found via gc), joining the thread, and
starting fresh. Use `restart_orb_http` first when the thread is dead; use this when it is not.

SELF_ASSESSMENT: Tier 2 (mutates one in-process server; never touches other processes).

Born 2026-09-17 ~13:1x. Context: at 06:53 that morning the :5876 listener vanished
while the `iris-orb-http` thread was still alive, the pid lockfile was correct and the
eyes were fine. The only recovery available was a full stack restart, which (with two
watchdogs racing) cost a double launch, three MCP attach tries and a body-less CLI twin.
`scripts/body_switch.ps1` line 86 even *told* the operator to run
`iris_tool_call restart_orb_http` — which exists but is a no-op while anything answers the port,
and cannot end a live-but-broken server. This tool can.

Cause of the loss is still UNKNOWN (uvicorn 0.46 in a non-main thread has no signal
path; nothing in-repo sets `should_exit`). So this tool also installs a file handler on
the `uvicorn.error` logger the first time it is called, writing WARNING+ lines to
`state/orb_http.log` — the runtime's stderr goes into the SDK void, so the next loss
would otherwise leave no trace again.

Actions (params["action"]):
  status  (default) — thread alive? port answering? uvicorn Server object(s) found via gc,
                      their should_exit / connections; lockfile; installs the log handler.
  restart           — if the server answers HTTP and params["force"] is not true, REFUSE
                      (healthy). Otherwise: should_exit=True (+force_exit so shutdown does
                      not wait on the orb's keep-alive connections), join the thread
                      (≤ wait_s, default 15), clear our own pid lockfile, call
                      brain.orb_http.start() again with the module's stored _g/_root/_tts,
                      then probe the port for up to 10 s. Returns before/after facts.

Everything it does is logged to state/orb_http_restart.log.
"""
from __future__ import annotations

import datetime as _dt
import gc
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

_HOST = "127.0.0.1"
_PORT = 5876
_THREAD = "iris-orb-http"
_PROBE_URL = f"http://{_HOST}:{_PORT}/api/v1/voice_input"  # tiny JSON endpoint


def _root() -> Path:
    try:
        import brain.orb_http as oh  # noqa
        if getattr(oh, "_root", None):
            return Path(oh._root)
    except Exception:
        pass
    return Path(__file__).resolve().parents[2]


def _log(msg: str) -> None:
    try:
        p = _root() / "state" / "orb_http_restart.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _install_uvicorn_file_log() -> str:
    """Idempotent: one FileHandler on uvicorn.error / uvicorn (WARNING+) → state/orb_http.log."""
    try:
        path = _root() / "state" / "orb_http.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = "iris-orb-http-filelog"
        # 09-18 21:53: the listener vanished with uvicorn still reporting
        # servers_open=[True] and this log EMPTY - a dead accept loop is
        # reported by the asyncio logger (ERROR), not by uvicorn. Capture it.
        for name in ("uvicorn.error", "uvicorn", "asyncio"):
            lg = logging.getLogger(name)
            if any(getattr(h, "_iris_marker", "") == marker for h in lg.handlers):
                continue
            h = logging.FileHandler(str(path), encoding="utf-8")
            h.setLevel(logging.WARNING)
            h.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s"))
            h._iris_marker = marker  # type: ignore[attr-defined]
            lg.addHandler(h)
            if lg.level == logging.NOTSET or lg.level > logging.WARNING:
                lg.setLevel(logging.WARNING)
        return str(path)
    except Exception as e:
        return f"install failed: {e!r}"


def _thread() -> threading.Thread | None:
    for t in threading.enumerate():
        if t.name == _THREAD:
            return t
    return None


def _port_bound() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((_HOST, _PORT))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _http_ok(timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(_PROBE_URL, timeout=timeout) as r:  # noqa: S310
            return (r.status == 200), f"http {r.status}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:120]


def _servers() -> list[Any]:
    try:
        import uvicorn
    except Exception:
        return []
    out = []
    for o in gc.get_objects():
        try:
            if isinstance(o, uvicorn.Server) and getattr(o.config, "port", None) == _PORT:
                out.append(o)
        except Exception:
            continue
    return out


def _describe(srv: Any) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k in ("should_exit", "force_exit", "started"):
        d[k] = bool(getattr(srv, k, False))
    try:
        st = getattr(srv, "server_state", None)
        d["connections"] = len(getattr(st, "connections", []) or [])
        d["tasks"] = len(getattr(st, "tasks", []) or [])
    except Exception:
        pass
    try:
        d["servers_open"] = [bool(getattr(s, "sockets", None)) for s in (getattr(srv, "servers", []) or [])]
    except Exception:
        pass
    return d


def _snapshot() -> dict[str, Any]:
    t = _thread()
    ok, detail = _http_ok()
    lock = _root() / "state" / "iris_orb_http.pid"
    try:
        lock_pid = int(lock.read_text(encoding="utf-8").strip()) if lock.is_file() else None
    except Exception:
        lock_pid = -1
    servers = _servers()
    return {
        "thread_alive": bool(t and t.is_alive()),
        "port_bound": _port_bound(),
        "http_ok": ok,
        "http_detail": detail,
        "lockfile_pid": lock_pid,
        "our_pid": os.getpid(),
        "uvicorn_servers": [_describe(s) for s in servers],
        "ts": time.time(),
    }


def _restart_orb_http_fn(params: dict[str, Any] | None, g: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    action = str(params.get("action", "status")).lower()
    logpath = _install_uvicorn_file_log()
    before = _snapshot()
    out: dict[str, Any] = {"ok": True, "action": action, "uvicorn_log": logpath, "before": before}
    if action == "status":
        return out
    if action != "restart":
        return {"ok": False, "error": f"unknown action {action!r} (status|restart)"}

    force = bool(params.get("force", False))
    wait_s = float(params.get("wait_s", 15))
    if before["http_ok"] and not force:
        out.update(ok=False, refused=True,
                   reason="server answers HTTP — healthy; pass force=true to bounce it anyway")
        _log(f"restart REFUSED (healthy): {json.dumps(before)}")
        return out

    _log(f"restart BEGIN force={force}: {json.dumps(before)}")
    steps: list[str] = []
    servers = _servers()
    for srv in servers:
        try:
            srv.should_exit = True
            srv.force_exit = True  # don't wait on the orb's 1Hz keep-alive connections
            steps.append("should_exit+force_exit set")
        except Exception as e:
            steps.append(f"set flags failed: {e!r}")
    t = _thread()
    if t and t.is_alive():
        t.join(timeout=wait_s)
        steps.append(f"thread joined alive={t.is_alive()}")
    else:
        steps.append("no live thread to join")

    # If the thread is still alive after the wait, the loop is wedged — say so, don't stack a second server.
    if t and t.is_alive():
        out.update(ok=False, steps=steps, after=_snapshot(),
                   error=f"old server thread still alive after {wait_s}s — event loop wedged; "
                         "a second bind would fail the instance check. Stack restart needed.")
        _log(f"restart FAILED (thread wedged): {json.dumps(out['after'])}")
        return out

    # Our own stale lockfile would pass start()'s check anyway (same pid), but clear it for hygiene.
    lock = _root() / "state" / "iris_orb_http.pid"
    try:
        if lock.is_file() and int(lock.read_text(encoding="utf-8").strip()) == os.getpid():
            lock.unlink()
            steps.append("own lockfile cleared")
    except Exception as e:
        steps.append(f"lockfile clear skipped: {e!r}")

    # Give the OS a beat to release the socket, then start again with the stored context.
    deadline = time.time() + 5.0
    while _port_bound() and time.time() < deadline:
        time.sleep(0.25)
    try:
        import brain.orb_http as oh
        oh.start(oh._g, oh._root, oh._tts_ref)
        steps.append("orb_http.start() called")
    except Exception as e:
        out.update(ok=False, steps=steps, after=_snapshot(), error=f"start() raised: {e!r}")
        _log(f"restart FAILED (start raised): {e!r}")
        return out

    deadline = time.time() + 10.0
    ok, detail = False, ""
    while time.time() < deadline:
        ok, detail = _http_ok(timeout=2.0)
        if ok:
            break
        time.sleep(0.5)
    after = _snapshot()
    out.update(ok=bool(ok), steps=steps, after=after)
    if not ok:
        out["error"] = f"server did not answer within 10s after start(): {detail}"
    _log(f"restart {'OK' if ok else 'FAILED'}: {json.dumps(after)}")
    return out


try:
    from tools.tool_registry import register_tool
    register_tool(
        "orb_http_bounce",
        "FORCE-bounce the in-process orb HTTP server on :5876 without a stack restart (thread alive but wedged/listener gone — the case restart_orb_http cannot handle). "
        "action=status (default): thread/port/HTTP/lockfile + uvicorn Server state via gc, "
        "and installs a WARNING+ file log for uvicorn at state/orb_http.log. "
        "action=restart: refuses if the server answers HTTP unless force=true; otherwise "
        "should_exit+force_exit → join thread → clear own lockfile → orb_http.start() → probe. "
        "Built 2026-09-17 after the 06:53 lost-listener incident (cause still unknown).",
        2,
        _restart_orb_http_fn,
    )
except Exception:
    pass
