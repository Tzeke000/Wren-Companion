"""
Phase 10 — semantic interpretation layer: structured perception → event hypotheses.

Separate from (1) raw perception fields, (2) :class:`InterpretationOutput` emotion/salience,
and (3) user-facing language generation. Consumes scene summary, identity resolution, quality,
continuity hints, and salience/emotion for lightweight event tagging.
"""
from __future__ import annotations

import traceback
from typing import Any, Optional

from config.ava_tuning import INTERPRETATION_CONFIG, INTERPRETATION_EVENT_PRIORITY_ITEMS

from .perception_types import (
    ContinuityOutput,
    IdentityResolutionResult,
    InterpretationLayerResult,
    InterpretationOutput,
    QualityOutput,
    SceneSummaryResult,
)

icfg = INTERPRETATION_CONFIG
# Higher = wins as primary_event when multiple tags apply (:mod:`config.ava_tuning`)
_EVENT_PRIORITY: dict[str, float] = dict(INTERPRETATION_EVENT_PRIORITY_ITEMS)


def _pick_primary(events: list[str]) -> str:
    if not events:
        return "uncertain_visual_state"
    return max(events, key=lambda e: _EVENT_PRIORITY.get(e, icfg.default_primary_priority))


def build_interpretation_layer(
    *,
    trusted: bool,
    vision_status: str,
    user_text: str,
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    interp: InterpretationOutput,
    qual: QualityOutput,
    cont: ContinuityOutput,
) -> InterpretationLayerResult:
    """
    Derive semantic event tags for this tick. Safe on ``None`` upstream slices; never raises
    (falls back to ``uncertain_visual_state`` on unexpected errors).
    """
    try:
        return _build_interpretation_layer_inner(
            trusted=trusted,
            vision_status=vision_status,
            user_text=user_text,
            id_res=id_res,
            scene=scene,
            interp=interp,
            qual=qual,
            cont=cont,
        )
    except Exception as e:
        print(f"[interpretation] build failed: {e}\n{traceback.format_exc()}")
        return InterpretationLayerResult(
            event_types=["uncertain_visual_state"],
            event_confidence=icfg.error_fallback_confidence,
            event_priority=_EVENT_PRIORITY["uncertain_visual_state"],
            interpreted_subject=None,
            interpreted_identity=None,
            interpretation_notes=["interpretation_layer_error"],
            evidence={"error": str(e)},
            no_meaningful_change=False,
            primary_event="uncertain_visual_state",
        )


