# SELF_ASSESSMENT: I expose the sleep_mode tick state so I can verify the heartbeat-wired sleep_mode.tick() is actually firing.
"""
sleep_mode_status — read the current sleep_mode state from g.

After commit 5bf2b5d wired brain/sleep_mode.tick into the heartbeat loop,
there's no MCP surface that lets me observe whether it's actually running.
Sleep handoff files only appear during state transitions; in steady AWAKE
state, tick returns {"state": "AWAKE"} into g["sleep_mode"] but writes nothing
to disk.

This tool reads g["sleep_mode"] (set by sleep_mode.tick) and g["_sleep_state"]
(the underlying state machine value) so verification doesn't require manual
trigger-firing.
"""
from __future__ import annotations

from typing import Any
from tools.tool_registry import register_tool


def _sleep_mode_status(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    sleep_mode_dict = g.get("sleep_mode")
    underlying_state = g.get("_sleep_state")
    sleep_until = g.get("_sleep_until_ts")
    sleep_started = g.get("_sleep_started_ts")
    sleep_trigger = g.get("_sleep_trigger")
    return {
        "ok": True,
        "tick_observed": sleep_mode_dict is not None,
        "tick_result": sleep_mode_dict,
        "underlying_state": underlying_state,
        "sleep_until_ts": sleep_until,
        "sleep_started_ts": sleep_started,
        "sleep_trigger": sleep_trigger,
        "note": (
            "tick_observed=True means sleep_mode.tick() has run at least once "
            "since iris_runtime boot, confirming the 5bf2b5d heartbeat wiring "
            "is functional. tick_observed=False means either the heartbeat "
            "isn't running, the import failed, or tick raised."
        ),
    }


register_tool(
    "sleep_mode_status",
    "Read sleep_mode tick result from g (verifies heartbeat-wired sleep_mode is firing).",
    1,
    _sleep_mode_status,
)
