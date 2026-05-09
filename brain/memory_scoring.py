"""
Phase 12 — memory importance scoring (no persistence side effects).

Scores Phase 11 perception-memory events with conservative additive factors so later
phases can decide storage strategy without changing runtime behavior today.
"""
from __future__ import annotations

import traceback
from typing import Any, Optional

from config.ava_tuning import (
    MEMORY_SCORING_CLASS_ITEMS,
    MEMORY_SCORING_BASE_WEIGHTS_ITEMS,
    MEMORY_SCORING_CONFIG,
)

from .perception_types import (
    ContinuityOutput,
    IdentityResolutionResult,
    InterpretationLayerResult,
    MemoryDecisionResult,
    MemoryImportanceResult,
    PerceptionMemoryOutput,
    QualityOutput,
    SceneSummaryResult,
)

msc = MEMORY_SCORING_CONFIG
_BASE_EVENT_WEIGHTS: dict[str, float] = dict(MEMORY_SCORING_BASE_WEIGHTS_ITEMS)
_EVENT_MEMORY_CLASS: dict[str, str] = dict(MEMORY_SCORING_CLASS_ITEMS)

_last_event_signature: Optional[tuple[Any, ...]] = None
_signature_repeat_count: int = 0

_IGNORE_MAX = msc.ignore_label_max
_LOW_MAX = msc.low_label_max
_MEDIUM_MAX = msc.medium_label_max


def reset_memory_scoring_guard() -> None:
    """Reset duplicate/repetition suppression state (tests/session restarts)."""
    global _last_event_signature, _signature_repeat_count
    _last_event_signature = None
    _signature_repeat_count = 0


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _label_for_score(score: float) -> str:
    if score <= _IGNORE_MAX:
        return "ignore"
    if score <= _LOW_MAX:
        return "low"
    if score <= _MEDIUM_MAX:
        return "medium"
    return "high"


def _repetition_penalty(event_type: str, event: Any) -> tuple[float, dict[str, Any]]:
    global _last_event_signature, _signature_repeat_count

    sig = (
        event_type,
        getattr(event, "resolved_identity", None),
        getattr(event, "identity_state", ""),
        bool(getattr(event, "suppressed_duplicate", False)),
        str(getattr(event, "scene_summary_snippet", "")[: msc.sig_snippet_len]),
    )
    if sig == _last_event_signature:
        _signature_repeat_count += 1
    else:
        _last_event_signature = sig
        _signature_repeat_count = 0

    penalty = 0.0
    if event_type == "no_meaningful_change":
        penalty += msc.repetition_no_change_penalty
    if _signature_repeat_count > 0:
        penalty += min(
            msc.repetition_per_hit_cap,
            msc.repetition_per_hit_scale * float(_signature_repeat_count),
        )
    if getattr(event, "suppressed_duplicate", False):
        penalty += msc.repetition_suppressed_duplicate
    return penalty, {"repeat_count": _signature_repeat_count, "signature": str(sig[:4])}


def score_memory_importance(
    *,
    perception_memory: Optional[PerceptionMemoryOutput],
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    il: Optional[InterpretationLayerResult],
    qual: QualityOutput,
    cont: ContinuityOutput,
    acquisition_freshness: str,
) -> MemoryImportanceResult:
    """
    Score memory importance from Phase 11 perception-memory output + structured context.
    Never writes persistence and never raises.
    """
    try:
        return _score_memory_importance_inner(
            perception_memory=perception_memory,
            id_res=id_res,
            scene=scene,
            il=il,
            qual=qual,
            cont=cont,
            acquisition_freshness=acquisition_freshness,
        )
    except Exception as e:
        print(f"[memory_scoring] failed: {e}\n{traceback.format_exc()}")
        decision = MemoryDecisionResult(
            event_type="",
            importance_score=0.0,
            importance_label="ignore",
            memory_worthy=False,
            memory_class="ignore",
            decision_reason="memory_scoring_error_fallback",
            novelty_score=0.0,
            relevance_score=0.0,
            uncertainty_penalty=msc.fallback_uncertainty_penalty,
            evidence={"error": str(e)},
            meta={"fallback": True},
        )
        return MemoryImportanceResult(decision=decision, skipped=True, skip_reason="scoring_error")


