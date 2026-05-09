from __future__ import annotations

from .perception import PerceptionState

_RELATIONAL_FLOAT_KEYS = (
    "loneliness",
    "engagement",
    "warmth",
    "care",
    "concern",
    "caution",
    "support_drive",
)


def process_visual_emotion(perception: PerceptionState, current_mood: dict) -> dict:
    """
    Takes what the camera sees and nudges Ava mood weights.
    Called every camera tick. Never raises.
    Returns updated mood dict with relational values clamped 0.0-1.0.
    """
    mood = dict(current_mood)

    try:
        if not perception.face_detected:
            mood["loneliness"] = min(1.0, float(mood.get("loneliness", 0.0)) + 0.05)
            mood["engagement"] = max(0.0, float(mood.get("engagement", 0.5)) - 0.08)
        else:
            mood["loneliness"] = max(0.0, float(mood.get("loneliness", 0.0)) - 0.05)
            mood["engagement"] = min(1.0, float(mood.get("engagement", 0.5)) + 0.06)

            emotion = (perception.face_emotion or "neutral").lower()

            if emotion in ("happy", "surprise"):
                mood["warmth"] = min(1.0, float(mood.get("warmth", 0.5)) + 0.05)
                mood["care"] = min(1.0, float(mood.get("care", 0.5)) + 0.03)

            elif emotion in ("angry", "disgust", "fear"):
                mood["concern"] = min(1.0, float(mood.get("concern", 0.0)) + 0.08)
                mood["caution"] = min(1.0, float(mood.get("caution", 0.0)) + 0.06)
                mood["warmth"] = max(0.0, float(mood.get("warmth", 0.5)) - 0.03)

            elif emotion == "sad":
                mood["care"] = min(1.0, float(mood.get("care", 0.5)) + 0.07)
                mood["support_drive"] = min(1.0, float(mood.get("support_drive", 0.0)) + 0.08)

        for k in _RELATIONAL_FLOAT_KEYS:
            if k in mood and isinstance(mood[k], (int, float)):
                mood[k] = max(0.0, min(1.0, float(mood[k])))

        bm = dict(mood.get("behavior_modifiers") or {})
        if "warmth" in mood:
            bm["warmth"] = round(max(0.0, min(1.0, float(mood["warmth"]))), 3)
        if "engagement" in mood:
            bm["initiative"] = round(max(0.0, min(1.0, float(mood["engagement"]))), 3)
        if "caution" in mood:
            bm["caution"] = round(max(0.0, min(1.0, float(mood["caution"]))), 3)
        mood["behavior_modifiers"] = bm
    except Exception:
        pass

    return mood
