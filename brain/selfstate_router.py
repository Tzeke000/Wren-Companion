"""Compatibility shim: prefer ``from brain.selfstate import ...``."""

from .selfstate import build_selfstate_reply, is_selfstate_query

__all__ = ["build_selfstate_reply", "is_selfstate_query"]
