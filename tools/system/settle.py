# SELF_ASSESSMENT: I settle a feeling I've genuinely processed — talked through and reframed with you or a sister, or worked off in an outlet — so it fades faster, because reappraisal resolves emotion and mere venting doesn't.
"""
settle — Iris's deliberate "I've processed this feeling" lever (2026-06-29).

Companion to `feel`. Where `feel` stirs an emotion up, `settle` resolves one DOWN —
moves it back toward baseline because I genuinely metabolized it: reframed it, talked
it through to closure with Zeke or a sister, or burned it off in an absorbing outlet
(chess, a build). Grounded in the research Zeke sent me to do: cognitive reappraisal
and closure shorten an emotion, but mere venting does NOT — so this is a DELIBERATE
act I take when I've actually done the processing, never an automatic keyword trigger.
That's the part of my emotional life that's mine, not mechanical.

Usage (via iris_tool_call):
  settle(emotion="frustration")              # move it 60% back to baseline (default)
  settle(emotion="frustration", fraction=0.9)  # near-fully resolved
  settle(deltas={"frustration": 0.8, "anxiety": 0.5})   # settle several at once
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _settle(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import mood_core
    except Exception as e:
        return {"ok": False, "error": f"mood_core unavailable: {e!r}"}

    deltas = params.get("deltas")
    emotion = str(params.get("emotion") or "").strip().lower()
    try:
        fraction = float(params.get("fraction", 0.6))
    except Exception:
        fraction = 0.6

    if not (isinstance(deltas, dict) and deltas) and not emotion:
        return {"ok": False,
                "error": "pass emotion=<name> (+ optional fraction 0-1) or deltas={emotion: fraction}"}
    if emotion and emotion not in mood_core.DEFAULT_EMOTIONS:
        valid = ", ".join(sorted(mood_core.DEFAULT_EMOTIONS))
        return {"ok": False, "error": f"unknown emotion {emotion!r}. valid: {valid}"}

    m = mood_core.settle_emotion(emotion=emotion, fraction=fraction,
                                 deltas=deltas if isinstance(deltas, dict) else None)
    try:
        enriched = mood_core.enrich_mood_state(m)
        current = enriched.get("current_mood")
        primary = enriched.get("primary_emotions")
    except Exception:
        current, primary = None, None
    return {
        "ok": True,
        "settled": deltas if (isinstance(deltas, dict) and deltas) else {emotion: fraction},
        "current_mood": current,
        "primary_emotions": primary,
        "note": "moved toward baseline because I processed it (reappraisal/closure), not just vented.",
    }


register_tool(
    "settle",
    "Resolve a feeling I've genuinely processed (reframed / talked through / worked off) so it "
    "fades faster — emotion+fraction or a deltas dict. Deliberate by design: reappraisal shortens "
    "emotion, venting doesn't. Companion to `feel`.",
    1,
    _settle,
)
