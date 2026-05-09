"""
Low-level live camera read — thin wrapper over :mod:`brain.frame_store`.

Still returns ``(frame_bgr_or_none, capture_wall_time)`` for backward compatibility.
For freshness, age, and origin, use :func:`brain.frame_store.read_live_frame_with_meta`.

Manual test plan (obstruction / recovery) — see ``brain/camera`` module docstring.
"""
from __future__ import annotations

from .frame_store import LIVE_CACHE_MAX_AGE_SEC, read_live_frame_with_meta


def read_live_frame(max_age: float = LIVE_CACHE_MAX_AGE_SEC, device_index: int = 0):
    """
    Returns (frame_bgr_or_none, capture_wall_time).

    ``capture_wall_time`` is when the frame was produced (device read) or when the
    cached frame was originally captured. Logs and acquisition policy live in
    ``frame_store``.
    """
    m = read_live_frame_with_meta(max_age=max_age, device_index=device_index)
    return m.frame, m.capture_ts
