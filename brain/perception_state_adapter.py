"""
Bundle → :class:`~brain.perception.PerceptionState` mapping (Phase 19 ownership).

``run_perception_pipeline`` in :mod:`brain.perception_pipeline` produces a
:class:`~brain.perception_types.PerceptionPipelineBundle`. This module is the single place
that maps bundle fields onto the legacy flat ``PerceptionState`` used by workspace /
runtime — including Phases 9–18 (scene through contemplation).

* **Pipeline** = staged inference and structured outputs.
* **Adapter** = deterministic field copies and safe defaults (no semantic changes).

Do not import :mod:`brain.perception_pipeline` here (avoids import cycles).
"""
from __future__ import annotations

from typing import Any

from .perception_types import (
    ContinuityOutput,
    IdentityResolutionResult,
    InterpretationLayerResult,
    InterpretationOutput,
    MemoryImportanceResult,
    PatternLearningResult,
    PerceptionMemoryOutput,
    PerceptionPipelineBundle,
    ProactiveTriggerResult,
    QualityOutput,
    ReflectionResult,
    SceneSummaryResult,
    SelfTestRunResult,
    ContemplationResult,
    WorkbenchProposalResult,
)
from .salience import salience_items_as_dicts


def _finalize_perception_runtime_context(state: Any, bundle: PerceptionPipelineBundle, g: Any) -> Any:
    """Attach Phase 22 voice through Phase 30 improvement loop (after strategic continuity)."""
    from .conversational_nuance import apply_conversational_nuance_to_perception_state
    from .self_improvement_loop import apply_improvement_loop_to_perception_state
    from .heartbeat import apply_heartbeat_to_perception_state
    from .adaptive_learning import apply_adaptive_learning_to_perception_state
    from .runtime_presence import apply_runtime_presence_to_perception_state
    from .session_continuity import apply_strategic_continuity_to_perception_state
    from .curiosity import apply_curiosity_to_perception_state
    from .memory_refinement import apply_memory_refinement_to_perception_state
    from .model_routing import apply_model_routing_to_perception_state
    from .outcome_learning import apply_outcome_learning_to_perception_state
    from .relationship_model import apply_social_continuity_to_perception_state
    from .voice_conversation import apply_voice_conversation_to_perception_state

    apply_voice_conversation_to_perception_state(state, g)
    apply_social_continuity_to_perception_state(state, bundle)
    apply_memory_refinement_to_perception_state(state, bundle)
    apply_model_routing_to_perception_state(state, bundle.model_routing)
    apply_curiosity_to_perception_state(state, bundle)
    apply_outcome_learning_to_perception_state(state, bundle)
    apply_conversational_nuance_to_perception_state(state, bundle)
    apply_strategic_continuity_to_perception_state(state, bundle)
    apply_improvement_loop_to_perception_state(state, bundle)
    apply_heartbeat_to_perception_state(state, bundle)
    apply_adaptive_learning_to_perception_state(state, bundle)
    apply_runtime_presence_to_perception_state(state, bundle)
    from .concern_reconciliation import apply_concern_reconciliation_to_perception_state

    apply_concern_reconciliation_to_perception_state(state, bundle)
    return state


def apply_cognitive_phases_from_bundle(state: Any, bundle: PerceptionPipelineBundle) -> None:
    """
    Map Phases 9–18 structured outputs from ``bundle`` onto ``state``.

    Shared by every ``bundle_to_perception_state`` exit path so early-exit and full-trust
    branches stay aligned without duplicating nine apply calls.
    """
    _apply_scene_summary_to_state(state, bundle.scene_summary)
    _apply_interpretation_layer_to_state(state, bundle.interpretation_layer)
    _apply_perception_memory_to_state(state, bundle.perception_memory)
    _apply_memory_importance_to_state(state, bundle.memory_importance)
    _apply_pattern_learning_to_state(state, bundle.pattern_learning)
    _apply_proactive_trigger_to_state(state, bundle.proactive_trigger)
    _apply_selftests_to_state(state, bundle.selftests)
    _apply_workbench_to_state(state, bundle.workbench)
    _apply_reflection_to_state(state, bundle.reflection)
    _apply_contemplation_to_state(state, bundle.contemplation)


