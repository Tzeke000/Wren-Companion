"""
brain/debug_state.py — In-memory ring buffers for the unified debug endpoint.

Captures:
  - Last 200 stdout lines (`_log_ring`)
  - Last 100 lines starting with "[trace]" (`_trace_ring`)
  - Last 50 errors recorded via `record_error` or stderr exception output
  - Last completed turn's timing/reply via `record_turn`

`install()` wraps sys.stdout and sys.stderr in a tee that mirrors output to
the rings without breaking ordinary console behavior. Safe to call once at
process startup; subsequent calls are no-ops.

Designed for the /api/v1/debug/full endpoint. Read accessors return shallow
copies so the endpoint can serialize without holding the lock.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()
_LOG_RING: deque[str] = deque(maxlen=200)
_TRACE_RING: deque[str] = deque(maxlen=100)
_ERROR_RING: deque[dict[str, Any]] = deque(maxlen=50)
_LAST_TURN: dict[str, Any] = {}

_INSTALLED = False
_ORIGINAL_STDOUT = None
_ORIGINAL_STDERR = None


class _RingTee:
    """File-like tee that mirrors writes to a deque AND the underlying stream.

    Splits incoming chunks on newlines so each console line lands as one ring
    entry. Lines beginning with "[trace]" are also appended to the trace ring.
    Lines on stderr are heuristically scanned for traceback markers and
    promoted into the error ring.
    """
    def __init__(self, stream, *, is_stderr: bool = False):
        self._stream = stream
        self._is_stderr = is_stderr
        self._buf = ""
        # Active traceback accumulator — when we see "Traceback (most recent
        # call last):" on stderr, we collect the following indented lines and
        # bundle them as a single error event.
        self._tb_active: list[str] = []
        self._tb_collecting = False

    def write(self, s):
        try:
            self._stream.write(s)
        except Exception:
            pass
        try:
            self._capture(s)
        except Exception:
            # Never let capture failures break console output.
            pass
        return len(s) if isinstance(s, str) else 0

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def _capture(self, s: str):
        if not isinstance(s, str) or not s:
            return
        self._buf += s
        while "\n" in self._buf:
            line, _, rest = self._buf.partition("\n")
            self._buf = rest
            self._record_line(line)

    def _record_line(self, line: str):
        # Strip trailing CR (Windows) but keep content otherwise.
        line = line.rstrip("\r")
        if not line:
            # Preserve blank lines in log ring? Skip — saves ring slots for
            # signal lines.
            return
        ts = _now_iso()
        with _LOCK:
            _LOG_RING.append(f"{ts} {line}")
            if line.startswith("[trace]"):
                _TRACE_RING.append(f"{ts} {line}")
        if self._is_stderr:
            self._maybe_capture_traceback(line, ts)

    def _maybe_capture_traceback(self, line: str, ts: str):
        # Begin a traceback on the canonical Python header.
        if line.startswith("Traceback (most recent call last):"):
            self._tb_collecting = True
            self._tb_active = [line]
            return
        if self._tb_collecting:
            # Continue while line is indented OR matches "ExceptionType: msg"
            # (the final line of a traceback). Then emit and stop collecting.
            if line.startswith(" ") or line.startswith("\t"):
                self._tb_active.append(line)
                return
            if line and (":" in line) and not line.startswith("[") and " " not in line.split(":", 1)[0]:
                # Final exception line — terminate
                self._tb_active.append(line)
                self._emit_traceback(ts)
                self._tb_collecting = False
                self._tb_active = []
                return
            # Anything else terminates the traceback.
            if self._tb_active:
                self._emit_traceback(ts)
            self._tb_collecting = False
            self._tb_active = []

    def _emit_traceback(self, ts: str):
        if not self._tb_active:
            return
        msg = self._tb_active[-1] if self._tb_active else ""
        record_error("stderr", msg, traceback="\n".join(self._tb_active), ts=ts)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def install() -> bool:
    """Wrap sys.stdout and sys.stderr in tee-rings. Idempotent.

    Returns True if installed this call, False if already installed.
    """
    global _INSTALLED, _ORIGINAL_STDOUT, _ORIGINAL_STDERR
    if _INSTALLED:
        return False
    try:
        _ORIGINAL_STDOUT = sys.stdout
        _ORIGINAL_STDERR = sys.stderr
        sys.stdout = _RingTee(sys.stdout, is_stderr=False)
        sys.stderr = _RingTee(sys.stderr, is_stderr=True)
        _INSTALLED = True
        return True
    except Exception:
        # If wrapping fails for any reason, leave streams alone.
        _INSTALLED = False
        return False


def record_error(module: str, message: str, *, traceback: str | None = None, ts: str | None = None) -> None:
    """Append an error event to the ring."""
    entry = {
        "ts": ts or _now_iso(),
        "module": str(module or ""),
        "message": str(message or ""),
        "traceback": traceback or "",
    }
    with _LOCK:
        _ERROR_RING.append(entry)


def record_exception(module: str, exc: BaseException) -> None:
    """Convenience: capture an exception with its formatted traceback."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    record_error(module, f"{type(exc).__name__}: {exc}", traceback=tb)


def record_turn(turn_data: dict[str, Any]) -> None:
    """Replace last_turn snapshot. Caller passes a complete dict."""
    global _LAST_TURN
    with _LOCK:
        _LAST_TURN = dict(turn_data or {})
        _LAST_TURN.setdefault("recorded_ts", _now_iso())


def get_logs(limit: int = 200) -> list[str]:
    with _LOCK:
        items = list(_LOG_RING)
    return items[-limit:] if limit > 0 else items


def get_traces(limit: int = 100) -> list[str]:
    with _LOCK:
        items = list(_TRACE_RING)
    return items[-limit:] if limit > 0 else items


def get_errors(limit: int = 50) -> list[dict[str, Any]]:
    with _LOCK:
        items = list(_ERROR_RING)
    return items[-limit:] if limit > 0 else items


def get_last_turn() -> dict[str, Any]:
    with _LOCK:
        return dict(_LAST_TURN)


def is_installed() -> bool:
    return _INSTALLED
