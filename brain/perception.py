"""
Unified perception from camera + user text.

Phase 3: building :class:`PerceptionState` goes through :mod:`brain.perception_pipeline`
(staged acquisition → quality → detection → recognition → interpretation → continuity →
identity resolution → scene summary → interpretation layer → perception memory output →
memory importance scoring → pattern learning → proactive triggers → self-tests →
workbench proposals → reflection/self-model → contemplation → social continuity → memory refinement →
model routing → curiosity → outcome learning → conversational nuance →
multi-session strategic continuity → supervised self-improvement loop →
package), then :mod:`brain.perception_state_adapter` maps the bundle to flat state.

Vision gating: identity, emotion, and present-tense scene claims require ``visual_truth_trusted``
(camera layer: stable after fresh frames / recovery — see ``brain.camera``).

Manual test plan matches ``brain.camera`` (obstruction → recovering → stable).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .perception_pipeline import run_perception_pipeline
from .perception_state_adapter import bundle_to_perception_state
from .perception_utils import compute_salience, lbph_distance_to_identity_confidence
from .shared import now_ts

# Backward-compatible names for any legacy imports
_lbph_distance_to_identity_confidence = lbph_distance_to_identity_confidence


@dataclass
class PerceptionState:
    frame: Any = None
    face_detected: bool = False
    face_identity: str | None = None
    face_emotion: str | None = None
    gaze_present: bool = False
    person_count: int = 0
    user_text: str = ""
    salience: float = 0.2
    # Phase 6 — structured salience (pre quality/blur scalar in salience_combined_scalar when set)
    salience_top_type: str = ""
    salience_top_label: str = ""
    salience_top_score: float = 0.0
    salience_items: list[dict[str, Any]] = field(default_factory=list)
    salience_combined_scalar: float = 0.2
    timestamp: float = field(default_factory=now_ts)
    face_status: str = "No camera image"
    recognized_text: str = ""
    # Better Eyes E1 — first-class vision / trust (see brain/camera.py)
    vision_status: str = "stable"
    frame_ts: float = 0.0
    frame_age_ms: float = -1.0
    frame_source: str = "none"
    frame_seq: int = 0
    is_fresh: bool = False
    fresh_frame_streak: int = 0
    visual_truth_trusted: bool = True
    frame_quality: float = 0.0
    frame_quality_reasons: list[str] = field(default_factory=list)
    recovery_state: str = "none"
    last_stable_identity: str | None = None
    identity_confidence: float = 0.0
    continuity_confidence: float = 0.0
    # Phase 7 — temporal continuity (see brain.continuity)
    # Phase 8 — canonical identity_state from identity_fallback (not raw LBPH alone)
    identity_state: str = "no_face"
    # confirmed_recognition | likely_identity_by_continuity | unknown_face | no_face
    resolved_face_identity: str | None = None
    stable_face_identity: str | None = None
    identity_fallback_source: str = "none"
    identity_fallback_notes: list[str] = field(default_factory=list)
    continuity_prior_identity: str | None = None
    continuity_current_identity: str | None = None
    continuity_matched_factors: dict[str, Any] = field(default_factory=dict)
    continuity_matched_notes: list[str] = field(default_factory=list)
    continuity_frame_gap: int = 0
    continuity_seconds_since_prior: float = -1.0
    continuity_suppress_flip: bool = False
    # Phase 2 — acquisition layer (see brain.frame_store)
    acquisition_freshness: str = "unavailable"
    # Phase 4 — structured quality (brain.frame_quality + perception_pipeline quality stage)
    quality_label: str = "unreliable"
    # Phase 5 — blur layer (Laplacian variance, labels, per-path scales; see brain.frame_quality)
    blur_value: float = 0.0
    blur_label: str = "sharp"
    blur_confidence_scale: float = 1.0
    blur_recognition_scale: float = 1.0
    blur_expression_scale: float = 1.0
    blur_interpretation_scale: float = 1.0
    blur_reason_flags: list[str] = field(default_factory=list)
    blur_quality_score: float = 0.0
    darkness_quality_score: float = 0.0
    overexposure_quality_score: float = 0.0
    motion_smear_quality_score: float = 1.0
    occlusion_quality_score: float = 1.0
    recognition_quality_scale: float = 1.0
    expression_quality_scale: float = 1.0
    # Phase 9 — scene summary (brain.scene_summary; see scene_compact_summary for one-liner)
    scene_compact_summary: str = ""
    scene_overall_state: str = "uncertain"
    scene_summary_confidence: float = 0.0
    scene_face_presence: str = "unknown"
    scene_face_count_estimate: int = 0
    scene_primary_identity_line: str = ""
    scene_key_entities: list[str] = field(default_factory=list)
    scene_lighting_summary: str = ""
    scene_blur_summary: str = ""
    scene_change_summary: str = ""
    scene_entrant_summary: str = ""
    scene_summary_notes: list[str] = field(default_factory=list)
    scene_summary_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 10 — semantic interpretation (brain.interpretation; not user-facing text)
    interpretation_event_types: list[str] = field(default_factory=list)
    interpretation_primary_event: str = "uncertain_visual_state"
    interpretation_confidence: float = 0.0
    interpretation_priority: float = 0.0
    interpretation_subject: str | None = None
    interpretation_identity: str | None = None
    interpretation_notes: list[str] = field(default_factory=list)
    interpretation_no_meaningful_change: bool = True
    interpretation_evidence: dict[str, Any] = field(default_factory=dict)
    # Phase 11 — compact memory-ready event (generation only; no persistence here)
    perception_memory_event_type: str = ""
    perception_memory_candidate: bool = False
    perception_memory_confidence: float = 0.0
    perception_memory_summary: str = ""
    perception_memory_meta: dict[str, Any] = field(default_factory=dict)
    perception_memory_suppressed: bool = False
    perception_memory_skip_reason: str = ""
    # Phase 12 — memory importance scoring (decision only; no persistence here)
    memory_importance_score: float = 0.0
    memory_importance_label: str = "ignore"
    memory_worthy: bool = False
    memory_decision_reason: str = ""
    memory_class: str = "ignore"
    memory_scoring_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 13 — pattern learning (signals only; no persistence here)
    pattern_type: str = ""
    pattern_strength: float = 0.0
    pattern_familiarity_score: float = 0.0
    pattern_unusualness_score: float = 0.0
    pattern_subject: str = ""
    pattern_notes: list[str] = field(default_factory=list)
    pattern_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 14 — adaptive proactive trigger recommendation (no forced initiative)
    proactive_should_trigger: bool = False
    proactive_trigger_type: str = "no_trigger"
    proactive_trigger_score: float = 0.0
    proactive_trigger_reason: str = ""
    proactive_suppression_reason: str = ""
    proactive_trigger_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 15 — startup/recurring self-tests (diagnostics only)
    selftest_overall_status: str = "ok"
    selftest_failed_checks: list[str] = field(default_factory=list)
    selftest_warning_checks: list[str] = field(default_factory=list)
    selftest_last_run_type: str = "recurring"
    selftest_summary: str = ""
    selftest_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 16 — repair workbench proposal summary (proposal-only; no execution)
    workbench_has_proposal: bool = False
    workbench_top_proposal_type: str = "no_action_needed"
    workbench_top_proposal_title: str = ""
    workbench_top_proposal_priority: str = "low"
    workbench_top_proposal_risk: str = "low"
    workbench_summary: str = ""
    workbench_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 16.5 — supervised execution state (no auto execution in pipeline)
    workbench_execution_ready: bool = False
    workbench_execution_mode: str = "dry_run"
    workbench_last_execution_success: bool = False
    workbench_last_execution_summary: str = ""
    workbench_last_modified_files: list[str] = field(default_factory=list)
    workbench_last_backup_paths: list[str] = field(default_factory=list)
    workbench_execution_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 16.6 — workbench approval/command status surface
    workbench_command_available: bool = True
    workbench_pending_proposal_count: int = 0
    workbench_selected_proposal_id: str = ""
    workbench_last_command: str = ""
    workbench_last_command_success: bool = False
    workbench_last_command_summary: str = ""
    workbench_last_rollback_success: bool = False
    workbench_command_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 17 — evidence-based reflection and soft self-model (no auto behavior override)
    reflection_summary: str = ""
    reflection_category: str = "uncertain_state_reflection"
    reflection_confidence: float = 0.0
    reflection_suggested_adjustment: str = ""
    self_model_tags: list[str] = field(default_factory=list)
    self_model_state: str = "uncertain_operation"
    reflection_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 18 — bounded internal contemplation (guidance-only, non-overriding)
    contemplation_theme: str = "certainty_vs_usefulness"
    contemplation_summary: str = ""
    contemplation_question: str = ""
    contemplation_confidence: float = 0.0
    contemplation_guiding_principles: list[str] = field(default_factory=list)
    contemplation_priority_weights: dict[str, Any] = field(default_factory=dict)
    contemplation_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 22 — voice conversation / turn-taking (advisory; safe defaults when not in voice cycle)
    voice_turn_state: str = "idle"
    voice_user_speaking: bool = False
    voice_assistant_speaking: bool = False
    voice_should_wait: bool = False
    voice_should_respond: bool = True
    voice_response_readiness: float = 0.5
    voice_interrupted: bool = False
    voice_continuity_hint: str = ""
    voice_pacing_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 23 — social continuity / soft relationship modeling (bounded; descriptive only)
    relationship_familiarity_score: float = 0.5
    relationship_trust_signal: float = 0.5
    relationship_summary: str = ""
    interaction_style_hint: str = "steady_familiar_tone"
    unfinished_thread_present: bool = False
    recurring_topics: list[str] = field(default_factory=list)
    recent_social_tone: str = "neutral"
    relationship_confidence: float = 0.35
    relationship_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 24 — memory refinement (additive on Phase 12; advisory for persistence hooks)
    refined_memory_class: str = "ignore"
    refined_memory_worthy: bool = False
    refined_memory_retention_strength: float = 0.2
    refined_memory_retrieval_priority: float = 0.15
    refined_memory_unfinished_thread_candidate: bool = False
    refined_memory_social_relevance: float = 0.35
    refined_memory_episodic_relevance: float = 0.25
    refined_memory_pattern_relevance: float = 0.25
    refined_memory_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 25 — model routing (Ollama tag selection; identity/persona unchanged)
    cognitive_mode: str = "fallback_safe_mode"
    routing_selected_model: str = ""
    routing_fallback_model: str = ""
    routing_reason: str = ""
    routing_confidence: float = 0.0
    routing_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 26 — bounded curiosity (structured; advisory — does not execute actions)
    curiosity_triggered: bool = False
    curiosity_theme: str = "no_curiosity_needed"
    curiosity_question: str = ""
    curiosity_reason: str = ""
    curiosity_confidence: float = 0.0
    curiosity_suggested_next_step: str = "no_exploration_needed"
    curiosity_should_observe: bool = False
    curiosity_should_clarify: bool = False
    curiosity_should_defer: bool = False
    curiosity_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 27 — outcome learning (advisory adjustment signals — no automatic retuning)
    outcome_learning_category: str = "no_adjustment_needed"
    outcome_learning_quality: str = "neutral"
    suggested_behavior_adjustment: str = ""
    adjustment_confidence: float = 0.0
    adjustment_target: str = ""
    outcome_learning_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 28 — conversational nuance (bounded guidance; no automatic reply rewrite)
    nuance_tone: str = "uncertain_neutral"
    nuance_summary: str = ""
    nuance_confidence: float = 0.32
    warmth_level: float = 0.52
    practicality_level: float = 0.48
    softness_level: float = 0.52
    seriousness_level: float = 0.46
    humor_tolerance: float = 0.34
    verbosity_bias: float = 0.52
    pacing_bias: float = 0.5
    restraint_bias: float = 0.46
    nuance_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 29 — multi-session strategic continuity (bounded carryover; not Phase 7 temporal continuity)
    strategic_continuity_summary: str = ""
    strategic_continuity_confidence: float = 0.0
    active_threads: list[dict[str, Any]] = field(default_factory=list)
    strategic_priorities: list[str] = field(default_factory=list)
    relationship_carryover: str = ""
    maintenance_carryover: str = ""
    continuity_scope: str = "none"
    continuity_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 30 — supervised self-improvement loop (descriptive; no auto-approve/execute)
    improvement_loop_active: bool = False
    improvement_loop_stage: str = "no_active_loop"
    improvement_loop_summary: str = ""
    improvement_active_issue: str = ""
    improvement_active_proposal_id: str = ""
    improvement_awaiting_approval: bool = False
    improvement_execution_success: bool = False
    improvement_execution_failed: bool = False
    improvement_suggested_next_step: str = ""
    improvement_loop_meta: dict[str, Any] = field(default_factory=dict)
    # Phase 31 — resident heartbeat + bounded adaptive learning (advisory; no auto-speak)
    heartbeat_active: bool = False
    heartbeat_mode: str = "no_heartbeat"
    heartbeat_summary: str = ""
    heartbeat_last_reason: str = ""
    heartbeat_tick_id: int = 0
    heartbeat_meta: dict[str, Any] = field(default_factory=dict)
    learning_focus: str = ""
    learning_summary: str = ""
    learning_confidence: float = 0.0
    learning_preference_shifts: list[dict[str, Any]] = field(default_factory=list)
    # Phase 32 — runtime presence / operator snapshot (descriptive)
    runtime_presence_mode: str = "unknown"
    runtime_ready_state: str = "steady"
    runtime_active_issue_summary: str = ""
    runtime_threads_summary: str = ""
    runtime_maintenance_summary: str = ""
    runtime_learning_summary: str = ""
    runtime_operator_summary: str = ""
    runtime_presence_meta: dict[str, Any] = field(default_factory=dict)
    # Concern reconciliation (startup/runtime registry vs current evidence; advisory)
    active_concern_count: int = 0
    top_active_concern: str = ""
    concern_reconciliation_summary: str = ""
    concern_reconciliation_meta: dict[str, Any] = field(default_factory=dict)


def _compute_salience(state: PerceptionState) -> float:
    return compute_salience(state.face_detected, state.face_emotion, state.user_text or "")


def build_perception(camera_manager, image, g: dict, user_text: str = "") -> PerceptionState:
    """
    Build PerceptionState from camera + user input.
    When vision is not stable, do not treat identity/emotion as current truth.
    """
    bundle = run_perception_pipeline(camera_manager, image, g, user_text or "")
    return bundle_to_perception_state(bundle, user_text or "", g)
