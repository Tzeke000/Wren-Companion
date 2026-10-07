"""voice/net_audio.py - my ears' microphone over the network (2026-10-07, the server port).

Zeke 2026-10-07: mouth AND ears run on the server's V100; he talks to me through his headset,
which is plugged into the TOWER. scripts/mic_source.py on the tower captures that named mic
and streams it here. With IRIS_MIC_SOURCE="host:port" set, wren_voice_core opens a
NetInputStream wherever it used to open sounddevice.InputStream. Unset = local mic as before.

Protocol (one TCP connection = one capture session, mirror of scripts/audio_sink.py):
    client -> server : JSON header line {"sr": 16000, "ch": 1, "fmt": "f32le", "blocksize": N}
    server -> client : raw little-endian float32 mono PCM, continuously, until either side closes
"""
from __future__ import annotations

import json
import os
import socket

import numpy as np

MIC_SOURCE = os.environ.get("IRIS_MIC_SOURCE", "").strip()
NET_MIC = "net"   # sentinel device index: "the mic is the network source"


class NetInputStream:
    """Duck-types the parts of sounddevice.InputStream that wren_voice_core uses:
    context manager + read(frames) -> (ndarray[frames, 1] float32, overflowed)."""

    def __init__(self, target: str, samplerate: int, blocksize: int):
        host, _, port = target.rpartition(":")
        self._addr = (host or "127.0.0.1", int(port))
        self._sr = int(samplerate)
        self._bs = int(blocksize)
        self._sock: "socket.socket | None" = None

    def __enter__(self) -> "NetInputStream":
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(5.0)
        s.connect(self._addr)
        s.settimeout(10.0)   # a live mic sends ~64 KB/s; 10 s of nothing = the source is gone
        hdr = {"sr": self._sr, "ch": 1, "fmt": "f32le", "blocksize": self._bs, "from": "iris-ears"}
        s.sendall((json.dumps(hdr) + "\n").encode("utf-8"))
        self._sock = s
        return self

    def read(self, frames: int):
        need = int(frames) * 4
        buf = bytearray()
        while len(buf) < need:
            chunk = self._sock.recv(need - len(buf))
            if not chunk:
                raise ConnectionError("mic source closed the stream")
            buf += chunk
        return np.frombuffer(bytes(buf), dtype="<f4").reshape(-1, 1), False

    def close(self) -> None:
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


def open_mic(sd, samplerate: int, dev_idx, blocksize: int):
    """sounddevice.InputStream(...) or, when the mic is the network source, a NetInputStream."""
    if dev_idx == NET_MIC or MIC_SOURCE:
        if not MIC_SOURCE:
            raise RuntimeError("dev_idx is the network mic but IRIS_MIC_SOURCE is not set")
        return NetInputStream(MIC_SOURCE, samplerate, blocksize)
    return sd.InputStream(samplerate=samplerate, channels=1, dtype="float32",
                          device=dev_idx, blocksize=blocksize)
