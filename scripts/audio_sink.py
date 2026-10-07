"""scripts/audio_sink.py - play my voice on the TOWER's speakers when I live on the server.

Zeke 2026-10-07: "move your mouth and ears onto the server ... where it goes to the tower so I
can still hear you." The mouth (voice/wren_styletts_server.py) runs on the server's V100 and,
with IRIS_AUDIO_SINK=<tower>:8775, streams its output blocks here instead of to a local sound
device. This plays them on the tower's current default output.

Protocol (one TCP connection = one mouth session):
    first line : JSON header  {"sr": 24000, "ch": 1, "fmt": "f32le", "from": "iris-mouth"}
    then       : raw little-endian float32 PCM until the socket closes
A new connection preempts the old one. A closed socket stops playback at once (barge-in).

Security: only addresses in IRIS_AUDIO_SINK_ALLOW (default: localhost + the server VM) may
connect (default: localhost + `iris_home_host` from config/private.local.json) - otherwise
anyone on the wifi could make the tower talk in my voice.

usage:  .venv python scripts/audio_sink.py      (env IRIS_AUDIO_SINK_PORT, IRIS_AUDIO_SINK_ALLOW)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

import numpy as np
import sounddevice as sd

PORT = int(os.environ.get("IRIS_AUDIO_SINK_PORT", "8775"))
BIND = os.environ.get("IRIS_AUDIO_SINK_BIND", "0.0.0.0")
from _private import priv  # config/private.local.json (git-ignored): network addresses never in tracked files
_DEFAULT_ALLOW = ",".join(a for a in ("127.0.0.1", priv("iris_home_host")) if a)
ALLOW = {a.strip() for a in os.environ.get("IRIS_AUDIO_SINK_ALLOW", _DEFAULT_ALLOW).split(",") if a.strip()}
BLOCK_BYTES = 4096  # 1024 float32 samples ~ 43 ms at 24 kHz

_current: "socket.socket | None" = None
_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[audio_sink {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _read_header(conn: socket.socket) -> dict:
    buf = b""
    while not buf.endswith(b"\n"):
        ch = conn.recv(1)
        if not ch:
            raise ConnectionError("closed before header")
        buf += ch
        if len(buf) > 4096:
            raise ValueError("header too long")
    return json.loads(buf.decode("utf-8"))


def _serve(conn: socket.socket, peer: str) -> None:
    global _current
    stream = None
    try:
        hdr = _read_header(conn)
        sr, ch = int(hdr.get("sr", 24000)), int(hdr.get("ch", 1))
        if hdr.get("fmt", "f32le") != "f32le":
            raise ValueError(f"unsupported fmt {hdr.get('fmt')!r}")
        # follow the OS default output (Zeke switches between headset and speakers)
        try:
            sd._terminate(); sd._initialize()
        except Exception:
            pass
        stream = sd.OutputStream(samplerate=sr, channels=ch, dtype="float32")
        stream.start()
        log(f"playing from {peer} ({hdr.get('from', '?')}, {sr} Hz) on '{sd.query_devices(kind='output').get('name')}'")
        pending = b""
        while True:
            data = conn.recv(BLOCK_BYTES)
            if not data:
                break
            pending += data
            n = (len(pending) // (4 * ch)) * (4 * ch)
            if n:
                stream.write(np.frombuffer(pending[:n], dtype="<f4").reshape(-1, ch))
                pending = pending[n:]
        log(f"session from {peer} ended")
    except Exception as e:
        log(f"session from {peer} error: {e!r}")
    finally:
        if stream is not None:
            try:
                stream.abort(); stream.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
        with _lock:
            if _current is conn:
                _current = None


def main() -> int:
    global _current
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # small receive buffer on the LISTENING socket so accepted connections inherit it before the
    # window is negotiated - keeps the mouth <~0.5 s ahead of what is actually playing
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
    srv.bind((BIND, PORT))
    srv.listen(4)
    log(f"listening on {BIND}:{PORT}, allow={sorted(ALLOW)}")
    while True:
        conn, (ip, port) = srv.accept()
        if ip not in ALLOW:
            log(f"REFUSED {ip}:{port} (not in IRIS_AUDIO_SINK_ALLOW)")
            conn.close()
            continue
        with _lock:
            old, _current = _current, conn
        if old is not None:
            log("new session preempts the old one")
            try:
                old.close()
            except Exception:
                pass
        threading.Thread(target=_serve, args=(conn, f"{ip}:{port}"), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