def _apply_quality_fields_to_state(state: Any, q: QualityOutput) -> None:
    """Copy structured quality + scales onto PerceptionState (Phase 4)."""
    state.recognition_quality_scale = q.recognition_confidence_scale
    state.expression_quality_scale = q.expression_confidence_scale
    sq = q.structured
    if sq is None:
        state.quality_label = "unreliable"
        state.blur_value = 0.0
        state.blur_label = "sharp"
        state.blur_confidence_scale = 1.0
        state.blur_recognition_scale = 1.0
        state.blur_expression_scale = 1.0
        state.blur_interpretation_scale = 1.0
        state.blur_reason_flags = []
        state.blur_quality_score = 0.0
        state.darkness_quality_score = 0.0
        state.overexposure_quality_score = 0.0
        state.motion_smear_quality_score = 1.0
        state.occlusion_quality_score = 1.0
        return
    state.quality_label = sq.quality_label
    state.blur_value = getattr(sq, "blur_value", 0.0)
    state.blur_label = getattr(sq, "blur_label", "sharp")
    state.blur_confidence_scale = getattr(sq, "blur_confidence_scale", 1.0)
    state.blur_recognition_scale = getattr(sq, "blur_recognition_scale", 1.0)
    state.blur_expression_scale = getattr(sq, "blur_expression_scale", 1.0)
    state.blur_interpretation_scale = getattr(sq, "blur_interpretation_scale", 1.0)
    state.blur_reason_flags = list(getattr(sq, "blur_reason_flags", []) or [])
    state.blur_quality_score = sq.blur_score
    state.darkness_quality_score = sq.darkness_score
    state.overexposure_quality_score = sq.overexposure_score
    state.motion_smear_quality_score = sq.motion_smear_score
    state.occlusion_quality_score = sq.occlusion_score


def _apply_continuity_structured_to_state(state: Any, cont: ContinuityOutput | Any) -> None:
    """Copy Phase 7 continuity snapshot onto PerceptionState."""
    cr = getattr(cont, "structured", None)
    if cr is None:
        return
    state.continuity_confidence = float(cr.continuity_confidence)
    state.continuity_prior_identity = cr.prior_identity
    state.continuity_current_identity = cr.current_identity
    state.continuity_matched_factors = dict(cr.matched_factors)
    state.continuity_matched_notes = list(cr.matched_notes)
    state.continuity_frame_gap = int(cr.frame_gap)
    state.continuity_seconds_since_prior = float(cr.seconds_since_prior)
    state.continuity_suppress_flip = bool(cr.suppress_flip)


def _apply_identity_resolution_to_state(state: Any, ir: IdentityResolutionResult | None) -> None:
    """Copy Phase 8 identity resolution onto PerceptionState."""
    if ir is None:
        state.identity_state = "no_face"
        state.resolved_face_identity = None
        state.stable_face_identity = None
        state.identity_fallback_source = "none"
        state.identity_fallback_notes = []
        state.identity_confidence = 0.0
        return
    state.identity_state = ir.identity_state
    state.resolved_face_identity = ir.resolved_identity
    state.stable_face_identity = ir.stable_identity
    state.identity_fallback_source = ir.fallback_source
    state.identity_fallback_notes = list(ir.fallback_notes)
    state.identity_confidence = float(ir.identity_confidence)


