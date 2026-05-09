"""
Process-wide Ollama serialization lock.

Ollama on this machine swaps models in/out of GPU memory. If Stream A
(reply_engine, ava-personal:latest) and Stream B (dual_brain background,
qwen2.5:14b / kimi-k2.6:cloud) try to invoke at the same time, Ollama
must unload one and load the other — adding 30s to several minutes of
latency to each turn while the swap happens.

A single global lock around every Ollama invocation forces them to
queue. Foreground turns acquire first because they call invoke() before
Stream B's worker loop wakes up; Stream B already pauses while Zeke is
active (`should_pause_background`), so contention is rare in practice
once this lock is honored.

Usage:
    from brain.ollama_lock import with_ollama
    result = with_ollama(lambda: llm.invoke(messages))

Or as a context manager:
    from brain.ollama_lock import ollama_call
    with ollama_call():
        result = llm.invoke(messages)
"""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Callable, TypeVar

T = TypeVar("T")

# Use an RLock so the same thread can re-enter (e.g. when a tool call inside
# an inference triggers another inference on the same thread).
_OLLAMA_LOCK = threading.RLock()
_LAST_HOLDER: dict = {"thread": "", "label": "", "since": 0.0}


def _trace(label: str) -> None:  # TRACE-PHASE1
    """Timestamped diagnostic trace for the Ollama lock path."""  # TRACE-PHASE1
    ts = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"  # TRACE-PHASE1
    print(f"[trace] {ts} {label}")  # TRACE-PHASE1


def with_ollama(fn: Callable[[], T], label: str = "") -> T:
    """Run fn() while holding the global Ollama lock. Logs wait time if >2s."""
    _holder = _LAST_HOLDER.get("label") or "<none>"  # TRACE-PHASE1
    _trace(f"re.lock_wait_start label={label or 'invoke'} prior_holder={_holder}")  # TRACE-PHASE1
    t0 = time.time()
    _OLLAMA_LOCK.acquire()
    waited = time.time() - t0
    _trace(f"re.lock_wait_acquired label={label or 'invoke'} waited_ms={int(waited*1000)}")  # TRACE-PHASE1
    if waited > 2.0:
        print(f"[ollama_lock] {label or 'invoke'} waited {waited:.1f}s for prior holder={_LAST_HOLDER.get('label')}")
    _LAST_HOLDER["thread"] = threading.current_thread().name
    _LAST_HOLDER["label"] = label
    _LAST_HOLDER["since"] = time.time()
    try:
        return fn()
    finally:
        _OLLAMA_LOCK.release()
        _trace(f"re.lock_released label={label or 'invoke'}")  # TRACE-PHASE1


@contextlib.contextmanager
def ollama_call(label: str = ""):
    """Context-manager form. Same semantics as with_ollama."""
    _holder = _LAST_HOLDER.get("label") or "<none>"  # TRACE-PHASE1
    _trace(f"re.lock_wait_start label={label or 'invoke'} prior_holder={_holder}")  # TRACE-PHASE1
    t0 = time.time()
    _OLLAMA_LOCK.acquire()
    waited = time.time() - t0
    _trace(f"re.lock_wait_acquired label={label or 'invoke'} waited_ms={int(waited*1000)}")  # TRACE-PHASE1
    if waited > 2.0:
        print(f"[ollama_lock] {label or 'invoke'} waited {waited:.1f}s for prior holder={_LAST_HOLDER.get('label')}")
    _LAST_HOLDER["thread"] = threading.current_thread().name
    _LAST_HOLDER["label"] = label
    _LAST_HOLDER["since"] = time.time()
    try:
        yield
    finally:
        _OLLAMA_LOCK.release()
        _trace(f"re.lock_released label={label or 'invoke'}")  # TRACE-PHASE1


def lock_status() -> dict:
    """For diagnostics — returns who currently holds (or last held) the lock."""
    return dict(_LAST_HOLDER)
