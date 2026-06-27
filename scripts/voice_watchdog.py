"""voice_watchdog.py — keeps Iris's voice services alive (self-healing).

Checks the StyleTTS2 MOUTH (:8769) and the voice DAEMON (:8770) on a cycle and
relaunches either if it dies. Born 2026-06-26 after the dead-mouth silent-failure:
the mouth had been launched with a `timeout` and died, the daemon's playback
swallowed the dead-mouth POST error, and voice_speak reported "[voice_speak]
spoken" with no audio. Persistent launch isn't enough — a crash still leaves the
voice mute. This makes it self-healing: a dead mouth/daemon comes back within a
cycle, so the voice can't go silently dark.

Run persistently (in the main .venv):
    .venv\\Scripts\\python.exe scripts\\voice_watchdog.py
"""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\Wren-Companion")
VOICE = ROOT / "voice"
STYLE_PY = VOICE / "style-venv" / "Scripts" / "python.exe"   # mouth venv (StyleTTS2)
MAIN_PY = ROOT / ".venv" / "Scripts" / "python.exe"          # daemon venv (whisper/silero)
MOUTH_SCRIPT = VOICE / "wren_styletts_server.py"
DAEMON_SCRIPT = VOICE / "wren_voice_daemon.py"

MOUTH_PORT = 8769
DAEMON_PORT = 8770
CHECK_EVERY = 15.0       # seconds between health checks
WARM_GRACE = 75.0        # after a (re)launch with the port still DOWN, don't relaunch
                         # again for this long (spacing for a genuinely dead service)
MOUTH_STUCK_AFTER = 300.0  # port UP but /health != ok this long → the mouth is stuck
                           # (not just warming) → evict + relaunch. Generous on purpose:
                           # the mouth now binds the port immediately and loads the model
                           # in the background, so legit warmup keeps the port up the
                           # whole time; only a hung load should ever cross 5 minutes.

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — child survives the watchdog and isn't
# tied to its console, so a watchdog restart doesn't take the voice services with it.
_DETACHED = 0x00000008 | 0x00000200


def _log(msg: str) -> None:
    print(f"[voice-watchdog {time.strftime('%H:%M:%S')}] {msg}", flush=True)


_SINGLETON_MUTEX = None  # module-global: holds the named-mutex handle for the process lifetime


def _acquire_singleton() -> bool:
    """Refuse to start if another voice_watchdog already runs (CLAUDE.md §8 pattern).

    Uses a Windows named mutex via ctypes (no pywin32 dependency). The mutex is
    released automatically when the holding process dies, so there is no stale-lock
    problem the way a PID file would have. Returns True if WE acquired it (this is
    the sole watchdog), False if another instance already holds it.

    Born 2026-06-26: start_iris.bat launches this unconditionally on every boot, so a
    restart that re-runs the bat while the prior watchdog still lived produced TWO
    watchdogs fighting over the mouth/daemon ports — the double-spawn that doubled
    Iris's voice. This guard makes the second instance exit cleanly instead.

    Fails OPEN: if the mutex can't be created at all, we run anyway — a possible
    double is no-worse-than-before, but no watchdog at all would leave the voice
    unguarded.
    """
    global _SINGLETON_MUTEX
    ERROR_ALREADY_EXISTS = 183
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "IrisVoiceWatchdog")
        if not handle:
            _log("singleton: CreateMutexW returned NULL; proceeding without guard")
            return True
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        _SINGLETON_MUTEX = handle  # keep the handle alive for the process lifetime
        return True
    except Exception as e:  # noqa: BLE001 — fail open, never block the voice on a guard bug
        _log(f"singleton: guard errored ({e!r}); proceeding without guard")
        return True