def _score_memory_importance_inner(
    *,
    perception_memory: Optional[PerceptionMemoryOutput],
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    il: Optional[InterpretationLayerResult],
    qual: QualityOutput,
    cont: ContinuityOutput,
    acquisition_freshness: str,
) -> MemoryImportanceResult:
    pm = perception_memory or PerceptionMemoryOutput(event=None, skipped=True, skip_reason="missing_memory_output")
    ir = id_res or IdentityResolutionResult()
    sc = scene or SceneSummaryResult()
    layer = il or InterpretationLayerResult()

    if pm.event is None:
        decision = MemoryDecisionResult(
            event_type="",
            importance_score=0.0,
            importance_label="ignore",
            memory_worthy=False,
            memory_class="ignore",
            decision_reason=pm.skip_reason or "no_memory_event",
            novelty_score=0.0,
            relevance_score=0.0,
            uncertainty_penalty=0.0,
            evidence={"perception_memory_skipped": bool(pm.skipped)},
            meta={"skip_reason": pm.skip_reason or ""},
        )
        print("[memory_scoring] event= score=0.00 label=ignore worthy=False (no_event)")
        return MemoryImportanceResult(
            decision=decision,
            skipped=True,
            skip_reason=pm.skip_reason or "no_memory_event",
        )

    ev = pm.event
    event_type = str(ev.event_type or "uncertain_visual_state")
    base_weight = _BASE_EVENT_WEIGHTS.get(event_type, msc.default_event_weight)

    # Additive factors (positive relevance/novelty + conservative penalties).
    novelty = float(layer.event_priority) * msc.novelty_layer_scale
    if event_type in ("person_entered", "person_left", "unknown_person_present", "room_became_empty"):
        novelty += msc.novelty_transition_bonus
    if sc.overall_scene_state == "changed":
        novelty += msc.novelty_scene_changed_bonus
    if sc.scene_change_summary:
        novelty += msc.novelty_scene_summary_bonus
    novelty = _clamp01(novelty)

    relevance = float(layer.event_confidence) * msc.relevance_layer_scale
    if ir.identity_state == "confirmed_recognition":
        relevance += msc.relevance_confirmed_bonus
    elif ir.identity_state == "likely_identity_by_continuity":
        relevance += msc.relevance_likely_bonus
    elif ir.identity_state == "unknown_face":
        relevance += msc.relevance_unknown_face_bonus
    if event_type == "known_person_present" and ir.resolved_identity:
        relevance += msc.relevance_known_present_bonus
    if event_type == "likely_known_person_present":
        relevance -= msc.relevance_likely_known_penalty
    if event_type == "unknown_person_present":
        relevance += msc.relevance_unknown_present_bonus
    relevance = _clamp01(relevance)

    uncertainty_penalty = 0.0
    if event_type == "uncertain_visual_state":
        uncertainty_penalty += msc.uncertainty_uncertain_event
    if not ev.memory_worthy_candidate:
        uncertainty_penalty += msc.uncertainty_not_worthy_candidate
    if layer.no_meaningful_change:
        uncertainty_penalty += msc.uncertainty_no_meaningful_change
    if sc.overall_scene_state == "uncertain":
        uncertainty_penalty += msc.uncertainty_scene_uncertain
    if acquisition_freshness in ("stale", "unavailable"):
        uncertainty_penalty += msc.uncertainty_stale_acquisition
    if cont.structured and cont.structured.suppress_flip:
        uncertainty_penalty += msc.uncertainty_suppress_flip

    quality_penalty = 0.0
    if qual.structured is not None:
        qlabel = str(getattr(qual.structured, "quality_label", "unreliable"))
    else:
        qlabel = "unreliable"
    blur_label = str(getattr(qual, "blur_label", "") or "")
    if qlabel == "unreliable":
        quality_penalty += msc.quality_unreliable
    elif qlabel == "weak":
        quality_penalty += msc.quality_weak
    if blur_label == "blurry":
        quality_penalty += msc.quality_blurry
    elif blur_label == "soft":
        quality_penalty += msc.quality_soft

    continuity_bonus = 0.0
    if cont.structured is not None:
        continuity_bonus += min(
            msc.continuity_bonus_cap,
            float(cont.structured.continuity_confidence) * msc.continuity_bonus_scale,
        )
        if cont.structured.suppress_flip:
            continuity_bonus += msc.continuity_suppress_flip_bonus

    repetition_penalty, rep_meta = _repetition_penalty(event_type, ev)

    raw_score = (
        base_weight
        + (msc.novelty_weight * novelty)
        + (msc.relevance_weight * relevance)
        + continuity_bonus
        - uncertainty_penalty
        - quality_penalty
        - repetition_penalty
    )
    score = _clamp01(raw_score)
    label = _label_for_score(score)
    memory_worthy = bool(score >= msc.worthy_score_min and label != "ignore")
    if event_type == "no_meaningful_change" and score < msc.no_meaningful_change_worthy_cap:
        memory_worthy = False

    memory_class = _EVENT_MEMORY_CLASS.get(event_type, "transient_context")
    if not memory_worthy:
        memory_class = "ignore"

    reason = (
        f"event={event_type}; base={base_weight:.2f}; novelty={novelty:.2f}; "
        f"relevance={relevance:.2f}; uncertainty_penalty={uncertainty_penalty:.2f}; "
        f"quality_penalty={quality_penalty:.2f}; repetition_penalty={repetition_penalty:.2f}"
    )
    decision = MemoryDecisionResult(
        event_type=event_type,
        importance_score=score,
        importance_label=label,
        memory_worthy=memory_worthy,
        memory_class=memory_class,
        decision_reason=reason,
        novelty_score=novelty,
        relevance_score=relevance,
        uncertainty_penalty=_clamp01(uncertainty_penalty + quality_penalty + repetition_penalty),
        evidence={
            "perception_memory_candidate": bool(ev.memory_worthy_candidate),
            "identity_state": ir.identity_state,
            "resolved_identity": ir.resolved_identity,
            "scene_overall_state": sc.overall_scene_state,
            "scene_change_summary": sc.scene_change_summary,
            "interpretation_primary_event": layer.primary_event,
            "interpretation_no_meaningful_change": bool(layer.no_meaningful_change),
            "continuity_confidence": float(cont.continuity_confidence),
            "acquisition_freshness": acquisition_freshness,
            "quality_label": qlabel,
            "blur_label": blur_label,
        },
        meta={
            "base_weight": base_weight,
            "continuity_bonus": continuity_bonus,
            "quality_penalty": quality_penalty,
            "repetition_penalty": repetition_penalty,
            "repeat_meta": rep_meta,
            "thresholds": {
                "ignore_max": _IGNORE_MAX,
                "low_max": _LOW_MAX,
                "medium_max": _MEDIUM_MAX,
                "worthy_min": msc.worthy_score_min,
            },
        },
    )
    print(
        f"[memory_scoring] event={event_type} score={score:.2f} "
        f"label={label} worthy={memory_worthy}"
    )
    return MemoryImportanceResult(decision=decision, skipped=False, skip_reason="")
