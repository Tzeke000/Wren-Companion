"""brain/external_service.py — Retry / backoff / circuit-breaker (architecture #23).

Every call to an external service (Ollama, mem0 vector store, Kokoro,
Claude CLI, internet HTTP) wraps through a uniform handler with:

- Exponential backoff between retries
- Wallclock budget cap per call
- Circuit breaker: after N failures in a row, open the circuit for T
  seconds; subsequent calls fast-fail without hitting the service
- Per-service stats: success / failure / open-circuit count

Why this matters: today's failure modes are inconsistent. Ollama
hangs differently from internet flake from Claude CLI absent. New
external integrations get the same robust handling for free if they
go through this wrapper.

Usage:

    from brain.external_service import call

    def my_request():
        return requests.get("https://example.com", timeout=5)

    result, ok, err = call(
        "example_com_get",
        my_request,
        max_attempts=3,
        backoff_seconds=[0.5, 1.5, 3.0],
        budget_seconds=10.0,
    )
    if ok:
        # success
    elif err == "circuit_open":
        # service is down; try again later
    else:
        # last attempt's exception
        ...

Each unique `service_id` (first arg) gets its own circuit-breaker
state. The breaker opens after `failure_threshold` consecutive
failures and stays open for `open_seconds`. While open, calls
fast-fail without the underlying invocation.

The wrapper is process-singleton — services share state across
threads. Thread-safe.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class _BreakerState:
    consecutive_failures: int = 0
    open_until_ts: float = 0.0
    total_calls: int = 0
    total_successes: int = 0
    total_failures: int = 0
    total_open_circuits: int = 0


_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_OPEN_SECONDS = 30.0
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BACKOFF = (0.5, 1.5, 3.0)


class ExternalServiceManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, _BreakerState] = {}
        # config overrides per service_id
        self._overrides: dict[str, dict[str, Any]] = {}

    def configure(
        self,
        service_id: str,
        *,
        failure_threshold: int | None = None,
        open_seconds: float | None = None,
        max_attempts: int | None = None,
        backoff_seconds: tuple[float, ...] | None = None,
    ) -> None:
        """Override defaults for a specific service. Optional — defaults
        are sensible for most cases."""
        with self._lock:
            cfg = self._overrides.setdefault(service_id, {})
            if failure_threshold is not None:
                cfg["failure_threshold"] = int(failure_threshold)
            if open_seconds is not None:
                cfg["open_seconds"] = float(open_seconds)
            if max_attempts is not None:
                cfg["max_attempts"] = int(max_attempts)
            if backoff_seconds is not None:
                cfg["backoff_seconds"] = tuple(backoff_seconds)

    def _get_state(self, service_id: str) -> _BreakerState:
        with self._lock:
            st = self._states.get(service_id)
            if st is None:
                st = _BreakerState()
                self._states[service_id] = st
            return st

    def _is_open(self, service_id: str) -> bool:
        with self._lock:
            st = self._get_state(service_id)
            now = time.time()
            if st.open_until_ts > now:
                return True
            if st.open_until_ts > 0 and st.open_until_ts <= now:
                # Half-open: allow one trial. Reset open_until_ts to 0
                # so we let the call through; failure will re-open the
                # circuit, success will reset consecutive_failures.
                st.open_until_ts = 0
            return False

    def _on_success(self, service_id: str) -> None:
        with self._lock:
            st = self._get_state(service_id)
            st.total_successes += 1
            st.total_calls += 1
            st.consecutive_failures = 0
            st.open_until_ts = 0

    def _on_failure(self, service_id: str) -> None:
        with self._lock:
            st = self._get_state(service_id)
            st.total_failures += 1
            st.total_calls += 1
            st.consecutive_failures += 1
            cfg = self._overrides.get(service_id, {})
            threshold = int(cfg.get("failure_threshold", _DEFAULT_FAILURE_THRESHOLD))
            if st.consecutive_failures >= threshold:
                open_seconds = float(cfg.get("open_seconds", _DEFAULT_OPEN_SECONDS))
                st.open_until_ts = time.time() + open_seconds
                st.total_open_circuits += 1
                print(f"[external_service] circuit OPEN for {service_id!r} ({st.consecutive_failures} failures, opens for {open_seconds}s)")

    def call(
        self,
        service_id: str,
        fn: Callable[[], Any],
        *,
        max_attempts: int | None = None,
        backoff_seconds: tuple[float, ...] | None = None,
        budget_seconds: float | None = None,
    ) -> tuple[Optional[Any], bool, str]:
        """Invoke `fn()` through the wrapper.

        Returns (result, ok, err_kind):
          ok=True → result is fn()'s return
          ok=False → result is None; err_kind is one of:
            "circuit_open" — circuit is open for this service
            "exception"    — fn raised on every attempt
            "budget"       — wallclock budget exceeded before any attempt succeeded

        Wraps a single conceptual external call. Each call goes through
        all attempts (max_attempts) with backoff between them.
        """
        if self._is_open(service_id):
            with self._lock:
                self._get_state(service_id).total_calls += 1
            return None, False, "circuit_open"

        cfg = self._overrides.get(service_id, {})
        attempts = int(max_attempts if max_attempts is not None else cfg.get("max_attempts", _DEFAULT_MAX_ATTEMPTS))
        backoff = tuple(backoff_seconds if backoff_seconds is not None else cfg.get("backoff_seconds", _DEFAULT_BACKOFF))

        start_ts = time.time()
        last_exc_repr = ""

        for attempt in range(attempts):
            if budget_seconds is not None and (time.time() - start_ts) >= budget_seconds:
                self._on_failure(service_id)
                return None, False, "budget"
            try:
                result = fn()
                self._on_success(service_id)
                return result, True, ""
            except Exception as e:
                last_exc_repr = repr(e)[:200]
                if attempt + 1 < attempts:
                    delay = backoff[attempt] if attempt < len(backoff) else (backoff[-1] if backoff else 1.0)
                    time.sleep(delay)
                continue

        self._on_failure(service_id)
        return None, False, f"exception:{last_exc_repr}"

    def status(self, service_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if service_id is not None:
                st = self._states.get(service_id)
                if st is None:
                    return {"service_id": service_id, "exists": False}
                return self._snapshot_state(service_id, st)
            return {
                sid: self._snapshot_state(sid, st)
                for sid, st in self._states.items()
            }

    def _snapshot_state(self, service_id: str, st: _BreakerState) -> dict[str, Any]:
        now = time.time()
        return {
            "service_id": service_id,
            "consecutive_failures": st.consecutive_failures,
            "is_open": st.open_until_ts > now,
            "open_until_ts": st.open_until_ts,
            "open_seconds_remaining": max(0.0, st.open_until_ts - now),
            "total_calls": st.total_calls,
            "total_successes": st.total_successes,
            "total_failures": st.total_failures,
            "total_open_circuits": st.total_open_circuits,
        }

    def reset(self, service_id: str | None = None) -> None:
        """Reset state. With service_id: only that service. Without: all."""
        with self._lock:
            if service_id is None:
                self._states.clear()
            else:
                self._states.pop(service_id, None)


# Process singleton.
manager = ExternalServiceManager()


# Convenience module-level functions.
def call(
    service_id: str,
    fn: Callable[[], Any],
    *,
    max_attempts: int | None = None,
    backoff_seconds: tuple[float, ...] | None = None,
    budget_seconds: float | None = None,
) -> tuple[Optional[Any], bool, str]:
    return manager.call(
        service_id, fn,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget_seconds=budget_seconds,
    )


def configure(
    service_id: str,
    **kwargs: Any,
) -> None:
    manager.configure(service_id, **kwargs)


def status(service_id: str | None = None) -> dict[str, Any]:
    return manager.status(service_id)
