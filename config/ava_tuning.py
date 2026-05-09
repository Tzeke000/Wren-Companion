"""
Phase 20 — centralized tuning knobs for perception, memory, initiative-adjacent layers.

Single ownership for thresholds/weights/cadences used across ``brain/*`` modules.
Defaults preserve pre–Phase-20 runtime behavior; adjust values here rather than scattering
magic numbers in feature code.

See :mod:`summarize_tuning_config` for a compact JSON-serializable snapshot (debug/logging).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Phase 4–5 — frame quality + blur (``brain.frame_quality``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QualityConfig:
    weak_min_overall: float = 0.28
    usable_min_overall: float = 0.50
    overall_blur_weight: float = 0.55
    overall_lum_weight: float = 0.45
    reason_flags_overall_scale: float = 0.82
    lum_comfort_center: float = 0.42
    lum_comfort_abs_scale: float = 2.0
    darkness_norm_divisor: float = 0.15
    darkness_floor_under_lum: float = 0.05
    darkness_floor_cap: float = 0.15
    overexposure_start_lum: float = 0.85
    overexposure_span: float = 0.15
    mean_lum_very_dark: float = 0.07
    mean_lum_low_light: float = 0.12
    mean_lum_overexposed: float = 0.93
    motion_resize_ref_px: float = 160.0
    motion_diff_divisor: float = 35.0
    canny_low: int = 50
    canny_high: int = 150
    occlusion_fallback_score: float = 0.85
    empty_assessment_overall: float = 0.0


@dataclass(frozen=True)
class BlurConfig:
    var_soft_max: float = 45.0
    var_sharp_min: float = 120.0
    var_scale: float = 180.0
    sharp_recognition: float = 1.0
    sharp_expression: float = 1.0
    sharp_interpretation: float = 1.0
    soft_recognition: float = 0.92
    soft_expression: float = 0.88
    soft_interpretation: float = 0.96
    blurry_recognition: float = 0.78
    blurry_expression: float = 0.72
    blurry_interpretation: float = 0.90
    empty_blur_confidence: float = 0.78
    empty_blur_expression: float = 0.72
    empty_blur_interpretation: float = 0.90


@dataclass(frozen=True)
class ConfidenceScaleConfig:
    """Overall quality label → (recognition, expression) scales (Phase 4)."""

    usable_recognition: float = 1.0
    usable_expression: float = 1.0
    weak_recognition: float = 0.88
    weak_expression: float = 0.82
    unreliable_recognition: float = 0.72
    unreliable_expression: float = 0.65


# ---------------------------------------------------------------------------
# Phase 6 — salience (``brain.salience``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SalienceConfig:
    w_center: float = 0.26
    w_prominence: float = 0.22
    w_motion: float = 0.14
    w_recognition: float = 0.20
    w_engagement: float = 0.18
    legacy_blend: float = 0.38
    structured_blend: float = 0.62
    center_default_no_frame: float = 0.35
    prominence_default_no_frame: float = 0.2
    prominence_area_exp: float = 0.55
    prominence_area_mult: float = 4.2
    recognition_has_face_no_label: float = 0.72
    recognition_no_face: float = 0.35
    rect_pick_center_weight: float = 0.55
    rect_pick_prominence_weight: float = 0.45
    secondary_recognition: float = 0.55
    secondary_item_scale: float = 0.82
    secondary_engagement: float = 0.55
    secondary_min_score: float = 0.08
    cascade_center: float = 0.55
    cascade_prominence_single: float = 0.45
    cascade_prominence_multi: float = 0.38
    motion_scene_cue_threshold: float = 0.28
    motion_scene_cue_base: float = 0.25
    motion_scene_cue_mult: float = 0.9
    top_item_threshold: float = 0.08
    nonface_legacy_a: float = 0.55
    nonface_legacy_b: float = 0.45
    combined_scalar_min: float = 0.15
    combined_scalar_max: float = 1.0


# ---------------------------------------------------------------------------
# Phase 7 — continuity (``brain.continuity``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ContinuityConfig:
    max_frame_gap: int = 48
    max_seconds_gap: float = 3.2
    spatial_dist_ref: float = 0.17
    likely_same_spatial_min: float = 0.58
    size_ratio_ref: float = 0.48
    spatial_combine_dist_weight: float = 0.55
    spatial_combine_size_weight: float = 0.45
    time_decay_lambda: float = 0.65
    time_decay_cap_sec: float = 6.0
    gap_penalty_scale: float = 0.55
    salience_label_partial_match: float = 0.85
    no_face_prior_confidence: float = 0.08
    unknown_face_no_rect_confidence: float = 0.12
    confirmed_lbph_base: float = 0.42
    confirmed_lbph_spatial: float = 0.38
    confirmed_lbph_salience: float = 0.12
    carry_conf_base: float = 0.38
    carry_conf_spatial: float = 0.52
    carry_conf_salience: float = 0.1
    carry_conf_cap: float = 0.88
    unknown_face_conf_floor: float = 0.14
    unknown_face_conf_scale: float = 0.22


# ---------------------------------------------------------------------------
# Phase 8 — identity fallback (``brain.identity_fallback``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IdentityConfig:
    confirm_lbph_min: float = 0.41
    likely_carry_conf_cap: float = 0.9
    likely_carry_continuity_scale: float = 0.92
    likely_carry_floor: float = 0.28
    unknown_face_lbph_scale: float = 0.38
    unknown_face_lbph_cap: float = 0.55
    unknown_face_lbph_floor: float = 0.18
    unknown_face_identity_cap: float = 0.62


# ---------------------------------------------------------------------------
# Phase 9 — scene summary (``brain.scene_summary``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SceneSummaryConfig:
    motion_changed_threshold: float = 0.58
    lighting_darkness_low: float = 0.22
    lighting_overexposure_low: float = 0.52


# ---------------------------------------------------------------------------
# Phase 10 — interpretation layer (``brain.interpretation``)
# ---------------------------------------------------------------------------
# Stored as tuple pairs for immutability; converted to dict at import sites.
INTERPRETATION_EVENT_PRIORITY_ITEMS: tuple[tuple[str, float], ...] = (
    ("person_entered", 1.0),
    ("person_left", 0.95),
    ("scene_changed", 0.88),
    ("unknown_person_present", 0.72),
    ("known_person_present", 0.68),
    ("likely_known_person_present", 0.64),
    ("occupied_or_busy_visual_state", 0.58),
    ("user_or_subject_engaged", 0.55),
    ("user_or_subject_disengaged", 0.42),
    ("no_meaningful_change", 0.28),
    ("uncertain_visual_state", 0.18),
)


@dataclass(frozen=True)
class InterpretationConfig:
    error_fallback_confidence: float = 0.22
    default_primary_priority: float = 0.4


# ---------------------------------------------------------------------------
# Phase 11 — perception memory (``brain.perception_memory``)
# ---------------------------------------------------------------------------
MEMORY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "person_entered",
        "person_left",
        "known_person_present",
        "likely_known_person_present",
        "unknown_person_present",
        "scene_changed",
        "room_became_empty",
        "occupied_or_busy_visual_state",
        "uncertain_visual_state",
        "no_meaningful_change",
    }
)


@dataclass(frozen=True)
class MemoryEventConfig:
    """Duplicate guard / error fallback tuning for perception-memory generation."""

    error_event_confidence: float = 0.2
    error_event_priority: float = 0.18


# ---------------------------------------------------------------------------
# Phase 12 — memory scoring (``brain.memory_scoring``)
# ---------------------------------------------------------------------------
MEMORY_SCORING_BASE_WEIGHTS_ITEMS: tuple[tuple[str, float], ...] = (
    ("person_entered", 0.58),
    ("person_left", 0.54),
    ("known_person_present", 0.44),
    ("likely_known_person_present", 0.36),
    ("unknown_person_present", 0.50),
    ("scene_changed", 0.42),
    ("room_became_empty", 0.38),
    ("occupied_or_busy_visual_state", 0.34),
    ("uncertain_visual_state", 0.18),
    ("no_meaningful_change", 0.06),
)

MEMORY_SCORING_CLASS_ITEMS: tuple[tuple[str, str], ...] = (
    ("person_entered", "episodic_candidate"),
    ("person_left", "episodic_candidate"),
    ("known_person_present", "episodic_candidate"),
    ("likely_known_person_present", "transient_context"),
    ("unknown_person_present", "episodic_candidate"),
    ("scene_changed", "pattern_candidate"),
    ("room_became_empty", "transient_context"),
    ("occupied_or_busy_visual_state", "pattern_candidate"),
    ("uncertain_visual_state", "ignore"),
    ("no_meaningful_change", "ignore"),
)


@dataclass(frozen=True)
class MemoryScoringConfig:
    ignore_label_max: float = 0.25
    low_label_max: float = 0.45
    medium_label_max: float = 0.72
    default_event_weight: float = 0.2
    novelty_layer_scale: float = 0.45
    novelty_transition_bonus: float = 0.12
    novelty_scene_changed_bonus: float = 0.14
    novelty_scene_summary_bonus: float = 0.05
    relevance_layer_scale: float = 0.32
    relevance_confirmed_bonus: float = 0.18
    relevance_likely_bonus: float = 0.10
    relevance_unknown_face_bonus: float = 0.14
    relevance_known_present_bonus: float = 0.10
    relevance_likely_known_penalty: float = 0.03
    relevance_unknown_present_bonus: float = 0.08
    uncertainty_uncertain_event: float = 0.20
    uncertainty_not_worthy_candidate: float = 0.06
    uncertainty_no_meaningful_change: float = 0.08
    uncertainty_scene_uncertain: float = 0.08
    uncertainty_stale_acquisition: float = 0.10
    uncertainty_suppress_flip: float = 0.03
    quality_unreliable: float = 0.12
    quality_weak: float = 0.05
    quality_blurry: float = 0.08
    quality_soft: float = 0.03
    continuity_bonus_cap: float = 0.12
    continuity_bonus_scale: float = 0.12
    continuity_suppress_flip_bonus: float = 0.03
    novelty_weight: float = 0.35
    relevance_weight: float = 0.30
    repetition_no_change_penalty: float = 0.12
    repetition_per_hit_scale: float = 0.05
    repetition_per_hit_cap: float = 0.2
    repetition_suppressed_duplicate: float = 0.18
    worthy_score_min: float = 0.33
    no_meaningful_change_worthy_cap: float = 0.4
    fallback_uncertainty_penalty: float = 0.35
    sig_snippet_len: int = 80


# ---------------------------------------------------------------------------
# Phase 13 — pattern learning (``brain.pattern_learning``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PatternConfig:
    strength_familiarity_weight: float = 0.6
    strength_unusualness_weight: float = 0.4
    recurrence_score_divisor: float = 8.0
    candidate_strength_min: float = 0.35
    memory_worthy_unusualness_bump: float = 0.08
    stale_acquisition_unusualness_bump: float = 0.05
    suppress_flip_familiarity_bump: float = 0.05
    transition_min_familiarity: float = 0.30
    transition_min_unusualness: float = 0.40
    baseline_idle_min_familiarity: float = 0.40
    baseline_idle_min_unusualness: float = 0.50
    unusual_min_familiarity: float = 0.20
    unusual_min_unusualness: float = 0.55
    uncommon_transition_unusualness: float = 0.75
    unusual_bonus: float = 0.12
    pattern_default_min_familiarity: float = 0.35
    pattern_default_min_unusualness: float = 0.45


# ---------------------------------------------------------------------------
# Phase 14 — proactive triggers (``brain.proactive_triggers``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProactiveConfig:
    spam_repeat_threshold: int = 2
    stable_no_change_importance_max: float = 0.30
    person_entered_score_base: float = 0.56
    person_entered_importance_scale: float = 0.24
    person_entered_priority: float = 0.62
    unknown_person_importance_min: float = 0.42
    unknown_person_score_base: float = 0.60
    unknown_person_importance_scale: float = 0.18
    unknown_person_priority: float = 0.72
    return_absence_score_base: float = 0.52
    return_absence_continuity_scale: float = 0.16
    return_absence_priority: float = 0.65
    unusual_pattern_unusualness_min: float = 0.58
    unusual_pattern_score_base: float = 0.46
    unusual_pattern_unusualness_scale: float = 0.34
    unusual_pattern_priority: float = 0.66
    notable_change_importance_min: float = 0.38
    notable_change_score_base: float = 0.44
    notable_change_importance_scale: float = 0.28
    notable_change_priority: float = 0.60
    check_in_familiarity_min: float = 0.60
    check_in_score_base: float = 0.36
    check_in_familiarity_scale: float = 0.20
    check_in_priority: float = 0.42
    hold_silence_score: float = 0.80
    hold_silence_priority: float = 0.88


# ---------------------------------------------------------------------------
# Phase 15 — self-tests (``brain.selftests``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SelfTestConfig:
    recurring_interval_sec: float = 30.0


# ---------------------------------------------------------------------------
# Phase 16 — workbench (``brain.workbench``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkbenchConfig:
    recurring_warning_streak_min: int = 3
    """Proposal types to omit from the repair queue (merge with state/workbench/suppress_proposals.json)."""
    suppress_proposal_types: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Phase 17–18 — reflection / contemplation (lighter extraction of key thresholds)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReflectionConfig:
    default_confidence: float = 0.35
    warn_repeat_min: int = 2
    observation_confidence: float = 0.75
    failed_category_confidence: float = 0.82
    failed_checks_observation_confidence: float = 0.88
    repeated_warning_category_confidence: float = 0.72
    repeated_warning_observation_confidence: float = 0.76
    workbench_category_confidence: float = 0.66
    workbench_observation_confidence: float = 0.70
    execution_success_confidence: float = 0.78
    execution_success_observation_confidence: float = 0.82
    blocked_confidence: float = 0.74
    blocked_observation_confidence: float = 0.78
    proactive_success_importance_min: float = 0.45
    proactive_success_unusualness_max: float = 0.55
    proactive_success_category_confidence: float = 0.64
    proactive_observation_confidence: float = 0.66
    baseline_familiarity_min: float = 0.55
    baseline_category_confidence: float = 0.62
    baseline_observation_confidence: float = 0.68
    uncertain_default_confidence: float = 0.42
    uncertain_observation_confidence: float = 0.55
    self_model_confidence_scale: float = 0.92


@dataclass(frozen=True)
class ContemplationConfig:
    default_confidence: float = 0.46
    maintenance_confidence: float = 0.74
    continuity_theme_confidence: float = 0.63
    continuity_familiarity_min: float = 0.55
    observation_theme_confidence: float = 0.70
    significance_importance_min: float = 0.58
    significance_confidence: float = 0.62
    unusual_theme_min: float = 0.62
    unusual_theme_confidence: float = 0.58
    warning_repeat_min: int = 2
    consistency_theme_confidence: float = 0.60
    suppression_theme_confidence: float = 0.56
    low_confidence_threshold: float = 0.5
    # Soft internal priority weights (Phase 20 — same numeric recipe as pre-centralization)
    priority_maintain_base: float = 0.45
    priority_maintain_failed_scale: float = 0.12
    priority_maintain_workbench_bonus: float = 0.14
    priority_observe_base: float = 0.40
    priority_observe_untrusted_vision_bonus: float = 0.18
    priority_observe_unusual_mid_threshold: float = 0.55
    priority_observe_unusual_mid_bonus: float = 0.10
    priority_clarify_base: float = 0.36
    priority_clarify_unusual_high_threshold: float = 0.60
    priority_clarify_unusual_high_bonus: float = 0.14
    priority_clarify_warning_bonus: float = 0.10
    priority_remember_base: float = 0.34
    priority_remember_importance_scale: float = 0.28
    priority_remember_event_bonus: float = 0.08
    priority_adapt_base: float = 0.33
    priority_adapt_warning_repeat_bonus: float = 0.16
    priority_adapt_repair_blocked_bonus: float = 0.12
    priority_engage_base: float = 0.30
    priority_engage_trigger_bonus: float = 0.18
    priority_engage_suppression_penalty: float = 0.12
    priority_silent_base: float = 0.28
    priority_silent_suppression_bonus: float = 0.20
    priority_silent_untrusted_vision_bonus: float = 0.14
    priority_failed_engage_penalty: float = 0.12
    priority_failed_silent_bonus: float = 0.12


# ---------------------------------------------------------------------------
# Phase 25 — Ollama model routing (``brain.model_routing``)
# ---------------------------------------------------------------------------
# Default: every cognitive mode maps to the **same** tag string so Phase 25 ships with
# zero behavior change until you deliberately set distinct models here (and pull them in Ollama).
# Add more :class:`ModelCapabilityProfileDef` rows for each local model you use; the runtime
# registry is **filtered** by live ``/api/tags`` or ``ollama list`` discovery.

_COGNITIVE_MODES_ALL: tuple[str, ...] = (
    "social_chat_mode",
    "deep_reasoning_mode",
    "coding_repair_mode",
    "memory_maintenance_mode",
    "perception_support_mode",
    "fallback_safe_mode",
)


@dataclass(frozen=True)
class ModelCapabilityProfileDef:
    """
    Declarative capability profile for a model name (tendencies 0..1; lower ``fallback_priority`` = better last-resort).
    Intentional modes list which cognitive routes this model is a good match for.
    requires_internet=True: model only usable when connectivity monitor reports online.
    """

    model_name: str
    cognitive_modes: tuple[str, ...] = _COGNITIVE_MODES_ALL
    latency_tendency: float = 0.5
    reasoning_strength: float = 0.5
    coding_suitability: float = 0.5
    summarization_suitability: float = 0.5
    fallback_priority: int = 100
    vision_capable: bool = False
    requires_internet: bool = False
    vram_required_gb: float = 0.0
    src: str = "config"


# Config profiles override neutral "discovered" entries for the same tag name.
DEFAULT_MODEL_CAPABILITY_PROFILES: tuple[ModelCapabilityProfileDef, ...] = (
    ModelCapabilityProfileDef(
        model_name="ava-personal:latest",
        cognitive_modes=("social_chat_mode", "deep_reasoning_mode", "memory_maintenance_mode", "fallback_safe_mode"),
        latency_tendency=0.55,
        reasoning_strength=0.75,
        coding_suitability=0.65,
        summarization_suitability=0.72,
        fallback_priority=3,
        src="finetuned",
    ),
    ModelCapabilityProfileDef(
        model_name="llama3.1:8b",
        cognitive_modes=_COGNITIVE_MODES_ALL,
        latency_tendency=0.55,
        reasoning_strength=0.62,
        coding_suitability=0.58,
        summarization_suitability=0.55,
        fallback_priority=10,
    ),
    ModelCapabilityProfileDef(
        model_name="mistral:7b",
        cognitive_modes=("social_chat_mode", "fallback_safe_mode"),
        latency_tendency=0.75,
        reasoning_strength=0.55,
        coding_suitability=0.52,
        summarization_suitability=0.58,
        fallback_priority=5,
    ),
    ModelCapabilityProfileDef(
        model_name="gemma2:9b",
        cognitive_modes=("coding_repair_mode", "deep_reasoning_mode", "memory_maintenance_mode"),
        latency_tendency=0.60,
        reasoning_strength=0.65,
        coding_suitability=0.68,
        summarization_suitability=0.70,
        fallback_priority=8,
    ),
    ModelCapabilityProfileDef(
        model_name="qwen2.5:14b",
        cognitive_modes=("deep_reasoning_mode", "memory_maintenance_mode"),
        latency_tendency=0.35,
        reasoning_strength=0.85,
        coding_suitability=0.80,
        summarization_suitability=0.82,
        fallback_priority=15,
    ),
    ModelCapabilityProfileDef(
        model_name="deepseek-r1:8b",
        cognitive_modes=("deep_reasoning_mode", "coding_repair_mode"),
        latency_tendency=0.55,
        reasoning_strength=0.88,
        coding_suitability=0.75,
        summarization_suitability=0.70,
        fallback_priority=12,
    ),
    ModelCapabilityProfileDef(
        model_name="deepseek-r1:14b",
        cognitive_modes=("deep_reasoning_mode", "coding_repair_mode"),
        latency_tendency=0.35,
        reasoning_strength=0.92,
        coding_suitability=0.82,
        summarization_suitability=0.78,
        fallback_priority=12,
    ),
    ModelCapabilityProfileDef(
        model_name="mistral-small3.2",
        cognitive_modes=("social_chat_mode", "fallback_safe_mode"),
        latency_tendency=0.80,
        reasoning_strength=0.65,
        coding_suitability=0.55,
        summarization_suitability=0.62,
        fallback_priority=6,
    ),
    ModelCapabilityProfileDef(
        model_name="llava:13b",
        cognitive_modes=("perception_support_mode",),
        latency_tendency=0.40,
        reasoning_strength=0.60,
        coding_suitability=0.45,
        summarization_suitability=0.58,
        fallback_priority=50,
        vision_capable=True,
    ),
    ModelCapabilityProfileDef(
        model_name="qwen2.5:32b",
        cognitive_modes=("deep_reasoning_mode",),
        latency_tendency=0.20,
        reasoning_strength=0.95,
        coding_suitability=0.90,
        summarization_suitability=0.88,
        fallback_priority=20,
    ),
    # ── Cloud models (requires_internet=True) ────────────────────────────────
    ModelCapabilityProfileDef(
        model_name="kimi-k2.6:cloud",
        cognitive_modes=("deep_reasoning_mode", "coding_repair_mode"),
        latency_tendency=0.70,
        reasoning_strength=0.95,
        coding_suitability=0.92,
        summarization_suitability=0.90,
        fallback_priority=2,
        requires_internet=True,
        src="ollama_cloud",
    ),
    ModelCapabilityProfileDef(
        model_name="qwen3.5:cloud",
        cognitive_modes=("deep_reasoning_mode", "coding_repair_mode", "memory_maintenance_mode"),
        latency_tendency=0.65,
        reasoning_strength=0.93,
        coding_suitability=0.90,
        summarization_suitability=0.88,
        fallback_priority=2,
        vision_capable=True,
        requires_internet=True,
        src="ollama_cloud",
    ),
    ModelCapabilityProfileDef(
        model_name="glm-5.1:cloud",
        cognitive_modes=("deep_reasoning_mode", "coding_repair_mode"),
        latency_tendency=0.60,
        reasoning_strength=0.90,
        coding_suitability=0.88,
        summarization_suitability=0.85,
        fallback_priority=3,
        requires_internet=True,
        src="ollama_cloud",
    ),
    ModelCapabilityProfileDef(
        model_name="minimax-m2.7:cloud",
        cognitive_modes=("social_chat_mode", "memory_maintenance_mode", "fallback_safe_mode"),
        latency_tendency=0.50,
        reasoning_strength=0.85,
        coding_suitability=0.85,
        summarization_suitability=0.88,
        fallback_priority=3,
        requires_internet=True,
        src="ollama_cloud",
    ),
    # ── Local models (may not be downloaded yet) ─────────────────────────────
    ModelCapabilityProfileDef(
        model_name="gemma4",
        cognitive_modes=("deep_reasoning_mode", "coding_repair_mode"),
        latency_tendency=0.40,
        reasoning_strength=0.88,
        coding_suitability=0.85,
        summarization_suitability=0.85,
        fallback_priority=11,
        vram_required_gb=16.0,
        src="config",
    ),
    ModelCapabilityProfileDef(
        model_name="qwen3.5",
        cognitive_modes=("deep_reasoning_mode", "coding_repair_mode", "memory_maintenance_mode"),
        latency_tendency=0.45,
        reasoning_strength=0.92,
        coding_suitability=0.90,
        summarization_suitability=0.88,
        fallback_priority=10,
        vision_capable=True,
        vram_required_gb=11.0,
        src="config",
    ),
)


@dataclass(frozen=True)
class ModelRoutingConfig:
    """Per–cognitive-mode preferred models; fallback + global safety net."""

    default_model: str = "llama3.1:8b"
    social_chat_model: str = "ava-personal:latest"
    deep_reasoning_model: str = "qwen2.5:14b"
    coding_repair_model: str = "gemma2:9b"
    memory_maintenance_model: str = "qwen2.5:14b"
    perception_support_model: str = "llama3.1:8b"
    # Uncertain classification / conservative path — same default tag unless you tune it.
    fallback_safe_model: str = "llama3.1:8b"
    # When preferred + per-mode fallback are missing from Ollama's tag list.
    global_fallback_model: str = "llama3.1:8b"
    # Throttle for lightweight ``/api/tags`` polling.
    ollama_tags_poll_seconds: float = 55.0
    # --- routing stability (anti-thrashing; identity stays stable via prompts — engine stickiness here)
    routing_min_switch_gain: float = 0.14
    """Minimum capability-fit gain required to abandon the previous effective model."""
    routing_weak_mode_margin_stick: float = 0.095
    """When top-two cognitive-mode scores are closer than this, prefer staying on current model if viable."""
    routing_social_stickiness_weight: float = 0.22
    """Boost switch resistance when relationship context suggests conversational continuity."""
    routing_switch_cooldown_seconds: float = 8.0
    """Minimum wall time between engine switches unless bypassed by strong mode margin or urgency."""
    routing_cooldown_bypass_margin: float = 0.22
    """If top-two mode score gap exceeds this, cooldown may be bypassed."""
    routing_suitability_floor: float = 0.38
    """Minimum profile fit vs active mode for the previous model to be considered still “good enough”."""
    # --- social/fallback scoring knobs (Phase 36 tuning)
    social_short_message_base_score: float = 0.45
    social_short_message_boost: float = 0.20
    social_no_question_boost: float = 0.10
    social_greeting_boost: float = 0.15
    social_evening_boost: float = 0.05
    social_score_ceiling: float = 0.85
    fallback_base_score: float = 0.25


# Singletons (import these from feature code)
QUALITY_CONFIG = QualityConfig()
BLUR_CONFIG = BlurConfig()
CONFIDENCE_SCALE_CONFIG = ConfidenceScaleConfig()
SALIENCE_CONFIG = SalienceConfig()
CONTINUITY_CONFIG = ContinuityConfig()
IDENTITY_CONFIG = IdentityConfig()
SCENE_SUMMARY_CONFIG = SceneSummaryConfig()
INTERPRETATION_CONFIG = InterpretationConfig()
MEMORY_EVENT_CONFIG = MemoryEventConfig()
MEMORY_SCORING_CONFIG = MemoryScoringConfig()
PATTERN_CONFIG = PatternConfig()
PROACTIVE_CONFIG = ProactiveConfig()
SELFTEST_CONFIG = SelfTestConfig()
WORKBENCH_CONFIG = WorkbenchConfig()
REFLECTION_CONFIG = ReflectionConfig()
CONTEMPLATION_CONFIG = ContemplationConfig()
MODEL_ROUTING_CONFIG = ModelRoutingConfig()


def summarize_tuning_config() -> dict[str, Any]:
    """Compact snapshot for debugging (no secrets); safe to log occasionally."""

    def _serialize(obj: Any) -> Any:
        if is_dataclass(obj):
            return asdict(obj)
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_serialize(x) for x in obj]
        if isinstance(obj, frozenset):
            return sorted(obj)
        return obj

    out: dict[str, Any] = {
        "quality": _serialize(QUALITY_CONFIG),
        "blur": _serialize(BLUR_CONFIG),
        "confidence_scale": _serialize(CONFIDENCE_SCALE_CONFIG),
        "salience": _serialize(SALIENCE_CONFIG),
        "continuity": _serialize(CONTINUITY_CONFIG),
        "identity": _serialize(IDENTITY_CONFIG),
        "scene_summary": _serialize(SCENE_SUMMARY_CONFIG),
        "interpretation": _serialize(INTERPRETATION_CONFIG),
        "interpretation_event_priority": dict(INTERPRETATION_EVENT_PRIORITY_ITEMS),
        "memory_event_types": sorted(MEMORY_EVENT_TYPES),
        "memory_event": _serialize(MEMORY_EVENT_CONFIG),
        "memory_scoring_base_weights": dict(MEMORY_SCORING_BASE_WEIGHTS_ITEMS),
        "memory_scoring_classes": dict(MEMORY_SCORING_CLASS_ITEMS),
        "memory_scoring": _serialize(MEMORY_SCORING_CONFIG),
        "pattern": _serialize(PATTERN_CONFIG),
        "proactive": _serialize(PROACTIVE_CONFIG),
        "selftest": _serialize(SELFTEST_CONFIG),
        "workbench": _serialize(WORKBENCH_CONFIG),
        "reflection": _serialize(REFLECTION_CONFIG),
        "contemplation": _serialize(CONTEMPLATION_CONFIG),
        "model_routing": _serialize(MODEL_ROUTING_CONFIG),
        "model_capability_profiles": [asdict(p) for p in DEFAULT_MODEL_CAPABILITY_PROFILES],
    }
    return out