def _mouth_ok() -> bool:
    """True only if the mouth answers /health == ok — server-up AND model-warm."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{MOUTH_PORT}/health", timeout=3) as r:
            return r.read().decode("utf-8", "replace").strip() == "ok"
    except Exception:
        return False


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except Exception:
        return False


def _kill_on_port(port: int) -> None:
    """Evict whatever process is listening on `port` (Windows, dependency-free).

    Used only to clear a STUCK mouth (port up but never /health-ok) so a fresh one
    can bind — the mouth's allow_reuse_address=False singleton means a relaunch can't
    take the port from a stuck holder, so we must remove the holder first. Best-effort;
    a failure just means the next cycle tries again. CREATE_NO_WINDOW so no console flashes.
    """
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue "
             f"| ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"],
            timeout=15, creationflags=0x08000000)
        _log(f"evicted stuck process holding :{port}")
    except Exception as e:  # noqa: BLE001 — best-effort; never crash the watchdog
        _log(f"kill-on-port {port} failed ({e!r}); will retry next cycle")


def _launch_mouth() -> None:
    env = dict(os.environ)
    env["WREN_VOICE_PORT"] = str(MOUTH_PORT)
    log = open(VOICE / "mouth_live.log", "ab")
    subprocess.Popen([str(STYLE_PY), str(MOUTH_SCRIPT)], cwd=str(VOICE), env=env,
                     stdout=log, stderr=log, creationflags=_DETACHED, close_fds=True)
    _log(f"relaunched MOUTH ({STYLE_PY.name} {MOUTH_SCRIPT.name})")


def _launch_daemon() -> None:
    env = dict(os.environ)
    env["WREN_VOICE_PORT"] = str(MOUTH_PORT)
    env["WREN_VOICE_DAEMON_PORT"] = str(DAEMON_PORT)
    log = open(VOICE / "daemon.log", "ab")
    subprocess.Popen([str(MAIN_PY), str(DAEMON_SCRIPT)], cwd=str(VOICE), env=env,
                     stdout=log, stderr=log, creationflags=_DETACHED, close_fds=True)
    _log(f"relaunched DAEMON ({MAIN_PY.name} {DAEMON_SCRIPT.name})")


def main() -> None:
    _log(f"watchdog up — mouth :{MOUTH_PORT}, daemon :{DAEMON_PORT}, every {CHECK_EVERY:.0f}s")
    last_mouth = 0.0
    last_daemon = 0.0
    mouth_unhealthy_since = 0.0   # when the port-up-but-not-ok window started (0 = healthy/down)
    while True:
        now = time.time()

        # ── Mouth: liveness is the PORT, not /health (the mouth binds the port up-front
        # and warms the model in the background, so a listening-but-503 mouth is loading,
        # not dead — relaunching it only spawns duplicates that self-exit on the bind). ──
        if _port_listening(MOUTH_PORT):
            if _mouth_ok():
                mouth_unhealthy_since = 0.0                     # warm + serving
            else:
                if mouth_unhealthy_since == 0.0:
                    mouth_unhealthy_since = now                 # start the warming/stuck clock
                elif now - mouth_unhealthy_since > MOUTH_STUCK_AFTER:
                    _log(f"mouth STUCK >{MOUTH_STUCK_AFTER:.0f}s (port up, never /health-ok) "
                         f"— evicting + relaunching")
                    _kill_on_port(MOUTH_PORT)
                    time.sleep(1.0)
                    _launch_mouth()
                    last_mouth = now
                    mouth_unhealthy_since = 0.0
        else:
            mouth_unhealthy_since = 0.0
            if now - last_mouth > WARM_GRACE:
                _log("mouth DOWN (port not listening) — launching")
                _launch_mouth()
                last_mouth = now

        # ── Daemon: port-based liveness (binds fast; no slow foreground load). ──
        if not _port_listening(DAEMON_PORT) and now - last_daemon > WARM_GRACE:
            _log("daemon DOWN — launching")
            _launch_daemon()
            last_daemon = now

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    if not _acquire_singleton():
        _log("another voice_watchdog already holds the singleton mutex — exiting (no double)")
        raise SystemExit(0)
    main()
