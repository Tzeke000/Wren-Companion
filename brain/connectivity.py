"""
Brain connectivity monitor — checks internet reachability three ways,
caches result 30s, emits state-change events into globals.

Wire: startup.py initialises instance, heartbeat polls passively,
reply_engine reads _connectivity_changed flag each turn.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Optional


_CONNECTIVITY_LOG = "state/connectivity_log.jsonl"

# ─── fast public targets (no DNS needed) ────────────────────────────────────
_CHECK_URLS = (
    "https://1.1.1.1",
    "https://8.8.8.8",
)
_SOCKET_HOST = ("1.1.1.1", 443)
_CACHE_TTL = 30.0          # seconds to reuse last check result
_QUALITY_CACHE_TTL = 60.0  # seconds
_CLOUD_CHECK_TTL = 60.0    # seconds

_SINGLETON: Optional["ConnectivityMonitor"] = None


class ConnectivityMonitor:
    def __init__(self, g: Optional[dict[str, Any]] = None):
        self._g = g or {}
        self._lock = threading.Lock()
        self._online: Optional[bool] = None
        self._quality: str = "offline"
        self._cloud_ok: Optional[bool] = None
        self._last_check: float = 0.0
        self._last_quality_check: float = 0.0
        self._last_cloud_check: float = 0.0
        self._thread: Optional[threading.Thread] = None

    # ── public API ───────────────────────────────────────────────────────────

    def is_online(self) -> bool:
        now = time.time()
        with self._lock:
            if self._online is not None and (now - self._last_check) < _CACHE_TTL:
                return self._online
        result = self._check_connectivity_raw()
        with self._lock:
            prev = self._online
            self._online = result
            self._last_check = time.time()
            changed = prev is not None and prev != result
        if changed:
            self._on_state_change(result)
        return result

    def check_ollama_cloud(self) -> bool:
        """Verify cloud routing works by calling a minimal probe via Ollama."""
        now = time.time()
        with self._lock:
            if self._cloud_ok is not None and (now - self._last_cloud_check) < _CLOUD_CHECK_TTL:
                return bool(self._cloud_ok)
        result = self._probe_cloud()
        with self._lock:
            self._cloud_ok = result
            self._last_cloud_check = time.time()
        return result

    def get_connection_quality(self) -> str:
        """'offline' | 'online_slow' | 'online_fast' based on RTT to 1.1.1.1."""
        now = time.time()
        with self._lock:
            if (now - self._last_quality_check) < _QUALITY_CACHE_TTL:
                return self._quality
        q = self._measure_quality()
        with self._lock:
            self._quality = q
            self._last_quality_check = time.time()
        return q

    def start_monitor_loop(self) -> None:
        """Background thread polling every 60s."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="ava-connectivity-monitor",
        )
        self._thread.start()

    # ── internals ────────────────────────────────────────────────────────────

    def _check_connectivity_raw(self) -> bool:
        """Returns True if ANY of the three checks succeed."""
        import urllib.request, urllib.error
        for url in _CHECK_URLS:
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=2.0):
                    return True
            except Exception:
                pass
        # Socket fallback
        try:
            with socket.create_connection(_SOCKET_HOST, timeout=2.0):
                return True
        except Exception:
            pass
        return False

    def _measure_quality(self) -> str:
        try:
            t0 = time.monotonic()
            with socket.create_connection(_SOCKET_HOST, timeout=2.0):
                ms = (time.monotonic() - t0) * 1000
            if ms < 100:
                return "online_fast"
            return "online_slow"
        except Exception:
            return "offline"

    def _probe_cloud(self) -> bool:
        """Try ollama list or a tiny HTTP probe to see if cloud routing exists."""
        if not self._check_connectivity_raw():
            return False
        try:
            import urllib.request
            # Cloud-routing Ollama models appear in /api/tags when connected
            import os
            base = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
            req = urllib.request.Request(f"{base}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            models = data.get("models") if isinstance(data, dict) else []
            if isinstance(models, list):
                for m in models:
                    if isinstance(m, dict):
                        name = str(m.get("name") or "")
                        if ":cloud" in name:
                            return True
            return False
        except Exception:
            return False

    def _on_state_change(self, now_online: bool) -> None:
        g = self._g
        status = "online" if now_online else "offline"
        g["_is_online"] = now_online
        g["_connection_quality"] = self.get_connection_quality() if now_online else "offline"
        g["_ollama_cloud_reachable"] = self.check_ollama_cloud() if now_online else False
        g["_connectivity_changed"] = True
        g["_connectivity_changed_to"] = status
        # Log
        self._log_change(status, g)
        print(f"[connectivity] state_change → {status}")

    def _log_change(self, status: str, g: dict[str, Any]) -> None:
        try:
            base = Path(g.get("BASE_DIR") or ".")
            path = base / _CONNECTIVITY_LOG
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {"ts": time.time(), "status": status, "quality": g.get("_connection_quality")}
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(60.0)
            try:
                self.is_online()
            except Exception:
                pass


# ── module helpers ────────────────────────────────────────────────────────────

def get_monitor(g: Optional[dict[str, Any]] = None) -> ConnectivityMonitor:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = ConnectivityMonitor(g or {})
    elif g is not None:
        _SINGLETON._g = g
    return _SINGLETON


def bootstrap_connectivity(g: dict[str, Any]) -> None:
    """Called from startup.py. Runs first check, starts monitor loop."""
    mon = get_monitor(g)
    # Immediate check (sets _is_online etc.)
    online = mon.is_online()
    quality = mon.get_connection_quality()
    cloud = mon.check_ollama_cloud() if online else False
    g["_connectivity_monitor"] = mon
    g["_is_online"] = online
    g["_connection_quality"] = quality
    g["_ollama_cloud_reachable"] = cloud
    g["_connectivity_changed"] = False
    print(f"[connectivity] online={online} quality={quality} cloud_reachable={cloud}")
    mon.start_monitor_loop()
