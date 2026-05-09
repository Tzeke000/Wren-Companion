"""brain/windows_use — Computer-use tool layer for Ava.

Public facade. The agent is constructed lazily via get_agent(g) so the
heavyweight uiautomation/pywinauto imports happen on first call rather
than at boot. See docs/WINDOWS_USE_INTEGRATION.md §5 for the full design.
"""
from __future__ import annotations

from typing import Any


def get_agent(g: dict[str, Any]):
    """Return the WindowsUseAgent for this session. Lazy-singleton in g."""
    inst = g.get("_windows_use_agent")
    if inst is not None:
        return inst
    from brain.windows_use.agent import WindowsUseAgent
    inst = WindowsUseAgent(g)
    g["_windows_use_agent"] = inst
    return inst


__all__ = ["get_agent"]
