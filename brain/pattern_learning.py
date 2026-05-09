"""
Phase 13 — lightweight pattern learning (no durable persistence side effects).

Builds conservative probabilistic pattern signals from structured perception-memory
and memory-importance decisions.
"""
from __future__ import annotations

import traceback
from typing import Optional

from config.ava_tuning import PATTERN_CONFIG

from .perception_types import (
    ContinuityOutput,
    IdentityResolutionResult,
    InterpretationLayerResult,
    MemoryImportanceResult,
    PatternLearningResult,
    PatternSignal,
    PerceptionMemoryOutput,
    SceneSummaryResult,
)

pcfg = PATTERN_CONFIG

_event_counts: dict[str, int] = {}
_identity_state_counts: dict[str, int] = {}
_resolved_identity_counts: dict[str, int] = {}
_scene_state_counts: dict[str, int] = {}
_face_presence_counts: dict[str, int] = {}
_transition_counts: dict[str, int] = {}
_last_event_type: str = ""
_total_ticks: int = 0


def reset_pattern_learning_state() -> None:
    """Reset in-process learning state (tests/session reset)."""
    global _event_counts, _identity_state_counts, _resolved_identity_counts
    global _scene_state_counts, _face_presence_counts, _transition_counts
    global _last_event_type, _total_ticks
    _event_counts = {}
    _identity_state_counts = {}
    _resolved_identity_counts = {}
    _scene_state_counts = {}
    _face_presence_counts = {}
    _transition_counts = {}
    _last_event_type = ""
    _total_ticks = 0


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _bump(counter: dict[str, int], key: str) -> int:
    k = str(key or "")
    counter[k] = int(counter.get(k, 0)) + 1
    return counter[k]


