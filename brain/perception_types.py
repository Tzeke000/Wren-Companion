"""
Structured types for the Phase 3 perception pipeline (brain.perception_pipeline).

Each stage produces a small dataclass with :class:`StageResult` plus stage-specific fields.
Downstream code adapts a :class:`PerceptionPipelineBundle` to :class:`perception.PerceptionState`.

**Future extension points** (not all wired yet):
- **Frame quality** — see :class:`FrameQualityAssessment` and ``brain.frame_quality`` (Phase 4).
- **Salience** scoring beyond face + emotion heuristics.
- **Tracking / continuity** — multi-frame identity tracks (E4).
- **Scene summaries** — short-term visual memory text.
- **Interpretation** layer — LLM or rule-based scene narration (gated by trust).
- **Phase 5 blur** — scene summaries, recognition fallback, interpretation certainty, and visual memory-worthiness can read ``blur_label`` / ``blur_reason_flags`` from :class:`FrameQualityAssessment` or :class:`perception.PerceptionState` (hooks not all wired yet).
- **Phase 6 salience** — :class:`SalientItem`, :class:`SalienceResult`, ``brain.salience.build_salience_result``; ranked items on :class:`InterpretationOutput` / :class:`perception.PerceptionState` for summaries, memory-worthiness, and initiative (see roadmap).
- **Phase 7 continuity** — :class:`ContinuityResult` (identity_state, temporal match factors); ``brain.continuity.update_continuity``.
- **Phase 8 identity** — :class:`IdentityResolutionResult` (raw vs resolved identity); ``brain.identity_fallback.resolve_identity_fallback``.
- **Phase 9 scene** — :class:`SceneSummaryResult`; ``brain.scene_summary.build_scene_summary``.
- **Phase 10 interpretation** — :class:`InterpretationLayerResult` (semantic events); ``brain.interpretation.build_interpretation_layer``.
- **Phase 11 perception memory** — :class:`PerceptionMemoryEvent`, :class:`PerceptionMemoryOutput`; ``brain.perception_memory.build_perception_memory_output``.
- **Phase 12 memory scoring** — :class:`MemoryDecisionResult`, :class:`MemoryImportanceResult`; ``brain.memory_scoring.score_memory_importance``.
- **Phase 13 pattern learning** — :class:`PatternSignal`, :class:`PatternLearningResult`; ``brain.pattern_learning.learn_pattern_signals``.
- **Phase 14 proactive triggers** — :class:`ProactiveTriggerCandidate`, :class:`ProactiveTriggerResult`; ``brain.proactive_triggers.evaluate_proactive_triggers``.
- **Phase 15 self-tests** — :class:`SelfTestCheckResult`, :class:`SelfTestRunResult`, :class:`HealthSummaryResult`; ``brain.selftests``.
- **Phase 16 workbench proposals** — :class:`RepairProposal`, :class:`WorkbenchProposalResult`; ``brain.workbench.build_workbench_proposals``.
- **Phase 16.5 supervised execution** — :class:`WorkbenchExecutionRequest`, :class:`WorkbenchExecutionResult`, :class:`FileChangePlan`, :class:`FileChangeRecord`; ``brain.workbench_execute``.
- **Phase 16.6 command layer** — :class:`WorkbenchCommandRequest`, :class:`WorkbenchCommandResult`, :class:`WorkbenchProposalView`, :class:`WorkbenchQueueState`; ``brain.workbench_commands``.
- **Phase 17 reflection** — :class:`ReflectionObservation`, :class:`SelfModelSnapshot`, :class:`ReflectionResult`; ``brain.reflection.build_reflection_result``.
- **Phase 18 contemplation** — :class:`ContemplationPrompt`, :class:`InternalPriorityView`, :class:`ContemplationResult`; ``brain.contemplation.build_contemplation_result``.
- **Phase 21 calibration** — :class:`CalibrationObservation`, :class:`ThresholdReviewResult`, :class:`CalibrationReport`; ``brain.calibration`` (runtime signals + watchlist, no auto-retuning).
- **Phase 22 voice conversation** — :class:`VoiceTimingDecision`, :class:`VoiceConversationResult`; ``brain.voice_conversation`` (turn-taking hints; record-stop UX).
- **Phase 23 relationship / social continuity** — :class:`RelationshipSignal`, :class:`InteractionStyleProfile`, :class:`SocialContinuityResult`; ``brain.relationship_model`` (bounded soft social signals).
- **Phase 24 memory refinement** — :class:`MemoryLinkSuggestion`, :class:`RefinedMemoryDecision`, :class:`MemoryRefinementResult`; ``brain.memory_refinement`` (retention / retrieval hints; additive on Phase 12).
- **Phase 25 model routing** — :class:`ModelRouteCandidate`, :class:`CognitiveModeResult`, :class:`ModelRoutingResult`; ``brain.model_routing`` (explainable cognitive-mode → Ollama model mapping; stable identity across switches).
- **Phase 26 curiosity** — :class:`CuriosityQuestion`, :class:`ExplorationSuggestion`, :class:`CuriosityResult`; ``brain.curiosity`` (bounded anomaly / gap noticing; structured suggestions only — no autonomous actions).
- **Phase 27 outcome learning** — :class:`OutcomeObservation`, :class:`BehaviorAdjustmentSuggestion`, :class:`OutcomeLearningResult`; ``brain.outcome_learning`` (evidence-weighted advisory adjustment signals — no runtime auto-retuning).
- **Phase 28 conversational nuance** — :class:`NuanceSignal`, :class:`ToneGuidanceProfile`, :class:`ConversationalNuanceResult`; ``brain.conversational_nuance`` (bounded tone/pacing/restraint guidance — no direct response rewrite).
- **Phase 29 multi-session continuity** — :class:`ContinuityThread`, :class:`SessionCarryoverSummary`, :class:`StrategicContinuityResult`; ``brain.session_continuity`` (bounded carryover; **ava_core/** ``IDENTITY.md`` / ``SOUL.md`` / ``USER.md`` read first as authoritative anchors — read-only here; ``BOOTSTRAP.md`` omitted once core identity is established).
- **Phase 30 supervised self-improvement loop** — :class:`ImprovementCycle`, :class:`ImprovementLoopResult`, :class:`ImprovementStepStatus`; ``brain.self_improvement_loop`` (issue → proposal → approval → execution → reflection — **descriptive only**, no auto-approve/execute).
- **Phase 31 heartbeat & adaptive learning** — :class:`HeartbeatEvent`, :class:`HeartbeatState`, :class:`HeartbeatTickResult`, :class:`AdaptiveLearningResult`; ``brain.heartbeat``, ``brain.adaptive_learning`` (quiet background continuity + bounded preference updates — **no** safety/approval bypass).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SalientItem:
    """One ranked salience candidate (Phase 6)."""

    item_type: str  # "face" | "object" | "scene_cue"
    label: str
    score: float
    factors: dict[str, float] = field(default_factory=dict)
    is_top: bool = False


@dataclass
class SalienceResult:
    """Ranked salience output; ``combined_scalar`` feeds legacy ``InterpretationOutput.salience``."""

    items: list[SalientItem] = field(default_factory=list)
    combined_scalar: float = 0.2
    legacy_engagement_scalar: float = 0.2
    future_hooks: dict[str, Any] = field(default_factory=dict)


@dataclass
class FrameQualityAssessment:
    """
    Structured frame quality (Phase 4 + Phase 5 blur layer). Scores are in [0, 1] where
    higher is better except where noted. ``overall_quality_score`` matches the legacy
    camera aggregate used for ``ResolvedFrame.frame_quality`` / low-quality gating when
    computed via ``brain.frame_quality.compute_frame_quality``.

    **Phase 5 blur** (Laplacian variance): ``blur_value``, ``blur_label`` (sharp | soft | blurry),
    per-layer scales for recognition / expression / interpretation — see
    ``brain.frame_quality.blur_layer_confidence_scales``.
    """

    blur_value: float = 0.0
    blur_label: str = "sharp"
    blur_confidence_scale: float = 1.0
    blur_recognition_scale: float = 1.0
    blur_expression_scale: float = 1.0
    blur_interpretation_scale: float = 1.0
    blur_reason_flags: list[str] = field(default_factory=list)
    blur_score: float = 0.0
    darkness_score: float = 0.0
    overexposure_score: float = 0.0
    motion_smear_score: float = 1.0
    occlusion_score: float = 1.0
    overall_quality_score: float = 0.0
    quality_label: str = "unreliable"
    reason_flags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class StageResult:
    """Success/failure wrapper for one pipeline stage (always safe defaults on failure)."""

    ok: bool
    skipped: bool = False
    error: Optional[str] = None
    confidence: Optional[float] = None
    meta: dict = field(default_factory=dict)


@dataclass
class AcquisitionOutput:
    """Stage 1 — frame resolution (delegates to ``CameraManager.resolve_frame_detailed``)."""

    stage: StageResult
    resolved: Any = None  # ResolvedFrame | None


@dataclass
class QualityOutput:
    """Stage 2 — trust / staleness / structured quality (see ``FrameQualityAssessment``)."""

    stage: StageResult
    visual_truth_trusted: bool = False
    vision_status: str = "stable"
    frame_quality: float = 0.0
    frame_quality_reasons: list[str] = field(default_factory=list)
    is_fresh: bool = False
    recovery_state: str = "none"
    fresh_frame_streak: int = 0
    structured: Optional[FrameQualityAssessment] = None
    recognition_confidence_scale: float = 1.0
    expression_confidence_scale: float = 1.0
    # Phase 5 — blur layer (combined scales above = quality_label × blur)
    blur_value: float = 0.0
    blur_label: str = "sharp"
    blur_recognition_scale: float = 1.0
    blur_expression_scale: float = 1.0
    blur_interpretation_scale: float = 1.0
    quality_only_recognition_scale: float = 1.0
    quality_only_expression_scale: float = 1.0


@dataclass
class DetectionOutput:
    """Stage 3 — face detection (Haar cascade + status line)."""

    stage: StageResult
    face_detected: bool = False
    person_count: int = 0
    face_status: str = "No camera image"
    gaze_present: bool = False
    # Phase 6 — salience geometry (BGR frame pixels, x y w h)
    face_rects: list[tuple[int, int, int, int]] = field(default_factory=list)


@dataclass
class RecognitionOutput:
    """Stage 4 — LBPH recognition (skipped when vision not trusted)."""

    stage: StageResult
    recognized_text: str = ""
    face_identity: Optional[str] = None
    identity_confidence: float = 0.0


@dataclass
class ContinuityResult:
    """Phase 7 — structured temporal continuity (lightweight frame memory, not a full tracker)."""

    identity_state: str = "no_face"
    # confirmed_recognition | likely_identity_by_continuity | unknown_face | no_face
    continuity_confidence: float = 0.0
    prior_identity: Optional[str] = None
    current_identity: Optional[str] = None
    matched_factors: dict[str, float] = field(default_factory=dict)
    matched_notes: list[str] = field(default_factory=list)
    frame_gap: int = 0
    seconds_since_prior: float = -1.0
    suppress_flip: bool = False
    last_stable_identity: Optional[str] = None


@dataclass
class IdentityResolutionResult:
    """Phase 8 — public identity hierarchy (raw LBPH separate from resolved/stable)."""

    identity_state: str = "no_face"
    # confirmed_recognition | likely_identity_by_continuity | unknown_face | no_face
    raw_identity: Optional[str] = None
    resolved_identity: Optional[str] = None
    stable_identity: Optional[str] = None
    identity_confidence: float = 0.0
    fallback_source: str = "none"  # recognition | continuity | none
    fallback_notes: list[str] = field(default_factory=list)


@dataclass
class SceneSummaryResult:
    """Phase 9 — compact stable scene description for reasoning, memory, and UI."""

    face_presence: str = "unknown"  # none | unknown_face | single_face | multiple_faces
    face_count_estimate: int = 0
    primary_identity_summary: str = ""
    key_entities: list[str] = field(default_factory=list)
    lighting_summary: str = ""
    blur_summary: str = ""
    scene_change_summary: str = ""
    entrant_summary: str = ""
    overall_scene_state: str = "uncertain"  # stable | changed | uncertain
    compact_text_summary: str = ""
    summary_confidence: float = 0.5
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class InterpretationLayerResult:
    """Phase 10 — semantic events inferred from structured perception (not raw pixels, not user text gen)."""

    event_types: list[str] = field(default_factory=list)
    event_confidence: float = 0.5
    event_priority: float = 0.5
    interpreted_subject: Optional[str] = None
    interpreted_identity: Optional[str] = None
    interpretation_notes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    no_meaningful_change: bool = True
    primary_event: str = "uncertain_visual_state"


@dataclass
class PerceptionMemoryEvent:
    """Phase 11 — one memory-ready perception record (no persistence in this phase)."""

    wall_time: float = 0.0
    frame_seq: int = 0
    event_type: str = ""
    event_confidence: float = 0.0
    event_priority: float = 0.0
    identity_state: str = ""
    resolved_identity: Optional[str] = None
    stable_identity: Optional[str] = None
    relevant_entities: list[str] = field(default_factory=list)
    scene_summary_snippet: str = ""
    interpretation_primary_event: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    memory_worthy_candidate: bool = False
    notes: list[str] = field(default_factory=list)
    suppressed_duplicate: bool = False


@dataclass
class PerceptionMemoryOutput:
    """Phase 11 — optional primary event + duplicate-suppression metadata."""

    event: Optional[PerceptionMemoryEvent] = None
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class MemoryDecisionResult:
    """Phase 12 — scored decision for one memory-ready perception event (still no persistence)."""

    event_type: str = ""
    importance_score: float = 0.0
    importance_label: str = "ignore"  # ignore | low | medium | high
    memory_worthy: bool = False
    # transient_context | episodic_candidate | pattern_candidate | preference_candidate | ignore
    memory_class: str = "ignore"
    decision_reason: str = ""
    novelty_score: float = 0.0
    relevance_score: float = 0.0
    uncertainty_penalty: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryImportanceResult:
    """Phase 12 — wrapper with skip metadata around :class:`MemoryDecisionResult`."""

    decision: MemoryDecisionResult = field(default_factory=MemoryDecisionResult)
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class PatternSignal:
    """Phase 13 — one lightweight probabilistic pattern signal."""

    pattern_detected: bool = False
    pattern_type: str = ""  # identity_presence_pattern | scene_stability_pattern | ...
    pattern_subject: str = ""
    pattern_strength: float = 0.0
    familiarity_score: float = 0.0
    unusualness_score: float = 0.0
    recurrence_count: int = 0
    recurrence_score: float = 0.0
    recent_transition_pattern: str = ""
    pattern_candidate: bool = False
    suggested_memory_class: str = "ignore"
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PatternLearningResult:
    """Phase 13 — wrapper for all pattern-learning signals in one tick."""

    primary_signal: PatternSignal = field(default_factory=PatternSignal)
    signals: list[PatternSignal] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProactiveTriggerCandidate:
    """Phase 14 — one proactive trigger candidate (recommendation-only)."""

    trigger_type: str = "no_trigger"
    trigger_score: float = 0.0
    trigger_priority: float = 0.0
    trigger_reason: str = ""
    suggested_action: str = "wait"
    caution_flags: list[str] = field(default_factory=list)
    supporting_evidence: dict[str, Any] = field(default_factory=dict)
    suppressed: bool = False
    suppression_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProactiveTriggerResult:
    """Phase 14 — proactive trigger recommendation output for this tick."""

    should_trigger: bool = False
    trigger_type: str = "no_trigger"
    trigger_score: float = 0.0
    trigger_priority: float = 0.0
    trigger_reason: str = ""
    suppression_reason: str = ""
    suggested_action: str = "wait"
    caution_flags: list[str] = field(default_factory=list)
    supporting_evidence: dict[str, Any] = field(default_factory=dict)
    candidates: list[ProactiveTriggerCandidate] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SelfTestCheckResult:
    """Phase 15 — one subsystem self-test check result."""

    check_name: str = ""
    status: str = "skipped"  # ok | warning | failed | skipped
    severity: str = "info"  # info | warning | critical
    passed: bool = False
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0
    recommended_next_step: str = ""


@dataclass
class HealthSummaryResult:
    """Phase 15 — summarized health status across checks."""

    overall_status: str = "ok"  # ok | warning | failed
    overall_severity: str = "info"  # info | warning | critical
    failed_checks: list[str] = field(default_factory=list)
    warning_checks: list[str] = field(default_factory=list)
    passed_checks: list[str] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)
    message: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SelfTestRunResult:
    """Phase 15 — startup/recurring self-test run output."""

    run_type: str = "recurring"  # startup | recurring | on_demand
    checks: list[SelfTestCheckResult] = field(default_factory=list)
    summary: HealthSummaryResult = field(default_factory=HealthSummaryResult)
    timestamp: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RepairProposal:
    """Phase 16 — reviewable repair proposal (no automatic execution)."""

    proposal_id: str = ""
    proposal_type: str = "no_action_needed"
    title: str = ""
    problem_detected: str = ""
    likely_cause: str = ""
    recommended_action: str = ""
    risk_level: str = "low"  # low | medium | high
    requires_human_review: bool = True
    confidence: float = 0.0
    source_checks: list[str] = field(default_factory=list)
    priority: str = "low"  # low | medium | high | urgent
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkbenchProposalResult:
    """Phase 16 — proposal bundle generated from self-tests/runtime evidence."""

    has_proposal: bool = False
    top_proposal: RepairProposal = field(default_factory=RepairProposal)
    proposals: list[RepairProposal] = field(default_factory=list)
    summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FileChangePlan:
    """Phase 16.5 — requested file action plan for supervised execution."""

    action_type: str = "write_patch_plan"
    target_path: str = ""
    content: str = ""
    patch_text: str = ""
    append_text: str = ""
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FileChangeRecord:
    """Phase 16.5 — recorded file action outcome for one path."""

    target_path: str = ""
    action_type: str = ""
    success: bool = False
    backup_path: str = ""
    created: bool = False
    modified: bool = False
    diff_summary: str = ""
    error_message: str = ""
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkbenchExecutionRequest:
    """Phase 16.5 — supervised execution request (requires approval for mutations)."""

    execution_id: str = ""
    proposal_id: str = ""
    approved: bool = False
    elevated_approval: bool = False
    execution_mode: str = "dry_run"  # dry_run | staged | apply
    action_type: str = "write_patch_plan"
    target_paths: list[str] = field(default_factory=list)
    change_plans: list[FileChangePlan] = field(default_factory=list)
    rejection_reason: str = ""
    requested_by: str = "system"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkbenchExecutionResult:
    """Phase 16.5 — supervised execution result (no implicit auto-execution)."""

    execution_id: str = ""
    proposal_id: str = ""
    approved: bool = False
    execution_mode: str = "dry_run"
    action_type: str = ""
    success: bool = False
    blocked: bool = False
    denial_reason: str = ""
    requires_elevated_approval: bool = False
    target_paths: list[str] = field(default_factory=list)
    created_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    backup_paths: list[str] = field(default_factory=list)
    diff_summary: list[str] = field(default_factory=list)
    rollback_available: bool = False
    rollback_hint: str = ""
    error_message: str = ""
    file_records: list[FileChangeRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkbenchProposalView:
    """Phase 16.6 — compact proposal view for command/list operations."""

    proposal_id: str = ""
    proposal_type: str = "no_action_needed"
    title: str = ""
    priority: str = "low"
    risk_level: str = "low"
    confidence: float = 0.0
    requires_human_review: bool = True
    summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkbenchQueueState:
    """Phase 16.6 — current in-memory workbench proposal/selection state."""

    has_proposals: bool = False
    proposal_count: int = 0
    selected_proposal_id: str = ""
    top_proposal_id: str = ""
    top_proposal_type: str = "no_action_needed"
    top_proposal_title: str = ""
    top_proposal_priority: str = "low"
    top_proposal_risk: str = "low"
    approval_needed: bool = True
    last_execution_info: dict[str, Any] = field(default_factory=dict)
    last_rollback_info: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkbenchCommandRequest:
    """Phase 16.6 — structured request for proposal review/approval commands."""

    command_name: str = "show_workbench_status"
    proposal_id: str = ""
    execution_mode: str = "dry_run"  # dry_run | staged | apply
    approved: bool = False
    elevated_approval: bool = False
    requested_by: str = "operator"
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkbenchCommandResult:
    """Phase 16.6 — result of executing one workbench command request."""

    command_name: str = ""
    proposal_id: str = ""
    execution_mode: str = "dry_run"
    approved: bool = False
    elevated_approval: bool = False
    success: bool = False
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str = ""
    execution_result: Optional[WorkbenchExecutionResult] = None
    available_proposals: list[WorkbenchProposalView] = field(default_factory=list)
    queue_state: WorkbenchQueueState = field(default_factory=WorkbenchQueueState)
    last_execution_info: dict[str, Any] = field(default_factory=dict)
    last_rollback_info: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReflectionObservation:
    """Phase 17 — one grounded observation used for reflection."""

    source: str = ""
    key: str = ""
    value: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class SelfModelSnapshot:
    """Phase 17 — soft operational self-model tags and state."""

    self_model_tags: list[str] = field(default_factory=list)
    current_operational_state: str = "uncertain_state"
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReflectionResult:
    """Phase 17 — evidence-based reflection output (no direct behavior override)."""

    reflection_category: str = "uncertain_state_reflection"
    reflection_summary: str = ""
    recent_outcome: str = ""
    outcome_quality: str = "mixed"  # good | mixed | poor
    detected_issue: str = ""
    detected_success: str = ""
    suggested_adjustment: str = ""
    confidence: float = 0.0
    observations: list[ReflectionObservation] = field(default_factory=list)
    self_model: SelfModelSnapshot = field(default_factory=SelfModelSnapshot)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContemplationPrompt:
    """Phase 18 — bounded contemplation input framing."""

    contemplation_theme: str = "certainty_vs_usefulness"
    prompt_text: str = ""
    evidence_keys: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class InternalPriorityView:
    """Phase 18 — soft internal priorities (guidance only)."""

    observe: float = 0.5
    clarify: float = 0.5
    remember: float = 0.5
    adapt: float = 0.5
    maintain: float = 0.5
    engage: float = 0.5
    remain_silent: float = 0.5
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContemplationResult:
    """Phase 18 — bounded internal contemplation output."""

    contemplation_theme: str = "certainty_vs_usefulness"
    contemplation_summary: str = ""
    contemplation_question: str = ""
    contemplation_position: str = ""
    contemplation_confidence: float = 0.0
    guiding_principles: list[str] = field(default_factory=list)
    priority_weights: InternalPriorityView = field(default_factory=InternalPriorityView)
    caution_notes: list[str] = field(default_factory=list)
    evidence_basis: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class CalibrationObservation:
    """
    Phase 21 — one diagnostic calibration reading for a subsystem/metric pair.

    ``suggested_direction`` guides human tuning (or ``watch`` / ``keep``), not runtime auto-adjustment.
    """

    subsystem_name: str
    metric_name: str
    observed_value: float
    status: str  # ok | watch | attention | insufficient_data
    suggested_direction: str  # raise | lower | keep | watch
    confidence: float
    evidence_count: int
    notes: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThresholdReviewResult:
    """Phase 21 — watchlist item: a tuning-sensitive region that may deserve review."""

    area: str
    current_signal: str
    suggested_direction: str
    rationale: str
    evidence_count: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class CalibrationReport:
    """Phase 21 — counters, derived rates, observations, and threshold watchlist."""

    tick_count: int
    rates: dict[str, float]
    observations: list[CalibrationObservation]
    watchlist: list[ThresholdReviewResult]
    counters: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class VoiceTimingDecision:
    """
    Phase 22 — timing / floor / readiness for voice (advisory; does not block ``run_ava``).

    Conservative defaults bias toward **waiting** over blurting when uncertain.
    """

    should_wait: bool = False
    should_yield: bool = False
    should_interrupt: bool = False
    should_respond: bool = True
    response_readiness: float = 0.55
    silence_window_ms: float = 0.0
    pacing_notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class VoiceConversationResult:
    """
    Phase 22 — structured voice turn snapshot for one push-to-talk cycle.

    Gradio supplies a single audio clip per stop event (no streaming STT here); states are
    **soft** interpretations for pacing, prompts, and continuity—not live DSP/VAD truth.
    """

    turn_state: str = "idle"
    user_speaking: bool = False
    assistant_speaking: bool = False
    silence_window_ms: float = 0.0
    timing: VoiceTimingDecision = field(default_factory=VoiceTimingDecision)
    interruption_reason: str = ""
    continuity_hint: str = ""
    pacing_notes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RelationshipSignal:
    """Phase 23 — single bounded social/relationship cue (probabilistic, not a fixed trait label)."""

    name: str = ""
    strength: float = 0.0
    evidence: list[str] = field(default_factory=list)


@dataclass
class InteractionStyleProfile:
    """Phase 23 — soft preference signals in [0, 1]; 0.5 = neutral / unknown."""

    warmth_preference_signal: float = 0.5
    practicality_preference_signal: float = 0.5
    quiet_preference_signal: float = 0.5
    depth_preference_signal: float = 0.5


@dataclass
class SocialContinuityResult:
    """
    Phase 23 — bounded social continuity snapshot for one tick.

    Descriptive only — does **not** authorize behavior overrides or sensitive trait claims.
    """

    familiarity_score: float = 0.5
    trust_signal: float = 0.5
    warmth_preference_signal: float = 0.5
    practicality_preference_signal: float = 0.5
    quiet_preference_signal: float = 0.5
    depth_preference_signal: float = 0.5
    style_profile: InteractionStyleProfile = field(default_factory=InteractionStyleProfile)
    unfinished_thread_present: bool = False
    recurring_topics: list[str] = field(default_factory=list)
    recent_social_tone: str = "neutral"
    relationship_summary: str = ""
    interaction_style_hint: str = "steady_familiar_tone"
    confidence: float = 0.35
    signals: list[RelationshipSignal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryLinkSuggestion:
    """Phase 24 — soft association hint for retrieval / future linking (not a stored graph edge)."""

    link_kind: str = ""
    target_hint: str = ""
    strength: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RefinedMemoryDecision:
    """Phase 24 — refined usefulness decision layered on Phase 11–12 outputs."""

    refined_memory_worthy: bool = False
    refined_memory_class: str = "ignore"
    retention_strength: float = 0.2
    retrieval_priority: float = 0.15
    unfinished_thread_candidate: bool = False
    social_relevance_score: float = 0.35
    episodic_relevance_score: float = 0.25
    pattern_relevance_score: float = 0.25
    suppression_reason: str = ""


@dataclass
class MemoryRefinementResult:
    """Phase 24 — bounded memory refinement snapshot for one tick."""

    decision: RefinedMemoryDecision = field(default_factory=RefinedMemoryDecision)
    link_targets: list[MemoryLinkSuggestion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# --- Phase 25 — dynamic Ollama model routing (cognitive mode → reasoning engine; not identity) ---


@dataclass
class ModelCapabilityEntry:
    """
    Runtime snapshot: config-backed capability profile intersected with live Ollama availability.

    ``source`` is ``config`` for declared profiles; ``discovered`` when the model appeared in
    discovery but had no explicit profile (neutral conservative defaults).
    """

    model_name: str
    available: bool
    cognitive_modes: list[str]
    latency_tendency: float
    reasoning_strength: float
    coding_suitability: float
    summarization_suitability: float
    fallback_priority: int
    source: str = "config"


@dataclass
class ModelRouteCandidate:
    """One scored model choice for a cognitive routing category."""

    model_name: str
    cognitive_mode: str
    score: float
    reason: str = ""


@dataclass
class CognitiveModeResult:
    """Conservative classification of which routing category fits this tick."""

    cognitive_mode: str
    classification_confidence: float
    signals: list[str] = field(default_factory=list)


@dataclass
class ModelRoutingResult:
    """Structured routing decision: which Ollama model to use as the active reasoning engine."""

    classification: CognitiveModeResult
    cognitive_mode: str
    selected_model: str
    fallback_model: str
    routing_reason: str
    routing_confidence: float
    latency_priority: float
    context_priority: float
    quality_priority: float
    model_candidates: list[ModelRouteCandidate] = field(default_factory=list)
    continuity_preserved: bool = True
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# --- Phase 26 — bounded curiosity (structured internal prompts; no autonomous execution) ---


@dataclass
class CuriosityQuestion:
    """Soft internal wording anchor for curiosity (not auto-spoken unless another layer chooses safely)."""

    question_text: str = ""
    anchor_theme: str = ""


@dataclass
class ExplorationSuggestion:
    """Recommended exploration posture — observe, defer, or optional later clarification."""

    kind: str = "none"
    summary: str = ""


@dataclass
class CuriosityResult:
    """Phase 26 — bounded curiosity snapshot for one perception tick."""

    curiosity_triggered: bool = False
    curiosity_theme: str = "no_curiosity_needed"
    curiosity_question: str = ""
    curiosity_reason: str = ""
    curiosity_confidence: float = 0.0
    exploration_mode: str = "none"
    suggested_next_step: str = "no_exploration_needed"
    internal_question: Optional[CuriosityQuestion] = None
    exploration_suggestions: list[ExplorationSuggestion] = field(default_factory=list)
    should_observe: bool = False
    should_clarify: bool = False
    should_defer: bool = False
    boundedness_flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# --- Phase 27 — outcome learning & advisory behavior adjustment signals ---


@dataclass
class OutcomeObservation:
    """Single grounded cue feeding outcome learning for this tick."""

    source: str = ""
    signal_strength: float = 0.0
    detail: str = ""


@dataclass
class BehaviorAdjustmentSuggestion:
    """Soft recommendation posture — descriptive only unless a future phase applies it."""

    posture: str = ""
    summary: str = ""
    target_subsystem: str = ""


@dataclass
class OutcomeLearningResult:
    """Bounded outcome snapshot: what patterns seem to repeat and what adjustment *might* help."""

    outcome_category: str = "no_adjustment_needed"
    outcome_quality: str = "neutral"
    repeated_outcome_pattern: bool = False
    suggested_adjustment: str = ""
    adjustment_confidence: float = 0.18
    adjustment_target: str = ""
    supporting_evidence: list[OutcomeObservation] = field(default_factory=list)
    adjustment_suggestions: list[BehaviorAdjustmentSuggestion] = field(default_factory=list)
    should_strengthen: bool = False
    should_weaken: bool = False
    should_keep: bool = True
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# --- Phase 28 — conversational / emotional nuance (soft guidance; not a response engine) ---


@dataclass
class NuanceSignal:
    """One evidence-weighted cue feeding tone and pacing decisions."""

    source: str = ""
    weight: float = 0.0
    detail: str = ""


@dataclass
class ToneGuidanceProfile:
    """Aggregated soft preferences in 0..1 (0.5 = neutral / leave to runtime prompt)."""

    preferred_tone_category: str = "uncertain_neutral"
    warmth_bias: float = 0.5
    practicality_bias: float = 0.5
    softness_bias: float = 0.5
    seriousness_bias: float = 0.5
    humor_tolerance: float = 0.35


@dataclass
class ConversationalNuanceResult:
    """Structured interaction-style guidance for prompts and future style hooks only."""

    nuance_tone: str = "uncertain_neutral"
    warmth_level: float = 0.5
    practicality_level: float = 0.5
    softness_level: float = 0.5
    seriousness_level: float = 0.5
    humor_tolerance: float = 0.35
    verbosity_bias: float = 0.52
    pacing_bias: float = 0.5
    restraint_bias: float = 0.48
    emotional_pacing_hint: str = "steady"
    nuance_summary: str = ""
    confidence: float = 0.38
    signals: list[NuanceSignal] = field(default_factory=list)
    tone_profile: ToneGuidanceProfile = field(default_factory=ToneGuidanceProfile)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# --- Phase 29 — multi-session strategic continuity (bounded carryover; guidance only) ---


@dataclass
class ContinuityThread:
    """One prioritized carryover thread — short summary, not a memory blob."""

    category: str = "no_relevant_carryover"
    summary: str = ""
    relevance: float = 0.0
    scope: str = "none"  # immediate | recent | background | none
    confidence: float = 0.0
    source: str = ""
    evidence_note: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionCarryoverSummary:
    """Compact headline for logging and UI hooks (Phase 29)."""

    headline: str = ""
    thread_count: int = 0
    top_category: str = "no_relevant_carryover"


@dataclass
class StrategicContinuityResult:
    """
    Cross-session continuity snapshot for one tick — descriptive only.

    Does not persist or auto-execute; merges durable files with current pipeline evidence.
    """

    active_threads: list[ContinuityThread] = field(default_factory=list)
    unfinished_threads: list[ContinuityThread] = field(default_factory=list)
    strategic_priorities: list[str] = field(default_factory=list)
    relationship_carryover: str = ""
    maintenance_carryover: str = ""
    recent_adjustment_carryover: str = ""
    session_carryover: SessionCarryoverSummary = field(default_factory=SessionCarryoverSummary)
    continuity_summary: str = ""
    continuity_confidence: float = 0.0
    continuity_scope: str = "none"
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# --- Phase 30 — supervised self-improvement loop (descriptive only; no auto-approval) ---


class ImprovementStepStatus:
    """Valid ``loop_stage`` string values for :class:`ImprovementLoopResult` (Phase 30)."""

    NO_ACTIVE_LOOP = "no_active_loop"
    ISSUE_DETECTED = "issue_detected"
    PROPOSAL_READY = "proposal_ready"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED_READY_FOR_EXECUTION = "approved_ready_for_execution"
    EXECUTION_IN_PROGRESS = "execution_in_progress"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_FAILED = "execution_failed"
    ROLLBACK_AVAILABLE = "rollback_available"
    ROLLBACK_USED = "rollback_used"
    POST_EXECUTION_REFLECTION = "post_execution_reflection"
    RESOLVED = "resolved"


@dataclass
class ImprovementCycle:
    """One tracked improvement concern (may mirror workbench or continuity maintenance)."""

    cycle_key: str = ""
    headline: str = ""
    source: str = ""
    linked_proposal_id: str = ""
    status_hint: str = ""


@dataclass
class ImprovementLoopResult:
    """
    Supervised maintenance / self-improvement snapshot for one tick.

    Does not approve, execute, or mutate files — only structured situational awareness.
    """

    loop_active: bool = False
    loop_stage: str = ImprovementStepStatus.NO_ACTIVE_LOOP
    loop_summary: str = ""
    active_issue: str = ""
    active_proposal_id: str = ""
    active_proposal_type: str = ""
    awaiting_approval: bool = False
    awaiting_execution: bool = False
    execution_recently_succeeded: bool = False
    execution_recently_failed: bool = False
    rollback_recently_used: bool = False
    suggested_next_supervised_step: str = ""
    loop_confidence: float = 0.0
    cycles: list[ImprovementCycle] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# --- Phase 31 — heartbeat runtime & bounded adaptive learning ---


class HeartbeatMode:
    """Soft operational modes for :class:`HeartbeatTickResult` (Phase 31)."""

    IDLE_MONITORING = "idle_monitoring"
    ACTIVE_PRESENCE = "active_presence"
    CONVERSATION_ACTIVE = "conversation_active"
    MAINTENANCE_WATCH = "maintenance_watch"
    LEARNING_REVIEW = "learning_review"
    QUIET_RECOVERY = "quiet_recovery"
    NO_HEARTBEAT = "no_heartbeat"


@dataclass
class HeartbeatEvent:
    """One heartbeat-relevant observation (bounded, descriptive)."""

    kind: str = ""
    detail: str = ""
    significance: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class HeartbeatState:
    """Persisted heartbeat cadence counters (Phase 31)."""

    tick_id: int = 0
    last_wallclock: float = 0.0
    last_rich_learning_ts: float = 0.0
    last_digest: str = ""
    silence_streak: int = 0
    last_emit_sig: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class HeartbeatTickResult:
    """One heartbeat cycle snapshot — lightweight background continuity."""

    heartbeat_active: bool = False
    heartbeat_tick_id: int = 0
    heartbeat_mode: str = HeartbeatMode.NO_HEARTBEAT
    last_tick_time: float = 0.0
    tick_reason: str = ""
    important_state_change: bool = False
    suggested_action: str = ""
    should_remain_silent: bool = True
    heartbeat_summary: str = ""
    carryover: Optional["HeartbeatCarryoverState"] = None
    events: list[HeartbeatEvent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class LearningPreferenceShift:
    """One bounded preference delta for logging / inspection (Phase 31)."""

    focus: str = ""
    before: float = 0.5
    after: float = 0.5
    reason: str = ""
    evidence: str = ""


@dataclass
class HeartbeatCarryoverState:
    """Small restart-safe snapshot for heartbeat continuity (not a memory dump)."""

    last_recorded_mode: str = ""
    strategic_headline_digest: str = ""
    model_route_fallback_streak: int = 0
    last_selected_model: str = ""
    last_fallback_model: str = ""
    workbench_signal_digest: str = ""
    tick_id_at_snapshot: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdaptiveLearningResult:
    """Bounded adaptive preference adjustments — evidence-weighted, no safety rewrite."""

    learning_update_applied: bool = False
    learning_focus: str = ""
    learning_summary: str = ""
    learning_confidence: float = 0.0
    preference_shifts: list["LearningPreferenceShift"] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class StartupResumeSummary:
    """Phase 32 — quiet startup carryover flags (descriptive only)."""

    heartbeat_carryover_loaded: bool = False
    adaptive_preferences_loaded: bool = False
    session_state_present: bool = False
    quiet_monitoring_default: bool = True
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class OperatorStatusSnapshot:
    """Phase 32 — compact operator-facing lines (no approval side effects)."""

    heartbeat_line: str = ""
    learning_line: str = ""
    maintenance_line: str = ""
    threads_line: str = ""
    workbench_line: str = ""
    rollup: str = ""


@dataclass
class RuntimePresenceResult:
    """Phase 32 — unified runtime / maintenance / learning visibility (bounded)."""

    presence_mode: str = "quiet_monitoring"
    startup_loaded: bool = False
    continuity_loaded: bool = False
    active_issue_summary: str = ""
    active_threads_summary: str = ""
    maintenance_status_summary: str = ""
    learning_status_summary: str = ""
    heartbeat_status_summary: str = ""
    operator_status_summary: str = ""
    ready_state: str = "steady"
    operator_view: Optional[OperatorStatusSnapshot] = None
    startup_resume: Optional[StartupResumeSummary] = None
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConcernRecord:
    """Persistent concern entry (reconciled against current evidence; not a memory dump)."""

    concern_id: str
    concern_type: str
    status: str
    created_at: float
    last_seen_at: float
    supporting_evidence: dict[str, Any] = field(default_factory=dict)
    current_evidence: dict[str, Any] = field(default_factory=dict)
    resolution_reason: str = ""
    should_surface_now: bool = False
    cooldown_until: float = 0.0
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConcernStatusUpdate:
    """One status transition from a reconciliation pass."""

    concern_id: str
    concern_type: str
    prior_status: str
    new_status: str
    resolution_reason: str = ""
    should_surface_now: bool = False
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConcernReconciliationResult:
    """Output of startup/runtime concern reconciliation."""

    updates: list[ConcernStatusUpdate] = field(default_factory=list)
    active_count: int = 0
    top_active_concern: str = ""
    summary: str = ""
    surfaced_any: bool = False
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContinuityOutput:
    """Stage 5 — identity continuity / tracking + Phase 7 structured result."""

    stage: StageResult
    last_stable_identity: Optional[str] = None
    continuity_confidence: float = 0.0
    tracking_note: str = ""
    structured: Optional[ContinuityResult] = None


@dataclass
class InterpretationOutput:
    """Stage 6 — emotion + salience (scene / LLM interpretation is a future hook)."""

    stage: StageResult
    face_emotion: Optional[str] = None
    salience: float = 0.2
    scene_summary_hint: str = ""
    # Phase 6 — structured salience (ranked items); salience scalar blends with legacy engagement
    salience_structured: Optional[SalienceResult] = None


@dataclass
class PackageOutput:
    """Stage 7 — final bundle marker (logging + adapter input)."""

    stage: StageResult


@dataclass
class PerceptionPipelineBundle:
    """All stage outputs for one tick; :func:`brain.perception_state_adapter.bundle_to_perception_state` maps to ``PerceptionState``."""

    acquisition: AcquisitionOutput
    quality: QualityOutput
    detection: DetectionOutput
    recognition: RecognitionOutput
    continuity: ContinuityOutput
    interpretation: InterpretationOutput
    package: PackageOutput
    # Raw resolved frame reference for adapter (frame + timestamps)
    resolved: Any = None
    user_text: str = ""
    # Phase 8 — after continuity
    identity_resolution: Optional[IdentityResolutionResult] = None
    # Phase 9 — after identity resolution
    scene_summary: Optional[SceneSummaryResult] = None
    # Phase 10 — after scene summary (semantic events; separate from emotion/salience stage above)
    interpretation_layer: Optional[InterpretationLayerResult] = None
    # Phase 11 — after interpretation layer
    perception_memory: Optional[PerceptionMemoryOutput] = None
    # Phase 12 — after perception memory output
    memory_importance: Optional[MemoryImportanceResult] = None
    # Phase 13 — after memory importance scoring
    pattern_learning: Optional[PatternLearningResult] = None
    # Phase 14 — after pattern learning
    proactive_trigger: Optional[ProactiveTriggerResult] = None
    # Phase 15 — startup/recurring health diagnostics
    selftests: Optional[SelfTestRunResult] = None
    # Phase 16 — repair workbench proposal generation
    workbench: Optional[WorkbenchProposalResult] = None
    # Phase 17 — reflection and self-model synthesis
    reflection: Optional[ReflectionResult] = None
    # Phase 18 — bounded philosophical/internal contemplation
    contemplation: Optional[ContemplationResult] = None
    # Phase 23 — social continuity / soft relationship modeling
    social_continuity: Optional[SocialContinuityResult] = None
    # Phase 24 — long-term memory refinement (additive on memory scoring)
    memory_refinement: Optional[MemoryRefinementResult] = None
    # Phase 25 — explainable model routing (Ollama “reasoning engine” selection)
    model_routing: Optional[ModelRoutingResult] = None
    # Phase 26 — bounded curiosity / exploratory intent (structured; non-executing)
    curiosity: Optional[CuriosityResult] = None
    # Phase 27 — outcome learning (advisory adjustment signals — no silent retuning)
    outcome_learning: Optional[OutcomeLearningResult] = None
    # Phase 28 — conversational / emotional nuance (guidance only)
    conversational_nuance: Optional[ConversationalNuanceResult] = None
    # Phase 29 — multi-session strategic continuity (bounded carryover)
    strategic_continuity: Optional[StrategicContinuityResult] = None
    # Phase 30 — supervised self-improvement loop (descriptive only)
    improvement_loop: Optional[ImprovementLoopResult] = None
    # Phase 31 — heartbeat & bounded adaptive learning (quiet background continuity)
    heartbeat: Optional[HeartbeatTickResult] = None
    adaptive_learning: Optional[AdaptiveLearningResult] = None
    runtime_presence: Optional[RuntimePresenceResult] = None
    concern_reconciliation: Optional[ConcernReconciliationResult] = None
