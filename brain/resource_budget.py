"""brain/resource_budget.py — Resource budget system (architecture #24).

GPU VRAM, CPU, disk space, network bandwidth, LLM-cycle time —
all finite. Today, features compete first-come-first-served. One
greedy subsystem can starve everything else (e.g., a slow
build_prompt holding the Ollama lock for 30s).

This module is the BUDGET TRACKER + ENFORCEMENT seam:

- Each resource class has a soft budget (target ceiling) and a hard
  budget (absolute cap)
- Features reserve capacity by declaring (kind, amount, duration_s)
- The tracker says yes / no / wait based on current commitments
- Reservation logs let us answer "what was using all the VRAM?"

Today: tracking + advisory enforcement. Hard enforcement (refusing
new reservations when over budget) is OPT-IN per feature. The seam
exists so features can adopt budget-awareness without retrofitting
the tracker into existing code.

Resource kinds (today):

  llm_cycles_seconds — wallclock time on Ollama / LLM. Foreground
                       turns get priority; background should yield
                       when a foreground turn is in flight.
  vram_mb            — GPU VRAM committed. Tight on 8GB cards.
  cpu_load           — abstract "how busy is the CPU" estimate.
  disk_writes_mb     — disk I/O budget per minute.
  network_calls      — outbound network calls per minute.

API:

    from brain.resource_budget import reserve, release, status

    handle = reserve("subagent", "llm_cycles_seconds", 60.0)
    if handle is None:
        # over budget; defer / decline
        return
    try:
        ... do the LLM call ...
    finally:
        release(handle)

The reserve/release pattern is intentionally lightweight — no
context manager since some reservations are async (subagent runs
in background thread + releases when done).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


ResourceKind = Literal[
    "llm_cycles_seconds",
    "vram_mb",
    "cpu_load",
    "disk_writes_mb",
    "network_calls",
]


# Soft + hard budgets per resource. Soft = "warn / suggest yield."
# Hard = "refuse new reservations beyond this." On 8GB VRAM the hard
# is ~7GB to leave headroom for OS / browser. Adjust as needed.

DEFAULT_BUDGETS: dict[str, tuple[float, float]] = {
    # (soft, hard)
    "llm_cycles_seconds": (60.0, 180.0),    # in any 60-second window
    "vram_mb": (6500.0, 7500.0),            # leave headroom for OS / browser
    "cpu_load": (0.7, 0.9),                 # fraction of 1.0 across cores
    "disk_writes_mb": (50.0, 200.0),        # per minute
    "network_calls": (30.0, 100.0),         # per minute
}


@dataclass
class Reservation:
    handle: str
    feature: str
    kind: str
    amount: float
    started_ts: float
    expected_duration_s: float = 0.0
    notes: str = ""


@dataclass
class _ResourceState:
    committed: float = 0.0
    total_reservations_made: int = 0
    total_denials: int = 0


class BudgetTracker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._budgets: dict[str, tuple[float, float]] = dict(DEFAULT_BUDGETS)
        self._states: dict[str, _ResourceState] = {
            kind: _ResourceState() for kind in DEFAULT_BUDGETS
        }
        self._reservations: dict[str, Reservation] = {}

    def configure(self, kind: str, *, soft: float | None = None, hard: float | None = None) -> None:
        """Override budgets for a specific kind."""
        with self._lock:
            cur_soft, cur_hard = self._budgets.get(kind, (0.0, 0.0))
            new_soft = float(soft) if soft is not None else cur_soft
            new_hard = float(hard) if hard is not None else cur_hard
            self._budgets[kind] = (new_soft, new_hard)
            self._states.setdefault(kind, _ResourceState())

    def reserve(
        self,
        feature: str,
        kind: str,
        amount: float,
        *,
        expected_duration_s: float = 0.0,
        notes: str = "",
        enforce: bool = False,
    ) -> str | None:
        """Try to reserve `amount` of `kind` for `feature`.

        Returns a handle (string) on success.
        Returns None if `enforce=True` and the new reservation would
        exceed the hard budget.

        With `enforce=False` (default): always succeeds, just tracks
        the commitment. Lets us OBSERVE budget usage before turning
        on enforcement per-feature.
        """
        amount = max(0.0, float(amount))
        with self._lock:
            state = self._states.setdefault(kind, _ResourceState())
            soft, hard = self._budgets.get(kind, (float("inf"), float("inf")))
            if enforce and (state.committed + amount) > hard:
                state.total_denials += 1
                return None
            handle = uuid.uuid4().hex[:12]
            res = Reservation(
                handle=handle,
                feature=feature,
                kind=kind,
                amount=amount,
                started_ts=time.time(),
                expected_duration_s=float(expected_duration_s),
                notes=notes,
            )
            self._reservations[handle] = res
            state.committed += amount
            state.total_reservations_made += 1
            if state.committed > soft:
                # Soft warning — log + still allow
                print(f"[resource_budget] over SOFT budget on {kind}: committed={state.committed:.1f} soft={soft:.1f} (feature={feature!r})")
        return handle

    def release(self, handle: str) -> bool:
        """Release a reservation. Returns True if it existed."""
        with self._lock:
            res = self._reservations.pop(handle, None)
            if res is None:
                return False
            state = self._states.get(res.kind)
            if state is not None:
                state.committed = max(0.0, state.committed - res.amount)
        return True

    def is_over_soft(self, kind: str) -> bool:
        with self._lock:
            state = self._states.get(kind)
            soft, _ = self._budgets.get(kind, (float("inf"), float("inf")))
            return bool(state and state.committed > soft)

    def is_over_hard(self, kind: str) -> bool:
        with self._lock:
            state = self._states.get(kind)
            _, hard = self._budgets.get(kind, (float("inf"), float("inf")))
            return bool(state and state.committed > hard)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "budgets": {k: {"soft": s, "hard": h} for k, (s, h) in self._budgets.items()},
                "states": {
                    k: {
                        "committed": st.committed,
                        "over_soft": st.committed > self._budgets.get(k, (float("inf"), float("inf")))[0],
                        "over_hard": st.committed > self._budgets.get(k, (float("inf"), float("inf")))[1],
                        "total_reservations": st.total_reservations_made,
                        "total_denials": st.total_denials,
                    }
                    for k, st in self._states.items()
                },
                "active_reservations": [
                    {
                        "handle": r.handle,
                        "feature": r.feature,
                        "kind": r.kind,
                        "amount": r.amount,
                        "age_seconds": time.time() - r.started_ts,
                        "expected_duration_s": r.expected_duration_s,
                    }
                    for r in self._reservations.values()
                ],
            }

    def reset(self) -> None:
        with self._lock:
            self._states = {kind: _ResourceState() for kind in self._budgets}
            self._reservations.clear()


# Process singleton.
tracker = BudgetTracker()


# Module-level convenience.
def reserve(feature: str, kind: str, amount: float, **kwargs: Any) -> str | None:
    return tracker.reserve(feature, kind, amount, **kwargs)


def release(handle: str) -> bool:
    return tracker.release(handle)


def status() -> dict[str, Any]:
    return tracker.status()


def is_over_soft(kind: str) -> bool:
    return tracker.is_over_soft(kind)


def is_over_hard(kind: str) -> bool:
    return tracker.is_over_hard(kind)


def configure(kind: str, *, soft: float | None = None, hard: float | None = None) -> None:
    tracker.configure(kind, soft=soft, hard=hard)