def _apply_scene_summary_to_state(state: Any, ss: SceneSummaryResult | None) -> None:
    """Copy Phase 9 scene summary onto PerceptionState."""
    if ss is None:
        state.scene_compact_summary = ""
        state.scene_overall_state = "uncertain"
        state.scene_summary_confidence = 0.0
        state.scene_face_presence = "unknown"
        state.scene_face_count_estimate = 0
        state.scene_primary_identity_line = ""
        state.scene_key_entities = []
        state.scene_lighting_summary = ""
        state.scene_blur_summary = ""
        state.scene_change_summary = ""
        state.scene_entrant_summary = ""
        state.scene_summary_notes = []
        state.scene_summary_meta = {}
        return
    state.scene_compact_summary = ss.compact_text_summary
    state.scene_overall_state = ss.overall_scene_state
    state.scene_summary_confidence = float(ss.summary_confidence)
    state.scene_face_presence = ss.face_presence
    state.scene_face_count_estimate = int(ss.face_count_estimate)
    state.scene_primary_identity_line = ss.primary_identity_summary
    state.scene_key_entities = list(ss.key_entities)
    state.scene_lighting_summary = ss.lighting_summary
    state.scene_blur_summary = ss.blur_summary
    state.scene_change_summary = ss.scene_change_summary
    state.scene_entrant_summary = ss.entrant_summary
    state.scene_summary_notes = list(ss.notes)
    state.scene_summary_meta = dict(ss.meta)


def _apply_interpretation_layer_to_state(state: Any, il: InterpretationLayerResult | None) -> None:
    """Copy Phase 10 semantic interpretation onto PerceptionState."""
    if il is None:
        state.interpretation_event_types = []
        state.interpretation_primary_event = "uncertain_visual_state"
        state.interpretation_confidence = 0.0
        state.interpretation_priority = 0.0
        state.interpretation_subject = None
        state.interpretation_identity = None
        state.interpretation_notes = []
        state.interpretation_no_meaningful_change = True
        state.interpretation_evidence = {}
        return
    state.interpretation_event_types = list(il.event_types)
    state.interpretation_primary_event = il.primary_event
    state.interpretation_confidence = float(il.event_confidence)
    state.interpretation_priority = float(il.event_priority)
    state.interpretation_subject = il.interpreted_subject
    state.interpretation_identity = il.interpreted_identity
    state.interpretation_notes = list(il.interpretation_notes)
    state.interpretation_no_meaningful_change = bool(il.no_meaningful_change)
    state.interpretation_evidence = dict(il.evidence)


def _apply_perception_memory_to_state(state: Any, pm: PerceptionMemoryOutput | None) -> None:
    """Copy Phase 11 memory-ready record summary onto PerceptionState (no persistence)."""
    if pm is None:
        state.perception_memory_suppressed = False
        state.perception_memory_event_type = ""
        state.perception_memory_candidate = False
        state.perception_memory_confidence = 0.0
        state.perception_memory_summary = ""
        state.perception_memory_meta = {}
        state.perception_memory_skip_reason = ""
        return
    state.perception_memory_skip_reason = pm.skip_reason or ""
    if pm.event is None:
        state.perception_memory_suppressed = bool(pm.skipped)
        state.perception_memory_event_type = ""
        state.perception_memory_candidate = False
        state.perception_memory_confidence = 0.0
        state.perception_memory_summary = ""
        state.perception_memory_meta = {
            "skipped": bool(pm.skipped),
            "skip_reason": state.perception_memory_skip_reason,
        }
        return
    state.perception_memory_suppressed = False
    ev = pm.event
    state.perception_memory_event_type = ev.event_type
    state.perception_memory_candidate = bool(ev.memory_worthy_candidate)
    state.perception_memory_confidence = float(ev.event_confidence)
    state.perception_memory_summary = ev.scene_summary_snippet
    state.perception_memory_meta = {
        "wall_time": ev.wall_time,
        "frame_seq": ev.frame_seq,
        "event_priority": ev.event_priority,
        "identity_state": ev.identity_state,
        "resolved_identity": ev.resolved_identity,
        "stable_identity": ev.stable_identity,
        "interpretation_primary": ev.interpretation_primary_event,
        "evidence": dict(ev.evidence),
        "notes": list(ev.notes),
        "relevant_entities": list(ev.relevant_entities),
    }


