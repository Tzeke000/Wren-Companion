"""
Phase 17 — reflection and self-model (evidence-based, bounded).

Produces grounded reflection outputs from structured runtime signals without
triggering direct actions or overriding safety gates.
"""
from __future__ import annotations

from typing import Any, Optional

from config.ava_tuning import REFLECTION_CONFIG

from .perception_types import (
    MemoryImportanceResult,
    PatternLearningResult,
    ProactiveTriggerResult,
    ReflectionObservation,
    ReflectionResult,
    SelfModelSnapshot,
    SelfTestRunResult,
    WorkbenchProposalResult,
)

rfc = REFLECTION_CONFIG


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _dict_like(x: Any) -> dict[str, Any]:
    if isinstance(x, dict):
        return x
    if hasattr(x, "__dict__"):
        return dict(getattr(x, "__dict__", {}) or {})
    return {}


def build_reflection_result(
    *,
    memory_importance: Optional[MemoryImportanceResult],
    pattern_learning: Optional[PatternLearningResult],
    proactive_trigger: Optional[ProactiveTriggerResult],
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    workbench_execution_result: Any = None,
    workbench_command_result: Any = None,
    visual_truth_trusted: bool = True,
    acquisition_freshness: str = "unavailable",
) -> ReflectionResult:
    """
    Build a bounded, evidence-based reflection result from structured signals.
    """
    mi = memory_importance or MemoryImportanceResult()
    pl = pattern_learning or PatternLearningResult()
    pt = proactive_trigger or ProactiveTriggerResult()
    st = selftests or SelfTestRunResult()
    wb = workbench or WorkbenchProposalResult()
    ex = _dict_like(workbench_execution_result)
    cmd = _dict_like(workbench_command_result)

    observations: list[ReflectionObservation] = []
    tags: set[str] = set()
    notes: list[str] = []
    category = "uncertain_state_reflection"
    outcome_quality = "mixed"
    detected_issue = ""
    detected_success = ""
    suggested_adjustment = ""
    recent_outcome = "No decisive operational outcome this tick."
    confidence = rfc.default_confidence

    failed_n = len(st.summary.failed_checks or [])
    warn_n = len(st.summary.warning_checks or [])
    has_proposal = bool(wb.has_proposal)
    prop_type = wb.top_proposal.proposal_type if wb.top_proposal else "no_action_needed"
    importance = float(mi.decision.importance_score)
    unusual = float(pl.primary_signal.unusualness_score if pl.primary_signal else 0.0)

    if visual_truth_trusted and acquisition_freshness in ("fresh", "aging"):
        tags.add("vision_stable")
    else:
        tags.add("vision_uncertain")
        observations.append(
            ReflectionObservation(
                source="perception",
                key="vision_status",
                value=f"trusted={visual_truth_trusted}, freshness={acquisition_freshness}",
                confidence=rfc.observation_confidence,
            )
        )

    if failed_n > 0:
        category = "failed_operation_reflection"
        outcome_quality = "poor"
        detected_issue = f"{failed_n} critical self-test check(s) failed."
        suggested_adjustment = "Prioritize diagnostics and approved remediation before expanding behavior."
        confidence = rfc.failed_category_confidence
        tags.update({"self_maintenance_needed", "repeated_warning_present"})
        observations.append(
            ReflectionObservation(
                source="selftests",
                key="failed_checks",
                value=", ".join(st.summary.failed_checks[:6]),
                evidence={"failed_checks": list(st.summary.failed_checks)},
                confidence=rfc.failed_checks_observation_confidence,
            )
        )
    elif warn_n >= rfc.warn_repeat_min:
        category = "repeated_warning_reflection"
        outcome_quality = "mixed"
        detected_issue = f"{warn_n} warning-level checks remain active."
        suggested_adjustment = "Track warning persistence and prefer low-risk review proposals."
        confidence = rfc.repeated_warning_category_confidence
        tags.update({"repeated_warning_present", "adaptation_in_progress"})
        observations.append(
            ReflectionObservation(
                source="selftests",
                key="warning_checks",
                value=", ".join(st.summary.warning_checks[:6]),
                evidence={"warning_checks": list(st.summary.warning_checks)},
                confidence=rfc.repeated_warning_observation_confidence,
            )
        )
    elif has_proposal and prop_type != "no_action_needed":
        category = "degraded_operation_reflection"
        outcome_quality = "mixed"
        detected_issue = f"Workbench proposal `{prop_type}` indicates unresolved maintenance work."
        suggested_adjustment = "Keep proposal review explicit; avoid unsupervised execution."
        confidence = rfc.workbench_category_confidence
        tags.add("self_maintenance_needed")
        observations.append(
            ReflectionObservation(
                source="workbench",
                key="top_proposal",
                value=prop_type,
                evidence={"priority": wb.top_proposal.priority, "risk": wb.top_proposal.risk_level},
                confidence=rfc.workbench_observation_confidence,
            )
        )
    elif ex.get("success") is True:
        category = "successful_repair_reflection"
        outcome_quality = "good"
        detected_success = "A supervised workbench execution completed successfully."
        suggested_adjustment = "Monitor post-change stability before additional edits."
        confidence = rfc.execution_success_confidence
        tags.update({"execution_capable_with_approval", "adaptation_in_progress"})
        observations.append(
            ReflectionObservation(
                source="workbench_execution",
                key="execution_success",
                value="true",
                evidence={"modified_files": ex.get("modified_files", []), "rollback_available": ex.get("rollback_available")},
                confidence=rfc.execution_success_observation_confidence,
            )
        )
    elif ex.get("blocked") is True or (cmd.get("blocked_reason") not in (None, "")):
        category = "blocked_execution_reflection"
        outcome_quality = "mixed"
        detected_issue = "A supervised execution attempt was blocked by safety or approval gates."
        suggested_adjustment = "Clarify approval scope and required evidence before retrying."
        confidence = rfc.blocked_confidence
        tags.update({"execution_capable_with_approval", "self_maintenance_needed"})
        observations.append(
            ReflectionObservation(
                source="workbench_execution",
                key="blocked_reason",
                value=str(ex.get("denial_reason") or cmd.get("blocked_reason") or "unknown"),
                confidence=rfc.blocked_observation_confidence,
            )
        )
    elif (
        pt.should_trigger
        and importance >= rfc.proactive_success_importance_min
        and unusual < rfc.proactive_success_unusualness_max
    ):
        category = "successful_operation_reflection"
        outcome_quality = "good"
        detected_success = "Proactive recommendation aligned with moderate/high relevance context."
        suggested_adjustment = "Maintain conservative trigger pacing and continue evidence checks."
        confidence = rfc.proactive_success_category_confidence
        tags.update({"baseline_stable", "execution_capable_with_approval"})
        observations.append(
            ReflectionObservation(
                source="proactive",
                key="trigger",
                value=f"{pt.trigger_type}:{pt.trigger_score:.2f}",
                evidence={"importance_score": importance},
                confidence=rfc.proactive_observation_confidence,
            )
        )
    elif (
        pl.primary_signal.pattern_type == "baseline_idle_pattern"
        and pl.primary_signal.familiarity_score >= rfc.baseline_familiarity_min
    ):
        category = "stable_baseline_reflection"
        outcome_quality = "good"
        detected_success = "Current state resembles a stable baseline pattern."
        suggested_adjustment = "Avoid unnecessary interventions during stable baseline periods."
        confidence = rfc.baseline_category_confidence
        tags.add("baseline_stable")
        observations.append(
            ReflectionObservation(
                source="pattern_learning",
                key="baseline",
                value=f"familiarity={pl.primary_signal.familiarity_score:.2f}",
                confidence=rfc.baseline_observation_confidence,
            )
        )
    else:
        category = "uncertain_state_reflection"
        outcome_quality = "mixed"
        detected_issue = "Signals are mixed or low-confidence for a strong conclusion."
        suggested_adjustment = "Continue monitoring and prefer low-risk diagnostics over decisive changes."
        confidence = rfc.uncertain_default_confidence
        observations.append(
            ReflectionObservation(
                source="reflection",
                key="mixed_signals",
                value="insufficient_evidence_for_strong_claim",
                confidence=rfc.uncertain_observation_confidence,
            )
        )

    if st.summary.overall_status == "ok" and not has_proposal:
        tags.add("baseline_stable")
    if has_proposal:
        tags.add("self_maintenance_needed")
    if ex or cmd:
        tags.add("execution_capable_with_approval")

    if not tags:
        tags.add("adaptation_in_progress")

    current_operational_state = {
        "successful_operation_reflection": "stable_operation",
        "degraded_operation_reflection": "degraded_operation",
        "failed_operation_reflection": "degraded_operation",
        "repeated_warning_reflection": "degraded_operation",
        "successful_repair_reflection": "recovery_operation",
        "blocked_execution_reflection": "guarded_operation",
        "stable_baseline_reflection": "stable_operation",
        "uncertain_state_reflection": "uncertain_operation",
    }.get(category, "uncertain_operation")

    if detected_success:
        recent_outcome = detected_success
    elif detected_issue:
        recent_outcome = detected_issue

    summary = (
        f"{recent_outcome} Suggested adjustment: {suggested_adjustment}"
        if suggested_adjustment
        else recent_outcome
    )
    self_model = SelfModelSnapshot(
        self_model_tags=sorted(tags),
        current_operational_state=current_operational_state,
        confidence=_clamp01(confidence * rfc.self_model_confidence_scale),
        notes=notes,
        meta={
            "selftest_status": st.summary.overall_status,
            "top_proposal_type": prop_type,
            "proactive_trigger_type": pt.trigger_type,
        },
    )
    result = ReflectionResult(
        reflection_category=category,
        reflection_summary=summary,
        recent_outcome=recent_outcome,
        outcome_quality=outcome_quality,
        detected_issue=detected_issue,
        detected_success=detected_success,
        suggested_adjustment=suggested_adjustment,
        confidence=_clamp01(confidence),
        observations=observations,
        self_model=self_model,
        notes=notes,
        meta={
            "importance_score": importance,
            "pattern_unusualness": unusual,
            "warning_count": warn_n,
            "failed_count": failed_n,
            "workbench_has_proposal": has_proposal,
        },
    )
    print(
        f"[reflection] category={result.reflection_category} "
        f"conf={result.confidence:.2f} summary={result.recent_outcome[:80]}"
    )
    print(f"[reflection] self_model={','.join(result.self_model.self_model_tags[:6])}")
    return result
