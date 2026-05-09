"""
Phase 11 — memory-ready perception event records (generation only; no persistence).

Derives compact events from interpretation + scene summary + identity resolution.
Includes a lightweight duplicate guard for repeated ``no_meaningful_change`` ticks.
"""
from __future__ import annotations

import traceback
from typing import Any, Optional

from config.ava_tuning import MEMORY_EVENT_CONFIG, MEMORY_EVENT_TYPES

from .perception_types import (
    ContinuityOutput,
    IdentityResolutionResult,
    InterpretationLayerResult,
    InterpretationOutput,
    PerceptionMemoryEvent,
    PerceptionMemoryOutput,
    QualityOutput,
    SceneSummaryResult,
)

mecfg = MEMORY_EVENT_CONFIG
_MEMORY_EVENT_TYPES = MEMORY_EVENT_TYPES

_last_no_change_sig: Optional[tuple[Any, ...]] = None
_had_face_prev: bool = False


def reset_perception_memory_guard() -> None:
    """Test / session reset for duplicate and room-empty baselines."""
    global _last_no_change_sig, _had_face_prev
    _last_no_change_sig = None
    _had_face_prev = False


def _map_memory_event_type(
    il: InterpretationLayerResult, id_res: IdentityResolutionResult
) -> str:
    p = il.primary_event
    if p in _MEMORY_EVENT_TYPES:
        return p
    if p == "user_or_subject_engaged":
        return "known_person_present" if id_res.resolved_identity else "no_meaningful_change"
    if p == "user_or_subject_disengaged":
        return "no_meaningful_change"
    if p == "person_entered" or p == "person_left":
        return p
    return "uncertain_visual_state"


def _salience_top(interp: InterpretationOutput) -> str:
    sr = interp.salience_structured
    if not sr or not sr.items:
        return ""
    top = next((x for x in sr.items if x.is_top), None)
    if top is None:
        top = sr.items[0]
    return f"{top.item_type}:{top.label}"


def build_perception_memory_output(
    *,
    wall_time: float,
    frame_seq: int,
    trusted: bool,
    acquisition_freshness: str,
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    il: Optional[InterpretationLayerResult],
    qual: QualityOutput,
    interp: InterpretationOutput,
    cont: ContinuityOutput,
) -> PerceptionMemoryOutput:
    """
    Produce at most one primary memory event for this tick. Never writes storage; never raises.
    """
    try:
        return _build_perception_memory_output_inner(
            wall_time=wall_time,
            frame_seq=frame_seq,
            trusted=trusted,
            acquisition_freshness=acquisition_freshness,
            id_res=id_res,
            scene=scene,
            il=il,
            qual=qual,
            interp=interp,
            cont=cont,
        )
    except Exception as e:
        print(f"[perception_memory] build failed: {e}\n{traceback.format_exc()}")
        ev = PerceptionMemoryEvent(
            wall_time=wall_time,
            frame_seq=frame_seq,
            event_type="uncertain_visual_state",
            event_confidence=mecfg.error_event_confidence,
            event_priority=mecfg.error_event_priority,
            identity_state="",
            evidence={"error": str(e)},
            memory_worthy_candidate=False,
            notes=["perception_memory_error"],
            interpretation_primary_event="uncertain_visual_state",
        )
        return PerceptionMemoryOutput(event=ev, skipped=False, skip_reason="")


def _build_perception_memory_output_inner(
    *,
    wall_time: float,
    frame_seq: int,
    trusted: bool,
    acquisition_freshness: str,
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    il: Optional[InterpretationLayerResult],
    qual: QualityOutput,
    interp: InterpretationOutput,
    cont: ContinuityOutput,
) -> PerceptionMemoryOutput:
    global _last_no_change_sig, _had_face_prev

    ir = id_res or IdentityResolutionResult()
    sc = scene or SceneSummaryResult()
    layer = il or InterpretationLayerResult()

    face_here = sc.face_presence != "none"
    mem_type = _map_memory_event_type(layer, ir)

    # Room became empty: no face this tick after we had a face last tick (trusted path).
    if (
        trusted
        and not face_here
        and _had_face_prev
        and mem_type not in ("person_left", "person_entered", "scene_changed")
    ):
        mem_type = "room_became_empty"

    _had_face_prev = face_here

    # Duplicate guard: identical stable no-change signature → skip emitting a new record.
    if mem_type == "no_meaningful_change":
        sig = (
            mem_type,
            ir.resolved_identity,
            sc.face_presence,
            sc.overall_scene_state,
            ir.identity_state,
        )
        if sig == _last_no_change_sig:
            print(
                "[perception_memory] event=no_meaningful_change worthy=False conf=0.00 (duplicate_suppressed)"
            )
            return PerceptionMemoryOutput(
                event=None, skipped=True, skip_reason="duplicate_no_meaningful_change"
            )
        _last_no_change_sig = sig
    else:
        _last_no_change_sig = None

    worthy = mem_type not in ("no_meaningful_change",)
    if mem_type == "uncertain_visual_state":
        worthy = True

    cr = cont.structured
    evidence: dict[str, Any] = {
        "trusted": trusted,
        "acquisition_freshness": acquisition_freshness,
        "scene_overall": sc.overall_scene_state,
        "scene_entrant": sc.entrant_summary,
        "identity_state": ir.identity_state,
        "quality_label": getattr(qual.structured, "quality_label", "") if qual.structured else "",
        "blur_label": str(getattr(qual, "blur_label", "") or ""),
        "salience_top": _salience_top(interp),
        "continuity_suppress_flip": bool(cr and cr.suppress_flip),
        "continuity_prior": cr.prior_identity if cr else None,
        "interpretation_events": list(layer.event_types[:12]),
    }

    snippet = (sc.compact_text_summary or "")[:200]
    notes = list(layer.interpretation_notes[:6])
    if mem_type == "room_became_empty":
        notes.append("derived_no_face_after_prior_face")

    evt = PerceptionMemoryEvent(
        wall_time=float(wall_time),
        frame_seq=int(frame_seq),
        event_type=mem_type,
        event_confidence=float(layer.event_confidence),
        event_priority=float(layer.event_priority),
        identity_state=ir.identity_state,
        resolved_identity=ir.resolved_identity,
        stable_identity=ir.stable_identity,
        relevant_entities=list(sc.key_entities or []),
        scene_summary_snippet=snippet,
        interpretation_primary_event=layer.primary_event,
        evidence=evidence,
        memory_worthy_candidate=worthy,
        notes=notes,
        suppressed_duplicate=False,
    )

    print(
        f"[perception_memory] event={evt.event_type} worthy={evt.memory_worthy_candidate} "
        f"conf={evt.event_confidence:.2f}"
    )
    return PerceptionMemoryOutput(event=evt, skipped=False, skip_reason="")
