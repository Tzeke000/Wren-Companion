# SELF_ASSESSMENT: I restart just the voice engine subprocess (XTTS) without restarting iris_runtime or the whole CC session.
"""
Phase 60 — Tier 1 voice-engine respawn tool.

Restarts the xtts_server subprocess in place when the child died (crash,
OOM, manual kill, or update to brain/xtts_server.py that needs activation)
WITHOUT requiring a full iris_runtime restart.

Why this exists: the XTTS subprocess lives in .venv_xtts with its own
deps. Sometimes it dies (crash, hang, OOM during model load). Before this
tool, the only path back was restart_self → CC restart → iris_runtime
respawn → xtts_server respawn. Heavy.

Now: I call iris_tool_call("voice_respawn") and the same TTSWorker
singleton in the running iris_runtime kills + re-spawns its xtts child.
Voice comes back without losing CC session continuity.

Scope: ONLY the XTTS subprocess. Doesn't touch Kokoro/Piper engines,
iris_runtime's main loop, or any other subsystem. If those need restart,
use restart_self.
"""
from __future__ import annotations

import time
from typing import Any

from tools.tool_registry import register_tool


def _respawn_xtts(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Respawn the xtts_server subprocess.

    Reaches into iris_runtime's module-level _tts singleton (the live
    TTSWorker instance) and calls its respawn_xtts() method. The method
    cleanly shuts down the old subprocess, clears stale pending-event
    state, and re-runs _try_init_xtts to spawn a fresh child.

    Falls back gracefully if iris_runtime isn't importable or the singleton
    isn't initialized — reports the failure rather than crashing.

    Params: none required.

    Returns:
        {ok, pid?, error?, engine_type?, duration_ms}
    """
    t0 = time.time()
    try:
        # Import lazily — at registration time iris_runtime might not be
        # fully imported, but at call time it definitely is (since the
        # MCP server is running and accepting tool calls).
        import iris_runtime  # type: ignore
    except Exception as e:
        return {"ok": False, "error": f"iris_runtime not importable: {e!r}"}

    tts = getattr(iris_runtime, "_tts", None)
    if tts is None:
        return {
            "ok": False,
            "error": "TTSWorker singleton not initialized — call any voice "
                     "tool first to bootstrap it, or restart iris_runtime.",
        }

    engine = getattr(tts, "_engine_type", "unknown")
    if engine != "xtts":
        return {
            "ok": False,
            "error": f"current TTS engine is '{engine}', not xtts — "
                     f"respawn_xtts only handles the xtts subprocess. "
                     f"Set AVA_TTS_ENGINE=xtts and restart_self to switch.",
            "engine_type": engine,
        }

    if not hasattr(tts, "respawn_xtts"):
        return {
            "ok": False,
            "error": "tts.respawn_xtts method not present — iris_runtime "
                     "or tts_worker is older than expected.",
            "engine_type": engine,
        }

    try:
        result = tts.respawn_xtts()
    except Exception as e:
        return {"ok": False, "error": f"respawn_xtts raised: {e!r}",
                "duration_ms": int((time.time() - t0) * 1000)}

    duration_ms = int((time.time() - t0) * 1000)
    if not isinstance(result, dict):
        result = {"ok": False, "error": f"respawn_xtts returned non-dict: {result!r}"}
    result.setdefault("engine_type", engine)
    result["duration_ms"] = duration_ms
    return result


register_tool(
    name="voice_respawn",
    description=(
        "Respawn the XTTS voice engine subprocess WITHOUT restarting "
        "iris_runtime or CC. Use when xtts_server has died, crashed, or "
        "when brain/xtts_server.py was edited and the new code needs to "
        "activate. Returns {ok, pid?, error?, duration_ms}. Only handles "
        "the xtts subprocess; for Kokoro/Piper/iris_runtime itself, use "
        "restart_self instead."
    ),
    tier=1,
    handler=_respawn_xtts,
)
