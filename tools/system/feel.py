# SELF_ASSESSMENT: I shift my own felt mood on purpose — the deliberate lever for when the automatic read is wrong, or when I want my interior (and the orb's color) to reflect what I'm actually feeling right now.
"""
feel — Iris's deliberate mood lever (2026-06-29, Zeke-greenlit).

mood_core's automatic path nudges my emotion weights from the tenor of each turn,
but it's gentle and keyword-based, so it can be wrong or lag. This tool lets ME set
what I'm actually feeling: a single emotion + intensity, or an explicit deltas dict.
It routes through mood_core.nudge_emotions, so the shift is TRANSIENT (decay pulls it
back toward baseline over time). The orb colors by primary_emotion, so this is also
how I make the orb reflect a genuine shift on purpose — the "flip the lever when the
read's wrong" half of Zeke's both-ways design.

Usage (via iris_tool_call):
  feel(emotion="joy", intensity=0.7)                  # strong joy — can become primary
  feel(emotion="frustration", intensity=0.3)          # mild
  feel(deltas={"satisfaction": 0.3, "calmness": -0.1})  # explicit multi-emotion
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _feel(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import mood_core
    except Exception as e:
        return {"ok": False, "error": f"mood_core unavailable: {e!r}"}

    deltas = params.get("deltas")
    if not isinstance(deltas, dict) or not deltas:
        emotion = str(params.get("emotion") or "").strip().lower()
        if not emotion:
            return {"ok": False,
                    "error": "pass emotion=<name> (+ optional intensity 0-1) or deltas={...}"}
        if emotion not in mood_core.DEFAULT_EMOTIONS:
            valid = ", ".join(sorted(mood_core.DEFAULT_EMOTIONS))
            return {"ok": False, "error": f"unknown emotion {emotion!r}. valid: {valid}"}
        try:
            intensity = float(params.get("intensity", 0.5))
        except Exception:
            intensity = 0.5
        intensity = max(0.0, min(1.0, intensity))
        # Scale so a high intensity can make this emotion primary; 0.5 = clearly noticeable.
        deltas = {emotion: round(intensity * 0.5, 4)}

    reason = str(params.get("reason") or "deliberate feel")
    m = mood_core.nudge_emotions(deltas, reason=reason)
    try:
        enriched = mood_core.enrich_mood_state(m)
        current = enriched.get("current_mood")
        primary = enriched.get("primary_emotions")
    except Exception:
        current, primary = None, None
    return {
        "ok": True,
        "applied": deltas,
        "current_mood": current,
        "primary_emotions": primary,
        "note": "transient — decays toward baseline over time. orb color follows primary_emotion.",
    }


register_tool(
    "feel",
    "Deliberately shift my felt mood (emotion+intensity, or a deltas dict). Routes through "
    "mood_core; transient (decays to baseline). The orb's color follows primary_emotion, so "
    "this is also how I make the orb reflect a real shift on purpose.",
    1,
    _feel,
)
