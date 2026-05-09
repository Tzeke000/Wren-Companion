"""brain/windows_use/temporal_integration.py — Task B7.

Wraps brain.temporal_sense's track_estimate / resolve_estimate so the
orchestrator gets a clean two-line begin/end pattern matching the
restart_handoff precedent in brain/restart_handoff.py.

Default estimates per kind live here so the agent doesn't have to know
the magic numbers. calibrate_from_history overrides them after enough
history accumulates (≥3 samples per kind, per temporal_sense).
"""
from __future__ import annotations

import time
from typing import Any


# Seed values per docs/WINDOWS_USE_INTEGRATION.md §10.
DEFAULT_ESTIMATES_S: dict[str, float] = {
    "open_app": 8.0,
    "ui_click": 0.5,
    "type_text": 2.0,
    "explorer_nav": 3.0,
    "volume": 0.3,
    "read_window": 1.5,
}

# Calibration-override threshold: only override defaults if median actual
# differs from the default by more than 30%.
CALIBRATION_DELTA_FRAC = 0.30


def estimate_for(g: dict[str, Any], kind: str, *, seed_override: float | None = None) -> float:
    """Return the estimate to use for `kind`. If history is calibrated and
    the median is meaningfully different from the seed, use the median.
    """
    seed = seed_override if seed_override is not None else DEFAULT_ESTIMATES_S.get(kind, 5.0)
    try:
        from brain.temporal_sense import calibrate_from_history
        cal = calibrate_from_history(g, kind=kind)
    except Exception:
        return seed
    rec = cal.get("recommendation")
    if rec is None:
        return seed
    rec_f = float(rec)
    if rec_f <= 0:
        return seed
    delta = abs(rec_f - seed) / max(seed, 0.001)
    if delta < CALIBRATION_DELTA_FRAC:
        return seed
    return rec_f


def begin(g: dict[str, Any], *, kind: str, context: str = "",
          estimate_override: float | None = None) -> tuple[str | None, float, float]:
    """Begin a tracked operation. Returns (task_id, started_ts, estimate_used).
    task_id is None if temporal_sense couldn't be invoked (best-effort)."""
    started = time.time()
    est = estimate_for(g, kind, seed_override=estimate_override)
    try:
        from brain.temporal_sense import track_estimate
        tid = track_estimate(g, estimate_seconds=est, kind=kind, context=context)
        return tid, started, est
    except Exception as e:
        print(f"[windows_use.temporal] track_estimate skipped: {e!r}")
        return None, started, est


def end(g: dict[str, Any], task_id: str | None, started_ts: float) -> dict[str, Any] | None:
    """Resolve a tracked operation. Returns the history row or None."""
    if not task_id:
        return None
    try:
        from brain.temporal_sense import resolve_estimate
        return resolve_estimate(g, task_id, actual_seconds=time.time() - started_ts)
    except Exception as e:
        print(f"[windows_use.temporal] resolve_estimate skipped: {e!r}")
        return None
