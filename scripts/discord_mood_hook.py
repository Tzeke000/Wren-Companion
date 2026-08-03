# SELF_ASSESSMENT: I am the Discord→mood bridge — a PostToolUse hook that fires
# after Iris sends a Discord reply and routes the reply's emotional tenor into
# mood_core, exactly like the voice/orb-chat paths do. Born 2026-08-03 from the
# mood study: Discord was Iris's only live surface for a month and was never
# wired to mood, so mood ran on pure clockwork. Zeke greenlit making the push
# mechanical ("you should make those pushes mechanical"). Fail-open by contract:
# ANY failure exits 0 silently — a mood hiccup must never block a Discord reply.
"""Reads the PostToolUse hook JSON on stdin; if the tool was the Discord reply
tool, applies infer_affect_nudge(reply_text) via nudge_emotions (same recipe as
iris_runtime.py:1606 orb-chat path). Cross-process safe: mood state is on-disk
read-modify-write with atomic tmp+replace (mood_core 2026-06-29 sweep)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(r"D:\Wren-Companion")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool = str(payload.get("tool_name", ""))
        if "discord" not in tool or not tool.endswith("__reply"):
            return 0
        text = str((payload.get("tool_input") or {}).get("text") or "")
        if not text.strip():
            return 0
        sys.path.insert(0, str(REPO))
        from brain import mood_core
        mood_core.configure(REPO)
        deltas = mood_core.infer_affect_nudge(text)
        if deltas:
            mood_core.nudge_emotions(deltas, reason="expressed affect (discord)")
        else:
            mood_core.nudge_emotions({"interest": 0.01, "calmness": -0.003},
                                     reason="engagement (discord)")
    except Exception:
        pass  # fail-open: never block the reply path
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
