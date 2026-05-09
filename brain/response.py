"""
Compatibility shim: canonical implementations live in ``brain.selfstate``.
"""

from __future__ import annotations

from .selfstate import (
    build_selfstate_reply,
    is_selfstate_query,
    summarize_health,
    summarize_mood,
)

__all__ = [
    "build_selfstate_reply",
    "is_selfstate_query",
    "summarize_health",
    "summarize_mood",
]
