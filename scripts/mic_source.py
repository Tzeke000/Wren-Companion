"""scripts/mic_source.py - stream Zeke's headset mic from the TOWER to my ears on the server.

Zeke 2026-10-07: mouth and ears live on the server; he talks to me through the headset that is
plugged into the tower. The ears (voice/wren_voice_core.py with IRIS_MIC_SOURCE=<tower>:8776,
see voice/net_audio.py) connect here, send a JSON header line, and receive raw float32 mono PCM
from the NAMED mic until they hang up. Several sessions may run at once (capture + barge-in
watch); each opens its own input stream.

Never the default input device: on 2026-07-13 the default was a loopback cable and my ears heard
my own mouth for 90 minutes. No named mic = refuse the session (deaf but honest).

Security: only localhost + the server VM (`iris_home_host` in config/private.local.json) may
connect - this is a live microphone in his room. IRIS_MIC_SOURCE_ALLOW overrides.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _private import priv  # noqa: E402  (git-ignored private config)

PORT = int(os.environ.get("IRIS_MIC_SOURCE_PORT", "8776"))
BIND = os.environ.get("IRIS_MIC_SOURCE_BIND", "0.0.0.0")
_DEFAULT_ALLOW = ",".join(a for a in ("127.0.0.1", priv("iris_home_host")) if a)
ALLOW = {a.strip() for a in os.environ.get("IRIS_MIC_SOURCE_ALLOW", _DEFAULT_ALLOW).split(",") if a.strip()}
MIC_SUBSTR = os.environ.get("WREN_MIC_SUBSTR", "Logitech PRO X")
_LOG_FILE = Path(__file__).resolve().parents[1] / "state" / "mic_source.log"


def log(msg: str) -> None:
    line = f"[mic_source {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def find_named_mic(substr: str):
    """EXACTLY voice/wren_listen.find_input_device: the FIRST input whose name contains substr
    (on Windows that is the MME entry, which resamples - the WASAPI entry rejects 16 kHz:
    'Invalid sample rate', seen 2026-10-07). Never the default device."""
    try:
        sd._terminate(); sd._initialize()   # pick up a headset plugged in after we started
    except Exception:
        pass
    low = substr.lower()
    for i, d in enumerate(sd.query_devices()):
        if low in d.get("name", "").lower() and d.get("max_input_channels", 0) > 0:
            return i
    return None


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
    try:
        hdr = _read_header(conn)
        sr, bs = int(hdr.get("sr", 16000)), int(hdr.get("blocksize", 512))
        idx = find_named_mic(MIC_SUBSTR)
        if idx is None:
            log(f"REFUSED session from {peer}: named mic '{MIC_SUBSTR}' not found (no default-device fallback)")
            return
        log(f"streaming mic idx={idx} '{sd.query_devices(idx)['name']}' to {peer} ({sr} Hz, block {bs})")
        with sd.InputStream(samplerate=sr, channels=1, dtype="float32", device=idx, blocksize=bs) as s:
            while True:
                data, _ov = s.read(bs)
                conn.sendall(data.tobytes())
    except (ConnectionError, OSError) as e:
        log(f"session {peer} ended ({e.__class__.__name__})")
    except Exception as e:
        log(f"session {peer} error: {e!r}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND, PORT))
    srv.listen(8)
    log(f"listening on {BIND}:{PORT}, allow={sorted(ALLOW)}, mic='{MIC_SUBSTR}'")
    while True:
        conn, (ip, port) = srv.accept()
        if ip not in ALLOW:
            log(f"REFUSED {ip}:{port} (not in allowlist)")
            conn.close()
            continue
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=_serve, args=(conn, f"{ip}:{port}"), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
