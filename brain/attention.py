from __future__ import annotations

from dataclasses import dataclass

from .perception import PerceptionState


@dataclass
class AttentionState:
    user_present: bool
    user_engaged: bool
    should_speak: bool
    suppression_reason: str


def compute_attention(
    perception: PerceptionState,
    seconds_since_last_message: float,
    circadian_initiative_scale: float = 1.0,
) -> AttentionState:
    """
    Decides whether Ava should speak right now based on what she sees.
    Called before choose_initiative_candidate().
    circadian_initiative_scale: lower at night — idle thresholds scale by 1/scale (more patient).
    """
    if not perception.face_detected:
        return AttentionState(False, False, False, "no_face_detected")

    em = (perception.face_emotion or "").lower()
    if em in ("angry", "disgust"):
        return AttentionState(True, True, False, "negative_expression_detected")

    scale = max(0.2, min(2.0, float(circadian_initiative_scale or 1.0)))
    idle_factor = 1.0 / scale
    absent_threshold = 1800.0 * idle_factor
    checkin_threshold = 300.0 * idle_factor

    # More than 30 min (scaled) — user probably left, stop initiating
    if seconds_since_last_message > absent_threshold:
        return AttentionState(True, False, False, "user_absent_long")

    # Idle window (scaled) with face present = good check-in opportunity
    if seconds_since_last_message > checkin_threshold:
        return AttentionState(True, False, True, "idle_checkin_opportunity")

    engaged = seconds_since_last_message < 120
    return AttentionState(True, engaged, engaged, "clear")