def _apply_memory_importance_to_state(state: Any, mi: MemoryImportanceResult | None) -> None:
    """Copy Phase 12 memory scoring decision onto PerceptionState."""
    if mi is None:
        state.memory_importance_score = 0.0
        state.memory_importance_label = "ignore"
        state.memory_worthy = False
        state.memory_decision_reason = ""
        state.memory_class = "ignore"
        state.memory_scoring_meta = {}
        return
    d = mi.decision
    state.memory_importance_score = float(d.importance_score)
    state.memory_importance_label = d.importance_label or "ignore"
    state.memory_worthy = bool(d.memory_worthy)
    state.memory_decision_reason = d.decision_reason or ""
    state.memory_class = d.memory_class or "ignore"
    state.memory_scoring_meta = {
        "event_type": d.event_type,
        "novelty_score": float(d.novelty_score),
        "relevance_score": float(d.relevance_score),
        "uncertainty_penalty": float(d.uncertainty_penalty),
        "evidence": dict(d.evidence),
        "meta": dict(d.meta),
        "skipped": bool(mi.skipped),
        "skip_reason": mi.skip_reason or "",
    }


def _apply_pattern_learning_to_state(state: Any, pl: PatternLearningResult | None) -> None:
    """Copy Phase 13 pattern-learning signal onto PerceptionState."""
    if pl is None:
        state.pattern_type = ""
        state.pattern_strength = 0.0
        state.pattern_familiarity_score = 0.0
        state.pattern_unusualness_score = 0.0
        state.pattern_subject = ""
        state.pattern_notes = []
        state.pattern_meta = {}
        return
    p = pl.primary_signal
    state.pattern_type = p.pattern_type or ""
    state.pattern_strength = float(p.pattern_strength)
    state.pattern_familiarity_score = float(p.familiarity_score)
    state.pattern_unusualness_score = float(p.unusualness_score)
    state.pattern_subject = p.pattern_subject or ""
    state.pattern_notes = list(p.notes)
    state.pattern_meta = {
        "pattern_detected": bool(p.pattern_detected),
        "recurrence_count": int(p.recurrence_count),
        "recurrence_score": float(p.recurrence_score),
        "recent_transition_pattern": p.recent_transition_pattern,
        "pattern_candidate": bool(p.pattern_candidate),
        "suggested_memory_class": p.suggested_memory_class,
        "primary_meta": dict(p.meta),
        "signals": [
            {
                "pattern_type": s.pattern_type,
                "pattern_subject": s.pattern_subject,
                "pattern_strength": float(s.pattern_strength),
                "familiarity_score": float(s.familiarity_score),
                "unusualness_score": float(s.unusualness_score),
                "recurrence_count": int(s.recurrence_count),
                "pattern_candidate": bool(s.pattern_candidate),
            }
            for s in list(pl.signals or [])[:8]
        ],
        "skipped": bool(pl.skipped),
        "skip_reason": pl.skip_reason or "",
        "meta": dict(pl.meta),
    }


