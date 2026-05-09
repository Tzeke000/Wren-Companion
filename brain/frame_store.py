"""
Phase 2 — live frame acquisition, timestamps, and freshness classification.

Keeps the last successfully captured frame, attaches capture wall time, computes age,
classifies **fresh / aging / stale / unavailable**, and logs acquisition outcomes.

Thresholds are centralized below; adjust here rather than scattering magic numbers.

**Compatibility:** :func:`read_live_frame` in ``camera_live.py`` still returns
``(frame, capture_ts)``. Use :func:`read_live_frame_with_meta` when you need
``freshness``, ``origin``, and explicit age.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

try:
    import cv2
except Exception:
    cv2 = None

# ---------------------------------------------------------------------------
# Centralized age thresholds (seconds since capture_wall_time)
# ---------------------------------------------------------------------------
# At or below FRESH_MAX: classify as "fresh" (recent capture).
# Above FRESH_MAX through AGING_MAX: "aging" (usable cache / soft staleness).
# Above AGING_MAX (frame still present): "stale" for diagnostics; vision gating
# in camera.py still uses STALE_FRAME_MS separately.
FRESH_MAX_AGE_SEC = 0.35
AGING_MAX_AGE_SEC = 1.20

# How long we may return the same live frame without re-opening the device.
LIVE_CACHE_MAX_AGE_SEC = 1.5

FreshnessLabel = Literal["fresh", "aging", "stale", "unavailable"]
OriginLabel = Literal["cache", "device", "none"]


def classify_acquisition_freshness(has_frame: bool, age_sec: float | None) -> FreshnessLabel:
    """
    Label live/UI frame age for logging and downstream display.

    ``age_sec`` is wall-clock seconds since capture; ``None`` or non-finite → unavailable.
    """
    if not has_frame or age_sec is None or age_sec < 0 or age_sec == float("inf"):
        return "unavailable"
    if age_sec <= FRESH_MAX_AGE_SEC:
        return "fresh"
    if age_sec <= AGING_MAX_AGE_SEC:
        return "aging"
    return "stale"


@dataclass
class LiveFrameResult:
    """One live acquisition attempt (cache hit, new device read, or failure)."""

    frame: Any
    capture_ts: float
    age_sec: float
    freshness: FreshnessLabel
    origin: OriginLabel


# Latest good frame from the device (not used for UI path).
_buffer_frame: Any = None
_buffer_ts: float = 0.0


def _now() -> float:
    return time.time()


def read_live_frame_with_meta(
    *,
    max_age: float = LIVE_CACHE_MAX_AGE_SEC,
    device_index: int = 0,
) -> LiveFrameResult:
    """
    Acquire a live frame: prefer in-memory buffer if younger than ``max_age``,
    otherwise open the camera, read once, and update the buffer on success.

    If the cache is older than ``max_age`` and a new device read fails, returns
    no frame (same as the legacy ``camera_live`` contract — no silent fallback
    to a very old buffer).
    """
    global _buffer_frame, _buffer_ts
    now = _now()

    if _buffer_frame is not None and _buffer_ts > 0.0:
        cache_age = now - _buffer_ts
        if cache_age <= max_age:
            freshness = classify_acquisition_freshness(True, cache_age)
            return LiveFrameResult(
                frame=_buffer_frame,
                capture_ts=float(_buffer_ts),
                age_sec=cache_age,
                freshness=freshness,
                origin="cache",
            )
        # Cache exists but is too old for fast path — try a new read before dropping.

    if cv2 is None:
        print("[frame_store] cv2 unavailable (import failed) — no frame")
        return LiveFrameResult(
            frame=None, capture_ts=0.0, age_sec=-1.0, freshness="unavailable", origin="none"
        )

    cap = cv2.VideoCapture(device_index)
    if not cap or not cap.isOpened():
        print(
            f"[frame_store] VideoCapture({device_index}) failed or not opened — "
            "camera failed to open"
        )
        print("[frame_store] no_frame_available (open failed)")
        return LiveFrameResult(
            frame=None, capture_ts=0.0, age_sec=-1.0, freshness="unavailable", origin="none"
        )

    ok = False
    frame = None
    try:
        ok, frame = cap.read()
    finally:
        cap.release()

    if ok and frame is not None:
        _buffer_frame = frame
        _buffer_ts = now
        return LiveFrameResult(
            frame=frame,
            capture_ts=now,
            age_sec=0.0,
            freshness="fresh",
            origin="device",
        )

    print(
        "[frame_store] frame_read_failure device read returned empty or not ok — "
        "no_frame_available (no stale fallback)"
    )
    return LiveFrameResult(
        frame=None, capture_ts=0.0, age_sec=-1.0, freshness="unavailable", origin="none"
    )


def push_frame(frame: Any) -> None:
    """Called by the persistent background camera thread to update the shared buffer.
    This decouples the background capture loop from read_live_frame_with_meta so the
    live_frame endpoint never needs to open its own VideoCapture."""
    global _buffer_frame, _buffer_ts
    _buffer_frame = frame
    _buffer_ts = _now()


def get_buffered_frame(max_age_sec: float = 10.0) -> "LiveFrameResult":
    """Return the frame pushed by the background thread, or unavailable if stale/missing.
    Does NOT open its own VideoCapture — solely reads from the push_frame() buffer.
    Use this in the live_frame endpoint instead of read_live_frame_with_meta."""
    now = _now()
    if _buffer_frame is None or _buffer_ts <= 0.0:
        return LiveFrameResult(frame=None, capture_ts=0.0, age_sec=-1.0, freshness="unavailable", origin="none")
    age = now - _buffer_ts
    if age > max_age_sec:
        return LiveFrameResult(frame=None, capture_ts=float(_buffer_ts), age_sec=age, freshness="stale", origin="cache")
    freshness = classify_acquisition_freshness(True, age)
    return LiveFrameResult(frame=_buffer_frame, capture_ts=float(_buffer_ts), age_sec=age, freshness=freshness, origin="cache")


def peek_buffer_age_sec() -> float | None:
    """Debug helper: seconds since last successful device capture, or None if empty."""
    if _buffer_frame is None or _buffer_ts <= 0.0:
        return None
    return max(0.0, _now() - _buffer_ts)