def _build_interpretation_layer_inner(
    *,
    trusted: bool,
    vision_status: str,
    user_text: str,
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    interp: InterpretationOutput,
    qual: QualityOutput,
    cont: ContinuityOutput,
) -> InterpretationLayerResult:
    sc = scene or SceneSummaryResult()
    ir = id_res or IdentityResolutionResult()
    events: list[str] = []
    notes: list[str] = []

    cr = cont.structured
    evidence: dict[str, Any] = {
        "vision_status": vision_status,
        "trusted": trusted,
        "scene_overall": sc.overall_scene_state,
        "scene_compact": (sc.compact_text_summary or "")[:160],
        "identity_state": ir.identity_state,
        "entrant_summary": sc.entrant_summary,
        "scene_change_summary": sc.scene_change_summary,
        "face_presence": sc.face_presence,
        "quality_label": getattr(qual.structured, "quality_label", "") if qual.structured else "",
        "blur_label": getattr(qual, "blur_label", ""),
        "face_emotion": interp.face_emotion,
        "salience_scalar": round(float(interp.salience or 0.0), 3),
        "continuity_suppress_flip": bool(cr and cr.suppress_flip),
        "interpretation_layer_version": 1,
    }

    if not trusted:
        events.append("uncertain_visual_state")
        notes.append("perception_untrusted")
        primary = "uncertain_visual_state"
        conf = 0.34
        il = InterpretationLayerResult(
            event_types=events,
            event_confidence=conf,
            event_priority=_EVENT_PRIORITY[primary],
            interpreted_subject=None,
            interpreted_identity=ir.resolved_identity,
            interpretation_notes=notes,
            evidence=evidence,
            no_meaningful_change=False,
            primary_event=primary,
        )
        _log_il(il)
        return il

    vision_bad = vision_status in ("recovering", "low_quality", "stale_frame", "no_frame")
    summary_uncertain = sc.overall_scene_state == "uncertain"

    if vision_bad or summary_uncertain:
        events.append("uncertain_visual_state")
        notes.append("vision_or_scene_uncertain")

    motion_spike = "noticeable" in (sc.scene_change_summary or "")
    ent = sc.entrant_summary or ""

    # Avoid high-commitment motion/entrant events when the scene summary is uncertain.
    if not vision_bad and not summary_uncertain:
        if ent == "possible_new_entrant":
            events.append("person_entered")
            notes.append("entrant_count_increased")
        elif ent == "fewer_visible_faces":
            events.append("person_left")
            notes.append("entrant_count_decreased")

        if sc.overall_scene_state == "changed" or motion_spike:
            events.append("scene_changed")
            notes.append("scene_change_or_motion_cue")

    face_here = sc.face_presence != "none"

    if face_here and not vision_bad and not summary_uncertain:
        if ir.identity_state == "confirmed_recognition" and ir.resolved_identity:
            events.append("known_person_present")
        elif ir.identity_state == "likely_identity_by_continuity" and ir.resolved_identity:
            events.append("likely_known_person_present")
        elif ir.identity_state == "unknown_face":
            events.append("unknown_person_present")

        em = (interp.face_emotion or "").lower()
        high_arousal = em in ("angry", "fear", "disgust", "surprised", "happy", "excited")
        sal = float(interp.salience or 0.2)
        ut = (user_text or "").strip()
        if high_arousal or sal >= 0.75 or (bool(ut) and sal >= 0.52):
            events.append("user_or_subject_engaged")
            notes.append("emotion_or_salience_or_text_engagement")
        elif sal <= 0.38 and em in ("neutral", "calm", "", "none"):
            events.append("user_or_subject_disengaged")
            notes.append("low_salience_neutral_emotion")

        if sc.blur_summary in ("blurry_frame", "soft_focus") and motion_spike:
            events.append("occupied_or_busy_visual_state")
            notes.append("blur_plus_motion_busy_visual")

    elif not face_here and sc.face_presence == "none" and not vision_bad and not summary_uncertain:
        notes.append("no_face_visible")

    # Dedupe, preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for e in events:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    events = uniq

    no_meaningful = (
        trusted
        and "uncertain_visual_state" not in events
        and "person_entered" not in events
        and "person_left" not in events
        and "scene_changed" not in events
        and sc.overall_scene_state == "stable"
        and ent in ("no_new_entrant", "no_prior_baseline", "no_face_no_entrant")
        and not motion_spike
    )
    if no_meaningful and "no_meaningful_change" not in events:
        events.append("no_meaningful_change")

    if not events:
        events = ["uncertain_visual_state"]
        notes.append("no_events_fallback")

    primary = _pick_primary(events)

    base = 0.52 * float(sc.summary_confidence) + 0.48 * max(0.32, float(ir.identity_confidence))
    qlab = getattr(qual.structured, "quality_label", "") if qual.structured else ""
    if qlab == "unreliable":
        base *= 0.62
        notes.append("quality_unreliable_penalty")
    if str(getattr(qual, "blur_label", "") or "") == "blurry":
        base *= 0.86
        notes.append("blur_penalty")
    if "uncertain_visual_state" in events:
        base = min(base, 0.5)
    conf = float(max(0.14, min(0.94, base)))

    subj: Optional[str] = None
    if ir.resolved_identity:
        subj = f"person:{ir.resolved_identity}"
    elif face_here:
        subj = "person:unresolved"
    else:
        subj = None

    il = InterpretationLayerResult(
        event_types=events,
        event_confidence=conf,
        event_priority=_EVENT_PRIORITY.get(primary, 0.5),
        interpreted_subject=subj,
        interpreted_identity=ir.resolved_identity,
        interpretation_notes=notes,
        evidence=evidence,
        no_meaningful_change=no_meaningful and "uncertain_visual_state" not in events,
        primary_event=primary,
    )
    _log_il(il)
    return il


def _log_il(il: InterpretationLayerResult) -> None:
    ev = ",".join(il.event_types[:8])
    print(f"[interpretation] events=[{ev}] primary={il.primary_event!r} conf={il.event_confidence:.2f}")