def _apply_proactive_trigger_to_state(state: Any, pt: ProactiveTriggerResult | None) -> None:
    """Copy Phase 14 proactive trigger recommendation onto PerceptionState."""
    if pt is None:
        state.proactive_should_trigger = False
        state.proactive_trigger_type = "no_trigger"
        state.proactive_trigger_score = 0.0
        state.proactive_trigger_reason = ""
        state.proactive_suppression_reason = ""
        state.proactive_trigger_meta = {}
        return
    state.proactive_should_trigger = bool(pt.should_trigger)
    state.proactive_trigger_type = pt.trigger_type or "no_trigger"
    state.proactive_trigger_score = float(pt.trigger_score)
    state.proactive_trigger_reason = pt.trigger_reason or ""
    state.proactive_suppression_reason = pt.suppression_reason or ""
    state.proactive_trigger_meta = {
        "trigger_priority": float(pt.trigger_priority),
        "suggested_action": pt.suggested_action,
        "caution_flags": list(pt.caution_flags),
        "supporting_evidence": dict(pt.supporting_evidence),
        "candidates": [
            {
                "trigger_type": c.trigger_type,
                "trigger_score": float(c.trigger_score),
                "trigger_priority": float(c.trigger_priority),
                "trigger_reason": c.trigger_reason,
                "suggested_action": c.suggested_action,
                "suppressed": bool(c.suppressed),
                "suppression_reason": c.suppression_reason,
            }
            for c in list(pt.candidates or [])[:8]
        ],
        "meta": dict(pt.meta),
    }


def _apply_selftests_to_state(state: Any, st: SelfTestRunResult | None) -> None:
    """Copy Phase 15 self-test summary onto PerceptionState."""
    if st is None:
        state.selftest_overall_status = "ok"
        state.selftest_failed_checks = []
        state.selftest_warning_checks = []
        state.selftest_last_run_type = "recurring"
        state.selftest_summary = ""
        state.selftest_meta = {}
        return
    s = st.summary
    state.selftest_overall_status = s.overall_status or "ok"
    state.selftest_failed_checks = list(s.failed_checks)
    state.selftest_warning_checks = list(s.warning_checks)
    state.selftest_last_run_type = st.run_type or "recurring"
    state.selftest_summary = s.message or ""
    state.selftest_meta = {
        "overall_severity": s.overall_severity,
        "passed_checks": list(s.passed_checks),
        "skipped_checks": list(s.skipped_checks),
        "run_timestamp": float(st.timestamp),
        "run_meta": dict(st.meta),
        "summary_meta": dict(s.meta),
    }


def _apply_workbench_to_state(state: Any, wb: WorkbenchProposalResult | None) -> None:
    """Copy Phase 16 workbench proposal summary onto PerceptionState."""
    if wb is None:
        state.workbench_has_proposal = False
        state.workbench_top_proposal_type = "no_action_needed"
        state.workbench_top_proposal_title = ""
        state.workbench_top_proposal_priority = "low"
        state.workbench_top_proposal_risk = "low"
        state.workbench_summary = ""
        state.workbench_meta = {}
        state.workbench_execution_ready = False
        state.workbench_execution_mode = "dry_run"
        state.workbench_last_execution_success = False
        state.workbench_last_execution_summary = ""
        state.workbench_last_modified_files = []
        state.workbench_last_backup_paths = []
        state.workbench_execution_meta = {}
        state.workbench_command_available = True
        state.workbench_pending_proposal_count = 0
        state.workbench_selected_proposal_id = ""
        state.workbench_last_command = ""
        state.workbench_last_command_success = False
        state.workbench_last_command_summary = ""
        state.workbench_last_rollback_success = False
        state.workbench_command_meta = {}
        return
    top = wb.top_proposal
    state.workbench_has_proposal = bool(wb.has_proposal)
    state.workbench_top_proposal_type = top.proposal_type or "no_action_needed"
    state.workbench_top_proposal_title = top.title or ""
    state.workbench_top_proposal_priority = top.priority or "low"
    state.workbench_top_proposal_risk = top.risk_level or "low"
    state.workbench_summary = wb.summary or ""
    state.workbench_meta = {
        "top_confidence": float(top.confidence),
        "top_requires_human_review": bool(top.requires_human_review),
        "top_recommended_action": top.recommended_action,
        "top_problem": top.problem_detected,
        "proposals": [
            {
                "proposal_id": p.proposal_id,
                "proposal_type": p.proposal_type,
                "title": p.title,
                "priority": p.priority,
                "risk_level": p.risk_level,
                "confidence": float(p.confidence),
                "requires_human_review": bool(p.requires_human_review),
            }
            for p in list(wb.proposals or [])[:8]
        ],
        "meta": dict(wb.meta),
    }
    state.workbench_execution_ready = bool(wb.has_proposal)
    state.workbench_execution_mode = "dry_run"
    state.workbench_last_execution_success = False
    state.workbench_last_execution_summary = "No supervised execution run this tick."
    state.workbench_last_modified_files = []
    state.workbench_last_backup_paths = []
    state.workbench_execution_meta = {
        "approval_required": True,
        "auto_execution": False,
        "execution_module": "brain.workbench_execute",
    }
    state.workbench_command_available = True
    state.workbench_pending_proposal_count = len(list(wb.proposals or []))
    state.workbench_selected_proposal_id = top.proposal_id
    state.workbench_last_command = ""
    state.workbench_last_command_success = False
    state.workbench_last_command_summary = "No command issued this tick."
    state.workbench_last_rollback_success = False
    state.workbench_command_meta = {
        "command_module": "brain.workbench_commands",
        "approval_needed_for_staged_apply": True,
    }


