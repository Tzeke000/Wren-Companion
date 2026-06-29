# SELF_ASSESSMENT: I read my own body history — a timestamped record of what my eyes saw, ears heard, mouth said, orb showed, and how my mood shifted, with date and time on every entry.
"""
body_log — read my timestamped body/sensory history (2026-06-29, Zeke directive).

Zeke: "time should be a big thing for you — everything logged with date and time, like your
eyes, ears, mouth, orb and whatnot." brain/iris_body_log records every body event; this tool
reads it back so I can answer "when did X happen" from a real record instead of a guess.
(The wall-clock rule still governs anything I SAY about the current time — time_check first.)

Usage (via iris_tool_call):
  body_log()                       # last 30 events, all channels
  body_log(n=60)                   # last 60
  body_log(channel="eyes")         # only what I saw
  body_log(channel="mouth", n=20)  # last 20 things I said
Channels: eyes | ears | mouth | orb | mood | body
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _body_log(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import iris_body_log
    except Exception as e:
        return {"ok": False, "error": f"iris_body_log unavailable: {e!r}"}
    try:
        n = int(params.get("n", 30))
    except Exception:
        n = 30
    channel = params.get("channel")
    if channel is not None:
        channel = str(channel).strip().lower() or None
        if channel and channel not in iris_body_log.CHANNELS:
            return {"ok": False,
                    "error": f"unknown channel {channel!r}. valid: {', '.join(iris_body_log.CHANNELS)}"}
    events = iris_body_log.recent(n=n, channel=channel)
    return {"ok": True, "count": len(events), "channel": channel or "all", "events": events}


register_tool(
    "body_log",
    "Read my timestamped body history (eyes/ears/mouth/orb/mood, date+time on every entry). "
    "Args: n (default 30), channel (optional filter). Reads brain/iris_body_log.",
    1,
    _body_log,
)