def _ratio(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return _clamp01(float(count) / float(total))


def _signal(
    *,
    pattern_type: str,
    subject: str,
    familiarity: float,
    unusualness: float,
    recurrence_count: int,
    transition: str,
    notes: list[str],
    mem_class: str = "pattern_candidate",
    min_detected_familiarity: float = PATTERN_CONFIG.pattern_default_min_familiarity,
    min_detected_unusualness: float = PATTERN_CONFIG.pattern_default_min_unusualness,
) -> PatternSignal:
    strength = _clamp01(
        (pcfg.strength_familiarity_weight * familiarity)
        + (pcfg.strength_unusualness_weight * unusualness)
    )
    detected = bool(familiarity >= min_detected_familiarity or unusualness >= min_detected_unusualness)
    recurrence_score = _clamp01(recurrence_count / pcfg.recurrence_score_divisor)
    candidate = bool(
        strength >= pcfg.candidate_strength_min
        and mem_class in ("pattern_candidate", "episodic_candidate")
    )
    return PatternSignal(
        pattern_detected=detected,
        pattern_type=pattern_type,
        pattern_subject=subject,
        pattern_strength=strength,
        familiarity_score=familiarity,
        unusualness_score=unusualness,
        recurrence_count=int(recurrence_count),
        recurrence_score=recurrence_score,
        recent_transition_pattern=transition,
        pattern_candidate=candidate,
        suggested_memory_class=mem_class if candidate else "ignore",
        notes=list(notes),
        meta={},
    )


def learn_pattern_signals(
    *,
    perception_memory: Optional[PerceptionMemoryOutput],
    memory_importance: Optional[MemoryImportanceResult],
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    il: Optional[InterpretationLayerResult],
    cont: ContinuityOutput,
    acquisition_freshness: str,
) -> PatternLearningResult:
    """Learn lightweight recurring pattern signals after memory scoring."""
    try:
        return _learn_pattern_signals_inner(
            perception_memory=perception_memory,
            memory_importance=memory_importance,
            id_res=id_res,
            scene=scene,
            il=il,
            cont=cont,
            acquisition_freshness=acquisition_freshness,
        )
    except Exception as e:
        print(f"[pattern_learning] failed: {e}\n{traceback.format_exc()}")
        fallback = PatternSignal(
            pattern_detected=False,
            pattern_type="unusual_event_pattern",
            pattern_subject="",
            pattern_strength=0.0,
            familiarity_score=0.0,
            unusualness_score=0.0,
            recurrence_count=0,
            recent_transition_pattern="",
            pattern_candidate=False,
            suggested_memory_class="ignore",
            notes=["pattern_learning_error_fallback"],
            meta={"error": str(e)},
        )
        return PatternLearningResult(
            primary_signal=fallback,
            signals=[fallback],
            skipped=True,
            skip_reason="pattern_learning_error",
            meta={"fallback": True},
        )


def _learn_pattern_signals_inner(
    *,
    perception_memory: Optional[PerceptionMemoryOutput],
    memory_importance: Optional[MemoryImportanceResult],
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    il: Optional[InterpretationLayerResult],
    cont: ContinuityOutput,
    acquisition_freshness: str,
) -> PatternLearningResult:
    global _last_event_type, _total_ticks
    pm = perception_memory or PerceptionMemoryOutput(event=None, skipped=True, skip_reason="missing_memory_output")
    mi = memory_importance or MemoryImportanceResult()
    ir = id_res or IdentityResolutionResult()
    sc = scene or SceneSummaryResult()
    layer = il or InterpretationLayerResult()
    event_type = (pm.event.event_type if pm.event is not None else "") or "no_event"
    transition = f"{_last_event_type or 'start'}->{event_type}"

    _total_ticks += 1
    total = max(1, _total_ticks)
    event_n = _bump(_event_counts, event_type)
    state_n = _bump(_identity_state_counts, ir.identity_state or "no_face")
    scene_n = _bump(_scene_state_counts, sc.overall_scene_state or "uncertain")
    face_n = _bump(_face_presence_counts, sc.face_presence or "unknown")
    ident_key = ir.resolved_identity or "(none)"
    ident_n = _bump(_resolved_identity_counts, ident_key)
    trans_n = _bump(_transition_counts, transition)

    event_familiarity = _ratio(event_n, total)
    transition_familiarity = _ratio(trans_n, total)
    transition_unusualness = _clamp01(1.0 - transition_familiarity)
    event_unusualness = _clamp01(1.0 - event_familiarity)

    # Conservative adjustments from scoring/trust context.
    if mi.decision.memory_worthy:
        event_unusualness = _clamp01(event_unusualness + pcfg.memory_worthy_unusualness_bump)
    if acquisition_freshness in ("stale", "unavailable"):
        event_unusualness = _clamp01(event_unusualness + pcfg.stale_acquisition_unusualness_bump)
    if cont.structured and cont.structured.suppress_flip:
        event_familiarity = _clamp01(event_familiarity + pcfg.suppress_flip_familiarity_bump)

    signals: list[PatternSignal] = []

    # identity_presence_pattern
    identity_subject = ir.resolved_identity or ir.identity_state or "unknown_identity"
    identity_familiarity = _ratio(ident_n, total) if ir.resolved_identity else _ratio(state_n, total)
    identity_unusualness = _clamp01(1.0 - identity_familiarity)
    signals.append(
        _signal(
            pattern_type="identity_presence_pattern",
            subject=identity_subject,
            familiarity=identity_familiarity,
            unusualness=identity_unusualness,
            recurrence_count=ident_n if ir.resolved_identity else state_n,
            transition=transition,
            notes=["identity_presence_recurrence_tracking"],
            mem_class="pattern_candidate",
        )
    )

    # scene_stability_pattern
    scene_familiarity = _ratio(scene_n, total)
    scene_unusualness = _clamp01(1.0 - scene_familiarity)
    signals.append(
        _signal(
            pattern_type="scene_stability_pattern",
            subject=sc.overall_scene_state or "uncertain",
            familiarity=scene_familiarity,
            unusualness=scene_unusualness,
            recurrence_count=scene_n,
            transition=transition,
            notes=["scene_overall_state_recurrence_tracking"],
        )
    )

    # event_transition_pattern
    signals.append(
        _signal(
            pattern_type="event_transition_pattern",
            subject=transition,
            familiarity=transition_familiarity,
            unusualness=transition_unusualness,
            recurrence_count=trans_n,
            transition=transition,
            notes=["event_transition_frequency_tracking"],
            mem_class="pattern_candidate",
            min_detected_familiarity=pcfg.transition_min_familiarity,
            min_detected_unusualness=pcfg.transition_min_unusualness,
        )
    )

    # engagement_pattern (lightweight from interpretation + face presence)
    engaged = bool(
        any(x in ("user_or_subject_engaged", "known_person_present", "likely_known_person_present") for x in (layer.event_types or []))
        or event_type in ("known_person_present", "likely_known_person_present", "occupied_or_busy_visual_state")
    )
    engagement_subject = "engaged_context" if engaged else "disengaged_or_idle_context"
    engaged_familiarity = event_familiarity if engaged else _ratio(face_n, total)
    engaged_unusualness = _clamp01(1.0 - engaged_familiarity)
    signals.append(
        _signal(
            pattern_type="engagement_pattern",
            subject=engagement_subject,
            familiarity=engaged_familiarity,
            unusualness=engaged_unusualness,
            recurrence_count=event_n,
            transition=transition,
            notes=["engagement_proxy_from_events"],
            mem_class="transient_context",
        )
    )

    # baseline_idle_pattern
    idle_now = bool(event_type == "no_meaningful_change" or sc.face_presence == "none")
    idle_subject = "idle_baseline" if idle_now else "active_baseline"
    idle_key = "idle_baseline" if idle_now else "active_baseline"
    idle_n = _bump(_event_counts, f"baseline:{idle_key}")
    idle_familiarity = _ratio(idle_n, total)
    idle_unusualness = _clamp01(1.0 - idle_familiarity)
    signals.append(
        _signal(
            pattern_type="baseline_idle_pattern",
            subject=idle_subject,
            familiarity=idle_familiarity,
            unusualness=idle_unusualness,
            recurrence_count=idle_n,
            transition=transition,
            notes=["idle_or_no_change_baseline_tracking"],
            mem_class="pattern_candidate",
            min_detected_familiarity=pcfg.baseline_idle_min_familiarity,
            min_detected_unusualness=pcfg.baseline_idle_min_unusualness,
        )
    )

    # unusual_event_pattern
    uncommon_event = event_type in ("unknown_person_present", "uncertain_visual_state")
    uncommon_event = uncommon_event or transition_unusualness > pcfg.uncommon_transition_unusualness
    unusual_bonus = pcfg.unusual_bonus if uncommon_event else 0.0
    unusual_signal = _signal(
        pattern_type="unusual_event_pattern",
        subject=event_type,
        familiarity=event_familiarity,
        unusualness=_clamp01(event_unusualness + unusual_bonus),
        recurrence_count=event_n,
        transition=transition,
        notes=["uncommon_event_or_transition_tracking"],
        mem_class="episodic_candidate" if uncommon_event else "pattern_candidate",
        min_detected_familiarity=pcfg.unusual_min_familiarity,
        min_detected_unusualness=pcfg.unusual_min_unusualness,
    )
    signals.append(unusual_signal)

    # Pick primary: highest strength, then unusualness.
    primary = sorted(
        signals,
        key=lambda s: (float(s.pattern_strength), float(s.unusualness_score), float(s.familiarity_score)),
        reverse=True,
    )[0]
    primary.meta = {
        "total_ticks": total,
        "event_type": event_type,
        "memory_worthy": bool(mi.decision.memory_worthy),
        "importance_score": float(mi.decision.importance_score),
        "importance_label": mi.decision.importance_label,
        "acquisition_freshness": acquisition_freshness,
    }
    _last_event_type = event_type

    print(
        f"[pattern_learning] type={primary.pattern_type} "
        f"familiar={primary.familiarity_score:.2f} unusual={primary.unusualness_score:.2f} "
        f"strength={primary.pattern_strength:.2f}"
    )
    return PatternLearningResult(
        primary_signal=primary,
        signals=signals,
        skipped=False,
        skip_reason="",
        meta={
            "event_type": event_type,
            "transition": transition,
            "transition_count": trans_n,
            "event_count": event_n,
            "total_ticks": total,
        },
    )
