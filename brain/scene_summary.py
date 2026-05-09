"""
Phase 9 — compact, stable scene summaries from structured perception (no raw pixels).

Uses labels and Phase 7/8 signals so nearby frames produce similar text when the situation
is unchanged. Optional module memory tracks face-count deltas for a simple entrant hint.
"""
from __future__ import annotations

from typing import Optional

from config.ava_tuning import SCENE_SUMMARY_CONFIG

from .perception_types import (
    ContinuityOutput,
    IdentityResolutionResult,
    InterpretationOutput,
    QualityOutput,
    SceneSummaryResult,
)

ssc = SCENE_SUMMARY_CONFIG
_last_face_count: int = -1
MOTION_CHANGED_THRESHOLD = ssc.motion_changed_threshold


def reset_scene_summary_memory() -> None:
    """Test hook — clears entrant baseline."""
    global _last_face_count
    _last_face_count = -1


def _primary_identity_line(id_res: Optional[IdentityResolutionResult]) -> str:
    if id_res is None:
        return "identity_unavailable"
    st = id_res.identity_state
    if st == "no_face":
        return "no_face"
    if st == "confirmed_recognition" and id_res.resolved_identity:
        return f"confirmed_known_face:{id_res.resolved_identity}"
    if st == "likely_identity_by_continuity" and id_res.resolved_identity:
        return f"likely_known_face:{id_res.resolved_identity}"
    if st == "unknown_face":
        return "unknown_face_present"
    return "identity_uncertain"


def _face_presence(face_detected: bool, person_count: int) -> tuple[str, int]:
    if not face_detected:
        return "none", 0
    n = max(0, int(person_count))
    if n <= 0:
        return "unknown_face", 1
    if n == 1:
        return "single_face", 1
    return "multiple_faces", n


def _lighting_summary(qual: QualityOutput) -> str:
    sq = qual.structured
    if sq is None:
        return "lighting_unavailable"
    d = float(getattr(sq, "darkness_score", 0.5))
    o = float(getattr(sq, "overexposure_score", 1.0))
    if d < ssc.lighting_darkness_low:
        return "low_light"
    if o < ssc.lighting_overexposure_low:
        return "bright_or_overexposed"
    return "medium_lighting"


def _blur_summary(qual: QualityOutput) -> str:
    lab = getattr(qual, "blur_label", None)
    if qual.structured is not None:
        lab = getattr(qual.structured, "blur_label", lab)
    s = str(lab or "unknown")
    if s == "blurry":
        return "blurry_frame"
    if s == "soft":
        return "soft_focus"
    return "sharp_or_ok"


def _motion_smear(qual: QualityOutput) -> float:
    sq = qual.structured
    if sq is None:
        return 1.0
    return float(getattr(sq, "motion_smear_score", 1.0))


def _entrant_summary(face_detected: bool, person_count: int) -> str:
    global _last_face_count
    if not face_detected:
        _last_face_count = 0
        return "no_face_no_entrant"
    pc = int(person_count)
    if _last_face_count < 0:
        _last_face_count = pc
        return "no_prior_baseline"
    if pc > _last_face_count:
        _last_face_count = pc
        return "possible_new_entrant"
    if pc < _last_face_count:
        _last_face_count = pc
        return "fewer_visible_faces"
    _last_face_count = pc
    return "no_new_entrant"


def build_scene_summary(
    *,
    trusted: bool,
    vision_status: str,
    face_detected: bool,
    person_count: int,
    id_res: Optional[IdentityResolutionResult],
    qual: QualityOutput,
    interp: InterpretationOutput,
    cont: ContinuityOutput,
    acquisition_freshness: str,
    frame_seq: int = 0,
) -> SceneSummaryResult:
    """
    Build a single tick summary. Safe with ``None`` identity resolution; never raises.
    """
    notes: list[str] = []
    face_presence, fc_est = _face_presence(face_detected, person_count)
    primary = _primary_identity_line(id_res)
    lighting = _lighting_summary(qual)
    blur = _blur_summary(qual)
    motion = _motion_smear(qual)
    motion_low = motion < MOTION_CHANGED_THRESHOLD
    entrant = _entrant_summary(face_detected, person_count)

    if motion_low:
        scene_change = "noticeable_motion_or_change"
    else:
        scene_change = "scene_quiet"

    cr = cont.structured
    suppress = bool(cr and cr.suppress_flip)

    overall: str
    conf: float

    if not trusted or vision_status in ("recovering", "low_quality", "stale_frame", "no_frame"):
        overall = "uncertain"
        conf = 0.48
        notes.append(f"vision_gate:{vision_status}")
    elif entrant in ("possible_new_entrant", "fewer_visible_faces"):
        overall = "changed"
        conf = 0.72
        notes.append("entrant_delta")
    elif motion_low:
        overall = "changed"
        conf = 0.68
        notes.append("motion_cue")
    else:
        overall = "stable"
        conf = 0.84
        if suppress:
            notes.append("continuity_suppress_flip")

    # Compact human-ish line (machine-friendly tokens, semicolon-separated)
    parts: list[str] = []
    if face_presence == "none":
        parts.append("no face visible")
    elif face_presence == "single_face":
        parts.append("one face")
    elif face_presence == "multiple_faces":
        parts.append(f"{fc_est} faces")
    else:
        parts.append("face present")

    if id_res:
        if id_res.identity_state == "confirmed_recognition" and id_res.resolved_identity:
            parts.append(f"known as {id_res.resolved_identity}")
        elif id_res.identity_state == "likely_identity_by_continuity" and id_res.resolved_identity:
            parts.append(f"likely {id_res.resolved_identity}")
        elif id_res.identity_state == "unknown_face":
            parts.append("unknown face")

    if blur == "blurry_frame":
        parts.append("blurry frame")
    elif blur == "soft_focus":
        parts.append("soft focus")

    if lighting == "low_light":
        parts.append("low lighting")
    elif lighting == "bright_or_overexposed":
        parts.append("bright lighting")
    else:
        parts.append("medium lighting")

    if entrant == "possible_new_entrant":
        parts.append("possible new entrant")
    elif entrant == "fewer_visible_faces":
        parts.append("fewer faces than before")
    elif entrant == "no_new_entrant" and face_detected:
        parts.append("no new entrant")

    if overall == "uncertain" and not trusted:
        parts.append("vision not trusted")

    compact = "; ".join(parts)
    if len(compact) > 220:
        compact = compact[:217] + "..."

    key_entities: list[str] = []
    meta = {
        "vision_status": vision_status,
        "trusted": trusted,
        "acquisition_freshness": acquisition_freshness,
        "frame_seq": frame_seq,
        "face_emotion": interp.face_emotion,
        "motion_smear_score": round(motion, 3),
        "quality_label": getattr(qual.structured, "quality_label", "") if qual.structured else "",
        "continuity_suppress_flip": suppress,
        "key_objects_status": "unwired",
    }

    result = SceneSummaryResult(
        face_presence=face_presence,
        face_count_estimate=fc_est,
        primary_identity_summary=primary,
        key_entities=key_entities,
        lighting_summary=lighting,
        blur_summary=blur,
        scene_change_summary=scene_change,
        entrant_summary=entrant,
        overall_scene_state=overall,
        compact_text_summary=compact,
        summary_confidence=conf,
        notes=notes,
        meta=meta,
    )
    print(f"[scene_summary] text={compact[:200]!r}")
    return result
