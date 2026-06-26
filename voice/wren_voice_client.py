"""wren_voice_client.py — thin JSON-over-TCP client to the Wren voice DAEMON.

The voice MCP tools (wren_voice_mcp.py) use this to talk to the long-running daemon
(wren_voice_daemon.py) instead of doing the work in-process. That's the GOSE pattern
(Zeke 2026-06-14): a thin MCP client → a separate, independently-restartable agent, so
voice fixes reload the DAEMON (no Claude Code relaunch) and the daemon can stream speech.

Protocol: newline-delimited JSON over TCP on 127.0.0.1:WREN_VOICE_DAEMON_PORT (default 8770).
  request : {"cmd": <str>, "args": {...}}\n
  response: {"ok": <bool>, "result": <any>}  or  {"ok": false, "error": <str>}\n

Reconnect: the client reconnects ONCE on a dropped/refused socket (mirrors the GOSE
GoseClient fix, memory 2026-06-05) so a daemon restart doesn't wedge the MCP tools — the
next call transparently reconnects to the fresh daemon.
"""
from __future__ import annotations

import json
import os
import socket
import threading


class VoiceClient:
    def __init__(self, host: str = "127.0.0.1", port: int | None = None,
                 timeout: float = 240.0):
        self.host = host
        self.port = port or int(os.environ.get("WREN_VOICE_DAEMON_PORT", "8770"))
        self.timeout = timeout            # per-call recv timeout: listen/call_start are slow
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()     # one in-flight request at a time per client

    # ── socket lifecycle ──────────────────────────────────────────────────────
    def _connect(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=10.0)
        s.settimeout(self.timeout)
        self._sock = s
        self._buf = b""

    def _reset(self) -> None:
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        self._buf = b""

    # ── one request/response, with a single reconnect retry ──────────────────
    def call(self, cmd: str, **args) -> dict:
        payload = (json.dumps({"cmd": cmd, "args": args}) + "\n").encode("utf-8")
        with self._lock:
            last_err = "unknown"
            for attempt in range(2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall(payload)
                    while b"\n" not in self._buf:
                        chunk = self._sock.recv(65536)
                        if not chunk:
                            raise ConnectionError("daemon closed the connection")
                        self._buf += chunk
                    line, self._buf = self._buf.split(b"\n", 1)
                    return json.loads(line.decode("utf-8", "replace"))
                except (ConnectionError, OSError, socket.timeout) as e:
                    last_err = repr(e)
                    self._reset()  # drop the stale socket; retry reconnects once
                except Exception as e:  # malformed response, etc. — don't loop on it
                    self._reset()
                    return {"ok": False, "error": f"voice client error: {e!r}"}
            return {"ok": False,
                    "error": (f"voice daemon unreachable on {self.host}:{self.port} "
                              f"({last_err}). It's launched by start_wren.bat "
                              f"(py -3.11 experiments\\wren_voice_daemon.py).")}


# Module-level singleton the MCP tools share.
_CLIENT: VoiceClient | None = None


def client() -> VoiceClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = VoiceClient()
    return _CLIENT
