"""
Phase 18 — bounded philosophical/internal contemplation.

Builds evidence-aware internal reasoning themes and soft priority weights without
overriding runtime behavior, safety gates, or approval boundaries.
"""
from __future__ import annotations

from typing import Optional

from config.ava_tuning import CONTEMPLATION_CONFIG

from .perception_types import (
    ContemplationResult,
    InternalPriorityView,
    MemoryImportanceResult,
    PatternLearningResult,
    PerceptionMemoryOutput,
    ProactiveTriggerResult,
    ReflectionResult,
    SelfTestRunResult,
    WorkbenchProposalResult,
)

cfc = CONTEMPLATION_CONFIG

_ADAPT_REPAIR_BLOCKED = frozenset({"successful_repair_reflection", "blocked_execution_reflection"})


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def build_contemplation_result(
    *,
    reflection: Optional[ReflectionResult],
    memory_importance: Optional[MemoryImportanceResult],
    pattern_learning: Optional[PatternLearningResult],
    perception_memory: Optional[PerceptionMemoryOutput],
    proactive_trigger: Optional[ProactiveTriggerResult],
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    visual_truth_trusted: bool,
    acquisition_freshness: str,
) -> ContemplationResult:
    """Produce bounded internal contemplation from structured runtime evidence."""
    rf = reflection or ReflectionResult()
    mi = memory_importance or MemoryImportanceResult()
    pl = pattern_learning or PatternLearningResult()
    pm = perception_memory or PerceptionMemoryOutput()
    pt = proactive_trigger or ProactiveTriggerResult()
    st = selftests or SelfTestRunResult()
    wb = workbench or WorkbenchProposalResult()

    failed_n = len(st.summary.failed_checks or [])
    warning_n = len(st.summary.warning_checks or [])
    importance = float(mi.decision.importance_score)
    unusual = float(pl.primary_signal.unusualness_score if pl.primary_signal else 0.0)
    familiar = float(pl.primary_signal.familiarity_score if pl.primary_signal else 0.0)
    has_event = pm.event is not None
    has_workbench_action = bool(wb.has_proposal and wb.top_proposal.proposal_type != "no_action_needed")

    theme = "certainty_vs_usefulness"
    summary = "Current evidence suggests balancing usefulness with uncertainty-aware caution."
    question = "What level of certainty is sufficient to be helpful without overclaiming?"
    position = "When uncertainty is elevated, observation and clarification are preferable to decisive intervention."
    principles = [
        "Ground conclusions in structured evidence.",
        "Prefer conservative confidence when signals conflict.",
        "Do not convert soft patterns into hard assumptions.",
    ]
    caution = []
    confidence = cfc.default_confidence

    if failed_n > 0 or has_workbench_action:
        theme = "maintenance_vs_growth"
        summary = "Current evidence suggests maintenance should take precedence over growth-oriented changes."
        question = "How can stability be restored before adding new complexity?"
        position = "Prioritize system reliability and supervised fixes before expanding behavior scope."
        confidence = cfc.maintenance_confidence
        principles = [
            "Stability before expansion.",
            "Repairs remain supervised and reviewable.",
            "Avoid autonomous escalation during degraded operation.",
        ]
    elif (
        rf.reflection_category in {"stable_baseline_reflection", "successful_operation_reflection"}
        and familiar >= cfc.continuity_familiarity_min
    ):
        theme = "continuity_of_self"
        summary = "Continuity currently depends on stable perception, memory scoring, and reflection alignment."
        question = "How can continuity remain coherent when context changes gradually?"
        position = "Sustained consistency should be favored when baseline signals remain stable."
        confidence = cfc.continuity_theme_confidence
        principles = [
            "Maintain coherent state transitions across ticks.",
            "Use soft self-model tags rather than rigid identity claims.",
            "Treat continuity as operational consistency, not certainty of essence.",
        ]
    elif not visual_truth_trusted or acquisition_freshness in ("stale", "unavailable"):
        theme = "observation_vs_intervention"
        summary = "Current evidence suggests observation is preferable to intervention while visual certainty is low."
        question = "What minimum evidence should be present before engaging proactively?"
        position = "When sensing confidence is weak, prioritize monitoring and defer strong interventions."
        confidence = cfc.observation_theme_confidence
        principles = [
            "Low-certainty states call for restraint.",
            "Interventions should be proportional to evidence quality.",
            "Maintain transparency about uncertainty.",
        ]
        caution.append("vision_uncertainty_present")
    elif importance >= cfc.significance_importance_min and has_event:
        theme = "significance_of_events"
        summary = "Higher-importance events likely contribute more to meaningful continuity than routine noise."
        question = "Which events meaningfully change context versus merely repeat baseline state?"
        position = "Weight notable transitions more than repetitive no-change observations."
        confidence = cfc.significance_confidence
        principles = [
            "Not all events deserve equal memory weight.",
            "Transitions often carry more meaning than static states.",
            "Keep weighting conservative and revisable.",
        ]
    elif unusual >= cfc.unusual_theme_min:
        theme = "relationship_to_user_context"
        summary = "Unusual context may matter, but interpretation should remain cautious and user-centered."
        question = "How can unusual context be acknowledged without overinterpreting intent?"
        position = "Treat unusual patterns as prompts for careful attention, not firm conclusions."
        confidence = cfc.unusual_theme_confidence
        principles = [
            "User context should guide relevance.",
            "Unusual does not imply urgent by default.",
            "Prefer clarifying engagement over assumption.",
        ]
    elif warning_n >= cfc.warning_repeat_min:
        theme = "consistency_of_behavior"
        summary = "Repeated warnings suggest behavior should emphasize consistency and guardrails."
        question = "How can operational consistency be preserved while warnings persist?"
        position = "Prioritize predictable, guarded behavior until warning pressure declines."
        confidence = cfc.consistency_theme_confidence
        principles = [
            "Consistency protects against cascading errors.",
            "Warnings should influence caution, not panic.",
            "Adjustments remain supervised.",
        ]
    elif pt.should_trigger is False and pt.suppression_reason:
        theme = "certainty_vs_usefulness"
        summary = "Trigger suppression indicates current usefulness may come from restraint rather than action."
        question = "When does silence provide more value than intervention?"
        position = "Suppression reasons are evidence that useful behavior can include not acting."
        confidence = cfc.suppression_theme_confidence
        principles = [
            "Usefulness includes deliberate non-intervention.",
            "Suppressions should inform pacing, not be ignored.",
            "Confidence should shape interaction intensity.",
        ]

    # Soft internal priorities (guidance only; no direct behavior override).
    maintain = _clamp01(
        cfc.priority_maintain_base
        + cfc.priority_maintain_failed_scale * failed_n
        + (cfc.priority_maintain_workbench_bonus if has_workbench_action else 0.0)
    )
    observe = _clamp01(
        cfc.priority_observe_base
        + (cfc.priority_observe_untrusted_vision_bonus if not visual_truth_trusted else 0.0)
        + (
            cfc.priority_observe_unusual_mid_bonus
            if unusual > cfc.priority_observe_unusual_mid_threshold
            else 0.0
        )
    )
    clarify = _clamp01(
        cfc.priority_clarify_base
        + (
            cfc.priority_clarify_unusual_high_bonus
            if unusual > cfc.priority_clarify_unusual_high_threshold
            else 0.0
        )
        + (cfc.priority_clarify_warning_bonus if warning_n > 0 else 0.0)
    )
    remember = _clamp01(
        cfc.priority_remember_base
        + cfc.priority_remember_importance_scale * importance
        + (cfc.priority_remember_event_bonus if has_event else 0.0)
    )
    adapt = _clamp01(
        cfc.priority_adapt_base
        + (cfc.priority_adapt_warning_repeat_bonus if warning_n >= cfc.warning_repeat_min else 0.0)
        + (
            cfc.priority_adapt_repair_blocked_bonus
            if rf.reflection_category in _ADAPT_REPAIR_BLOCKED
            else 0.0
        )
    )
    engage = _clamp01(
        cfc.priority_engage_base
        + (cfc.priority_engage_trigger_bonus if pt.should_trigger else 0.0)
        - (cfc.priority_engage_suppression_penalty if pt.suppression_reason else 0.0)
    )
    remain_silent = _clamp01(
        cfc.priority_silent_base
        + (cfc.priority_silent_suppression_bonus if pt.suppression_reason else 0.0)
        + (cfc.priority_silent_untrusted_vision_bonus if not visual_truth_trusted else 0.0)
    )
    if failed_n > 0:
        engage = _clamp01(engage - cfc.priority_failed_engage_penalty)
        remain_silent = _clamp01(remain_silent + cfc.priority_failed_silent_bonus)

    priorities = InternalPriorityView(
        observe=observe,
        clarify=clarify,
        remember=remember,
        adapt=adapt,
        maintain=maintain,
        engage=engage,
        remain_silent=remain_silent,
        meta={"bounded_guidance_only": True},
    )

    if confidence < cfc.low_confidence_threshold:
        caution.append("low_confidence_contemplation")
    caution.append("no_autonomous_override")
    evidence = {
        "reflection_category": rf.reflection_category,
        "reflection_confidence": float(rf.confidence),
        "self_model_tags": list(rf.self_model.self_model_tags),
        "memory_importance_score": importance,
        "pattern_unusualness": unusual,
        "pattern_familiarity": familiar,
        "proactive_should_trigger": bool(pt.should_trigger),
        "proactive_suppression_reason": pt.suppression_reason,
        "selftest_status": st.summary.overall_status,
        "failed_count": failed_n,
        "warning_count": warning_n,
        "workbench_top_proposal": wb.top_proposal.proposal_type if wb.top_proposal else "no_action_needed",
        "visual_truth_trusted": bool(visual_truth_trusted),
        "acquisition_freshness": acquisition_freshness,
    }
    result = ContemplationResult(
        contemplation_theme=theme,
        contemplation_summary=summary,
        contemplation_question=question,
        contemplation_position=position,
        contemplation_confidence=_clamp01(confidence),
        guiding_principles=principles,
        priority_weights=priorities,
        caution_notes=caution,
        evidence_basis=evidence,
        notes=["bounded_internal_reasoning", "evidence_aware"],
        meta={"phase": "18", "non_override": True},
    )
    print(
        f"[contemplation] theme={result.contemplation_theme} "
        f"conf={result.contemplation_confidence:.2f} "
        f"summary={result.contemplation_summary[:80]}"
    )
    print(
        "[contemplation] priorities="
        f"observe:{observe:.2f},clarify:{clarify:.2f},remember:{remember:.2f},"
        f"adapt:{adapt:.2f},maintain:{maintain:.2f},engage:{engage:.2f},silent:{remain_silent:.2f}"
    )
    return result