def _apply_reflection_to_state(state: Any, rf: ReflectionResult | None) -> None:
    """Copy Phase 17 reflection/self-model output onto PerceptionState."""
    if rf is None:
        state.reflection_summary = ""
        state.reflection_category = "uncertain_state_reflection"
        state.reflection_confidence = 0.0
        state.reflection_suggested_adjustment = ""
        state.self_model_tags = []
        state.self_model_state = "uncertain_operation"
        state.reflection_meta = {}
        return
    state.reflection_summary = rf.reflection_summary or ""
    state.reflection_category = rf.reflection_category or "uncertain_state_reflection"
    state.reflection_confidence = float(rf.confidence)
    state.reflection_suggested_adjustment = rf.suggested_adjustment or ""
    state.self_model_tags = list(rf.self_model.self_model_tags)
    state.self_model_state = rf.self_model.current_operational_state or "uncertain_operation"
    state.reflection_meta = {
        "recent_outcome": rf.recent_outcome,
        "outcome_quality": rf.outcome_quality,
        "detected_issue": rf.detected_issue,
        "detected_success": rf.detected_success,
        "observations": [
            {
                "source": o.source,
                "key": o.key,
                "value": o.value,
                "confidence": float(o.confidence),
            }
            for o in list(rf.observations or [])[:10]
        ],
        "self_model_confidence": float(rf.self_model.confidence),
        "self_model_meta": dict(rf.self_model.meta),
        "notes": list(rf.notes),
        "meta": dict(rf.meta),
    }


def _apply_contemplation_to_state(state: Any, ct: ContemplationResult | None) -> None:
    """Copy Phase 18 contemplation output onto PerceptionState."""
    if ct is None:
        state.contemplation_theme = "certainty_vs_usefulness"
        state.contemplation_summary = ""
        state.contemplation_question = ""
        state.contemplation_confidence = 0.0
        state.contemplation_guiding_principles = []
        state.contemplation_priority_weights = {}
        state.contemplation_meta = {}
        return
    p = ct.priority_weights
    state.contemplation_theme = ct.contemplation_theme or "certainty_vs_usefulness"
    state.contemplation_summary = ct.contemplation_summary or ""
    state.contemplation_question = ct.contemplation_question or ""
    state.contemplation_confidence = float(ct.contemplation_confidence)
    state.contemplation_guiding_principles = list(ct.guiding_principles)
    state.contemplation_priority_weights = {
        "observe": float(p.observe),
        "clarify": float(p.clarify),
        "remember": float(p.remember),
        "adapt": float(p.adapt),
        "maintain": float(p.maintain),
        "engage": float(p.engage),
        "remain_silent": float(p.remain_silent),
    }
    state.contemplation_meta = {
        "position": ct.contemplation_position,
        "caution_notes": list(ct.caution_notes),
        "evidence_basis": dict(ct.evidence_basis),
        "notes": list(ct.notes),
        "meta": dict(ct.meta),
    }


