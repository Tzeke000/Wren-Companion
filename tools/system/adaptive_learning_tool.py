"""adaptive_learning — run / inspect the Iris-path adaptive-learning loop (Iris_fixes #7).

Actions: status (default) | learn [dry_run] | explain | hints
The loop: evidence (wake verdicts, transcript barge-ins, memory access meta, leisure→learning)
→ EWMA-bounded weights in state/learning/adaptive_preferences.json → consumers (ambient
snapshot advisories, host prompts, question cooldown, memory rerank). `learn` is what the
self-cron should call each cycle; nothing else writes the weights on this machine.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _adaptive_learning(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import adaptive_iris as ai
    except Exception as e:
        return {"ok": False, "error": f"import failed: {e!r}"}
    p = params or {}
    action = str(p.get("action") or "status").strip().lower()
    try:
        if action in ("status", "explain"):
            return {"ok": True, **ai.explain()}
        if action == "learn":
            r = ai.learn(dry_run=bool(p.get("dry_run", False)))
            r.pop("prefs", None)
            return r
        if action == "hints":
            return {"ok": True, "hints": ai.hints()}
        return {"ok": False, "error": f"unknown action {action!r}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


register_tool(
    "adaptive_learning",
    "ADAPTIVE-LEARNING LOOP (Iris_fixes #7): derive learned weights (proactive usefulness, "
    "pacing, memory usefulness, curiosity usefulness, silence-when-better) from real evidence "
    "and show what consumes them. Actions: status | learn [dry_run] | hints.",
    1,
    _adaptive_learning,
)
