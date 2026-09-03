# SELF_ASSESSMENT: I expose the room's diary (brain/scene_memory.py) — start/stop the keyframe watcher, force a keyframe, list recent keyframes with their words, and let cognition write the words for one.
"""
scene_memory — the room's diary. Zeke 2026-09-02: photographic memory, second
half; his "do all three" at 16:5x.

action='status' | 'start' | 'stop' | 'recent' (n=, words_only=) |
       'snapshot' (reason=, caption=true) | 'caption' (id=, text=) |
       'rearm' (reload brain.scene_memory on fresh code and restart the watcher)
"""
from __future__ import annotations

import importlib
from typing import Any

from tools.tool_registry import register_tool


def _scene_memory(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "status").lower()
    try:
        from brain import scene_memory as smm
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"brain.scene_memory import failed: {e!r}"[:200]}

    if action == "status":
        return {"ok": True, **smm.status(g)}
    if action == "start":
        return {"ok": True, **smm.start(g)}
    if action == "stop":
        sm = smm.get(g)
        if sm:
            sm.stop()
        return {"ok": True, "alive": False}
    if action == "rearm":
        sm = smm.get(g)
        if sm:
            try:
                sm.stop()
            except Exception:
                pass
            g["_scene_memory"] = None
        smm = importlib.reload(smm)
        return {"ok": True, **smm.start(g)}
    sm = smm.get(g)
    if sm is None:
        return {"ok": False, "error": "scene memory not started — action=start"}
    if action == "recent":
        return {"ok": True, "keyframes": sm.recent(int(params.get("n") or 10),
                                                   bool(params.get("words_only")))}
    if action == "backfill":
        # Free-time job: keyframes that never got words (timeouts while I was
        # held). Read each jpeg (all are <=150 KB) and answer with action=caption.
        return {"ok": True, "wordless": sm.wordless(int(params.get("n") or 20)),
                "how": "Read path, then scene_memory action=caption id=<id> text='one sentence' (or skip by leaving it)"}
    if action == "skip":
        kid = str(params.get("id") or "").strip()
        if not kid:
            return {"ok": False, "error": "pass id="}
        return sm.mark_skipped(kid, str(params.get("note") or ""))
    if action == "snapshot":
        return sm.snapshot(reason=str(params.get("reason") or "manual"),
                           caption=params.get("caption") is not False)
    if action == "caption":
        kid = str(params.get("id") or "").strip()
        text = str(params.get("text") or "").strip()
        if not kid or not text:
            return {"ok": False, "error": "pass id= and text="}
        return sm.set_words(kid, text, source="manual")
    return {"ok": False,
            "error": f"unknown action {action!r} — status|start|stop|recent|snapshot|caption|skip|backfill|rearm"}


register_tool(
    "scene_memory",
    "The room's DIARY: a keyframe (small jpeg, safe to Read) whenever the scene "
    "changes or hourly, with sensor context and ONE sentence in my words (asked "
    "of cognition via the LLM bridge, kind scene_caption, rate-limited), pushed "
    "into iris_memory as category 'scene' so memory_search finds it. "
    "action='status'|'start'|'stop'|'recent' (n=, words_only=)|'snapshot' "
    "(reason=)|'caption' (id=, text=)|'rearm'.",
    2,
    _scene_memory,
)