def _apply_salience_structured_to_state(state: Any, interp: InterpretationOutput) -> None:
    """Copy Phase 6 salience snapshot onto PerceptionState (safe if structured missing)."""
    sr = interp.salience_structured
    if sr is None:
        state.salience_items = []
        state.salience_top_label = ""
        state.salience_top_type = ""
        state.salience_top_score = 0.0
        state.salience_combined_scalar = float(interp.salience)
        return
    state.salience_items = salience_items_as_dicts(sr.items)
    top = next((x for x in sr.items if x.is_top), None)
    state.salience_top_label = top.label if top else ""
    state.salience_top_type = top.item_type if top else ""
    state.salience_top_score = float(top.score) if top else 0.0
    state.salience_combined_scalar = float(sr.combined_scalar)


def bundle_to_perception_state(bundle: PerceptionPipelineBundle, user_text: str, g: Any = None) -> Any:
    """
    Map pipeline bundle to :class:`brain.perception.PerceptionState` (legacy shape for workspace / avaagent).
    """
    from .perception import PerceptionState
    from .shared import now_ts

    state = PerceptionState(user_text=user_text or "", timestamp=now_ts())
    resolved = bundle.resolved
    q = bundle.quality
    d = bundle.detection
    r = bundle.recognition
    c = bundle.continuity
    i = bundle.interpretation
    idr = bundle.identity_resolution

    if not bundle.acquisition.stage.ok or resolved is None:
        apply_cognitive_phases_from_bundle(state, bundle)
        return _finalize_perception_runtime_context(state, bundle, g)

    state.frame = resolved.frame
    state.vision_status = resolved.vision_status
    state.frame_ts = resolved.frame_ts
    state.frame_age_ms = resolved.frame_age_ms
    state.frame_source = resolved.source
    state.frame_seq = resolved.frame_seq
    state.is_fresh = resolved.is_fresh
    state.fresh_frame_streak = resolved.fresh_frame_streak
    state.visual_truth_trusted = q.visual_truth_trusted
    state.frame_quality = q.frame_quality
    state.frame_quality_reasons = list(q.frame_quality_reasons)
    state.recovery_state = q.recovery_state
    state.last_stable_identity = getattr(resolved, "last_stable_identity", None)
    state.identity_confidence = 0.0
    state.continuity_confidence = 0.0
    state.acquisition_freshness = getattr(resolved, "acquisition_freshness", "unavailable")
    _apply_quality_fields_to_state(state, q)

    if resolved.frame is None:
        state.face_status = "No camera image"
        state.recognized_text = "No frame — cannot assess the scene as current."
        state.face_detected = False
        state.person_count = 0
        state.gaze_present = False
        print(
            f"[perception] vision={state.vision_status} acq={state.acquisition_freshness} "
            f"qlabel={state.quality_label} fq={state.frame_quality:.2f} recovery={state.recovery_state} "
            f"trusted=False id_conf=0.0 (suppress identity/emotion/scene-as-current)"
        )
        apply_cognitive_phases_from_bundle(state, bundle)
        return _finalize_perception_runtime_context(state, bundle, g)

    if not resolved.visual_truth_trusted:
        if resolved.vision_status == "stale_frame":
            state.face_status = "Stale or outdated camera frame"
            state.recognized_text = (
                "Frame is too old to treat as a current view — not using recognition as ground truth."
            )
        elif resolved.vision_status == "recovering":
            state.face_status = "Vision recovering (stabilizing)"
            state.recognized_text = (
                "Vision is recovering after an interruption — identity and expression are not trusted as current yet."
            )
        elif resolved.vision_status == "low_quality":
            rsn = ", ".join(state.frame_quality_reasons) or "quality"
            state.face_status = "Frame quality too low for a reliable read"
            state.recognized_text = (
                f"Image quality is weak ({rsn}) — not using identity or expression as ground truth yet."
            )
        else:
            state.face_status = "Vision unavailable"
            state.recognized_text = "No reliable visual read right now."
        state.face_identity = None
        state.face_emotion = None
        state.face_detected = False
        state.person_count = 0
        state.gaze_present = False
        _apply_continuity_structured_to_state(state, c)
        _apply_identity_resolution_to_state(state, idr)
        if state.last_stable_identity and state.continuity_confidence < 0.12:
            state.continuity_confidence = 0.12
        base = float(i.salience)
        state.salience = base
        _apply_salience_structured_to_state(state, i)
        print(
            f"[perception] vision={state.vision_status} qlabel={state.quality_label} "
            f"age_ms={state.frame_age_ms:.0f} src={state.frame_source} fq={state.frame_quality:.2f} "
            f"recovery={state.recovery_state} streak={state.fresh_frame_streak} trusted=False "
            f"id_conf=0.0 cont={state.continuity_confidence:.2f} "
            f"(suppress identity/emotion/scene-as-current)"
        )
        apply_cognitive_phases_from_bundle(state, bundle)
        return _finalize_perception_runtime_context(state, bundle, g)

    state.face_status = d.face_status
    state.face_detected = d.face_detected
    state.person_count = d.person_count
    state.gaze_present = d.gaze_present
    state.recognized_text = r.recognized_text
    state.face_identity = r.face_identity
    state.face_emotion = i.face_emotion

    _apply_continuity_structured_to_state(state, c)
    cr = c.structured
    _apply_identity_resolution_to_state(state, idr)

    if idr is not None and idr.resolved_identity:
        state.last_stable_identity = idr.resolved_identity
    if (
        idr is not None
        and state.face_detected
        and idr.identity_state == "unknown_face"
    ):
        state.identity_confidence = max(
            float(state.identity_confidence),
            0.22 * float(q.recognition_confidence_scale),
        )
    if cr is not None:
        state.continuity_confidence = max(
            float(state.continuity_confidence), float(cr.continuity_confidence)
        )
    if not state.face_detected and state.last_stable_identity:
        state.continuity_confidence = max(float(state.continuity_confidence), 0.14)

    # Interpretation / salience: structured combined scalar × quality expression × blur (interp).
    base_sal = float(i.salience)
    state.salience = (
        base_sal * q.quality_only_expression_scale * q.blur_interpretation_scale
    )
    _apply_salience_structured_to_state(state, i)
    apply_cognitive_phases_from_bundle(state, bundle)
    print(
        f"[perception] vision={state.vision_status} acq={state.acquisition_freshness} "
        f"qlabel={state.quality_label} blur_label={state.blur_label} blur_val={state.blur_value:.1f} "
        f"age_ms={state.frame_age_ms:.0f} src={state.frame_source} "
        f"fq={state.frame_quality:.2f} recovery={state.recovery_state} streak={state.fresh_frame_streak} "
        f"trusted=True id_conf={state.identity_confidence:.2f} (rec_scale={q.recognition_confidence_scale:.2f}) "
        f"cont={state.continuity_confidence:.2f} id_state={state.identity_state} "
        f"resolved_id={state.resolved_face_identity!r} raw_id={state.face_identity!r} "
        f"salience={state.salience:.2f} top={state.salience_top_type}:{state.salience_top_label} "
        f"scene={state.scene_overall_state!r} interp={state.interpretation_primary_event!r} "
        f"(base={base_sal:.2f} expr_q={q.quality_only_expression_scale:.2f} blur_interp={q.blur_interpretation_scale:.2f})"
    )
    return _finalize_perception_runtime_context(state, bundle, g)
