"""
Phase 26 — bounded curiosity and exploratory intent (structured, recommendation-only).

Produces :class:`~brain.perception_types.CuriosityResult` from existing pipeline evidence.
Does **not** execute tools, mutate files, override approvals, or force user-visible questions.
"""
from __future__ import annotations

import traceback
from typing import Any, Optional

from .perception_types import (
    ContemplationResult,
    CuriosityQuestion,
    CuriosityResult,
    ExplorationSuggestion,
    IdentityResolutionResult,
    InterpretationLayerResult,
    MemoryRefinementResult,
    ModelRoutingResult,
    PatternLearningResult,
    ProactiveTriggerResult,
    ReflectionResult,
    SceneSummaryResult,
    SelfTestRunResult,
    SocialContinuityResult,
    WorkbenchProposalResult,
)


THEME_ANOMALY_PATTERN = "anomaly_in_pattern"
THEME_UNRESOLVED_IDENTITY = "unresolved_identity"
THEME_UNFINISHED_THREAD = "unfinished_thread"
THEME_WARNING_ROOT = "repeated_warning_root_cause"
THEME_UNCERTAINTY_GAP = "uncertainty_gap"
THEME_MODEL_GAP = "model_or_capability_gap"
THEME_SOCIAL_GAP = "social_context_gap"
THEME_NONE = "no_curiosity_needed"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _safe_default() -> CuriosityResult:
    return CuriosityResult(
        curiosity_triggered=False,
        curiosity_theme=THEME_NONE,
        curiosity_question="",
        curiosity_reason="no_structured_evidence_or_below_threshold",
        curiosity_confidence=0.18,
        exploration_mode="none",
        suggested_next_step="no_exploration_needed",
        internal_question=None,
        exploration_suggestions=[
            ExplorationSuggestion(kind="none", summary="No bounded curiosity action suggested.")
        ],
        should_observe=False,
        should_clarify=False,
        should_defer=False,
        boundedness_flags=[],
        notes=["Phase 26 curiosity layer — safe idle default."],
        meta={"phase": 26},
    )


def build_curiosity_result_safe(
    *,
    user_text: str,
    g: dict[str, Any] | None,
    pattern_learning: Optional[PatternLearningResult],
    memory_refinement: Optional[MemoryRefinementResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    social_continuity: Optional[SocialContinuityResult],
    proactive_trigger: Optional[ProactiveTriggerResult],
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    model_routing: Optional[ModelRoutingResult],
    identity_resolution: Optional[IdentityResolutionResult],
    interpretation_layer: Optional[InterpretationLayerResult],
    scene_summary: Optional[SceneSummaryResult],
) -> CuriosityResult:
    try:
        return _build_curiosity_result(
            _user_text=user_text,
            g=g if isinstance(g, dict) else {},
            pattern_learning=pattern_learning,
            memory_refinement=memory_refinement,
            reflection=reflection,
            contemplation=contemplation,
            social_continuity=social_continuity,
            proactive_trigger=proactive_trigger,
            selftests=selftests,
            workbench=workbench,
            model_routing=model_routing,
            identity_resolution=identity_resolution,
            interpretation_layer=interpretation_layer,
            scene_summary=scene_summary,
        )
    except Exception as e:
        print(f"[curiosity] safe_fallback err={e!r}\n{traceback.format_exc()}")
        r = _safe_default()
        r.boundedness_flags = list(r.boundedness_flags or []) + ["exception_safe_fallback"]
        r.meta["error"] = str(e)[:120]
        return r


def _build_curiosity_result(
    *,
    _user_text: str,
    g: dict[str, Any],
    pattern_learning: Optional[PatternLearningResult],
    memory_refinement: Optional[MemoryRefinementResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    social_continuity: Optional[SocialContinuityResult],
    proactive_trigger: Optional[ProactiveTriggerResult],
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    model_routing: Optional[ModelRoutingResult],
    identity_resolution: Optional[IdentityResolutionResult],
    interpretation_layer: Optional[InterpretationLayerResult],
    scene_summary: Optional[SceneSummaryResult],
) -> CuriosityResult:
    boundedness: list[str] = []

    voice_priority = bool(g.get("_voice_user_turn_priority"))
    if voice_priority:
        boundedness.append("voice_turn_soft_suppression")

    pl = pattern_learning or PatternLearningResult()
    ps = pl.primary_signal
    mr_opt = memory_refinement
    rf = reflection or ReflectionResult()
    ct = contemplation or ContemplationResult()
    soc = social_continuity or SocialContinuityResult()
    pt = proactive_trigger or ProactiveTriggerResult()
    st = selftests or SelfTestRunResult()
    wb = workbench or WorkbenchProposalResult()
    route = model_routing
    idr = identity_resolution or IdentityResolutionResult()
    il = interpretation_layer or InterpretationLayerResult()
    ss = scene_summary or SceneSummaryResult()

    quiet = float(getattr(soc, "quiet_preference_signal", 0.5) or 0.5)
    if quiet >= 0.72:
        boundedness.append("quiet_context_dampen_clarify")

    scores: dict[str, float] = {THEME_NONE: 0.05}
    reasons: dict[str, str] = {}

    # --- anomaly_in_pattern ---
    un = float(getattr(ps, "unusualness_score", 0.0) or 0.0)
    pstrength = float(getattr(ps, "pattern_strength", 0.0) or 0.0)
    if pl.skipped:
        pass
    elif un >= 0.52 and pstrength >= 0.28:
        scores[THEME_ANOMALY_PATTERN] = _clamp01(0.42 + 0.38 * un + 0.12 * pstrength)
        reasons[THEME_ANOMALY_PATTERN] = "pattern_unusual_vs_strength"

    # --- unresolved_identity ---
    if idr.identity_state == "unknown_face" and ss.face_presence in (
        "unknown_face",
        "multiple_faces",
        "single_face",
    ):
        boost = 0.55
        if "unknown_person_present" in (il.event_types or []) or il.primary_event in (
            "unknown_person_present",
            "likely_known_person_present",
        ):
            boost += 0.12
        pri = getattr(ps, "recurrence_score", 0.0) or 0.0
        boost += 0.08 * min(1.0, pri)
        scores[THEME_UNRESOLVED_IDENTITY] = _clamp01(boost)
        reasons[THEME_UNRESOLVED_IDENTITY] = "unknown_face_scene_signal"

    # --- unfinished_thread ---
    uthread = bool(getattr(soc, "unfinished_thread_present", False))
    ut_mr = False
    if mr_opt is not None:
        ut_mr = bool(getattr(getattr(mr_opt, "decision", None), "unfinished_thread_candidate", False))
    if uthread or ut_mr:
        scores[THEME_UNFINISHED_THREAD] = _clamp01(0.48 + 0.22 * float(uthread) + 0.18 * float(ut_mr))
        reasons[THEME_UNFINISHED_THREAD] = "social_or_memory_unfinished_thread"

    # --- repeated_warning_root_cause ---
    warns = list(getattr(getattr(st, "summary", None), "warning_checks", []) or [])
    if len(warns) >= 2:
        scores[THEME_WARNING_ROOT] = _clamp01(0.52 + 0.06 * min(len(warns), 4))
        reasons[THEME_WARNING_ROOT] = "multiple_selftest_warnings"
    elif len(warns) == 1 and getattr(st.summary, "overall_status", "") == "warning":
        scores[THEME_WARNING_ROOT] = 0.44
        reasons[THEME_WARNING_ROOT] = "single_persistent_warning_status"

    # --- uncertainty_gap ---
    rconf = float(getattr(rf, "confidence", 0.5) or 0.5)
    cconf = float(getattr(ct, "contemplation_confidence", 0.5) or 0.5)
    il_conf = float(getattr(il, "event_confidence", 0.5) or 0.5)
    uncertain_stack = (rconf < 0.46) + (cconf < 0.42) + (not il.no_meaningful_change and il_conf < 0.44)
    if uncertain_stack >= 2:
        scores[THEME_UNCERTAINTY_GAP] = _clamp01(0.45 + 0.08 * uncertain_stack)
        reasons[THEME_UNCERTAINTY_GAP] = "reflection_contemplation_interpretation_uncertainty"

    # --- model_or_capability_gap ---
    if route is not None:
        meta_r = dict(getattr(route, "meta", {}) or {})
        if meta_r.get("availability_clamp") or meta_r.get("availability_unknown"):
            scores[THEME_MODEL_GAP] = _clamp01(0.48 + (0.1 if meta_r.get("availability_unknown") else 0))
            reasons[THEME_MODEL_GAP] = "routing_fallback_or_unknown_availability"

    # --- social_context_gap ---
    fam = float(getattr(soc, "familiarity_score", 0.5) or 0.5)
    rconf_soc = float(getattr(soc, "confidence", 0.35) or 0.35)
    if fam >= 0.58 and rconf_soc < 0.34 and len(getattr(soc, "relationship_summary", "") or "") < 8:
        scores[THEME_SOCIAL_GAP] = 0.42
        reasons[THEME_SOCIAL_GAP] = "high_familiarity_thin_relationship_descriptor"

    # --- workbench recurrence (soft) ---
    if wb.has_proposal and len(getattr(wb, "proposals", []) or []) >= 2:
        scores[THEME_WARNING_ROOT] = max(scores.get(THEME_WARNING_ROOT, 0.0), 0.41)
        reasons[THEME_WARNING_ROOT] = reasons.get(THEME_WARNING_ROOT, "") + "|workbench_multi_proposals"

    # proactive observation bias (never forces speech)
    if getattr(pt, "suppression_reason", "") and float(getattr(pt, "trigger_score", 0.0) or 0.0) > 0.35:
        boundedness.append("proactive_suppressed_but_noted")

    # Voice dampening
    if voice_priority:
        for k in list(scores.keys()):
            if k != THEME_NONE:
                scores[k] = scores[k] * 0.58

    # Dedup recent signatures
    hist = g.get("_curiosity_sig_history")
    if not isinstance(hist, list):
        hist = []

    winner = THEME_NONE
    win_score = scores.get(THEME_NONE, 0.05)
    for k, v in scores.items():
        if k != THEME_NONE and v > win_score:
            winner = k
            win_score = v

    second = sorted((v for kk, v in scores.items() if kk != winner), reverse=True)
    margin = win_score - (second[0] if second else 0.0)

    sig_preview = f"{winner}|{reasons.get(winner, '')}"
    if sig_preview in hist[-8:]:
        boundedness.append("recent_duplicate_softened")
        win_score *= 0.62
        margin *= 0.85

    threshold = 0.47
    if voice_priority:
        threshold = 0.58
    if quiet >= 0.72:
        threshold += 0.05

    triggered = bool(winner != THEME_NONE and win_score >= threshold)

    cq_text, reason_full, explore_mode, next_step, suggs, observe, clarify, defer = _derive_outputs(
        theme=winner,
        triggered=triggered,
        win_score=win_score,
        reasons=reasons,
        voice_priority=voice_priority,
        quiet=quiet,
        idr=idr,
        soc=soc,
    )

    conf = _clamp01(win_score * 0.88 + 0.06 * margin)

    if triggered:
        new_hist = (hist + [sig_preview])[-10:]
        g["_curiosity_sig_history"] = new_hist

    iq = CuriosityQuestion(question_text=cq_text[:420], anchor_theme=winner) if cq_text else None

    notes = [
        "Bounded Phase 26 curiosity — descriptive only; does not authorize autonomous questioning or tools.",
    ]

    meta_out: dict[str, Any] = {
        "winner_score": round(win_score, 4),
        "margin": round(margin, 4),
        "threshold": threshold,
        "scores": {k: round(float(v), 4) for k, v in sorted(scores.items())},
        "reason_tags": dict(reasons),
    }

    res = CuriosityResult(
        curiosity_triggered=triggered,
        curiosity_theme=winner if triggered else THEME_NONE,
        curiosity_question=cq_text[:520],
        curiosity_reason=reason_full[:900],
        curiosity_confidence=conf if triggered else min(conf, 0.38),
        exploration_mode=explore_mode,
        suggested_next_step=next_step[:240],
        internal_question=iq,
        exploration_suggestions=suggs,
        should_observe=observe,
        should_clarify=clarify,
        should_defer=defer,
        boundedness_flags=boundedness,
        notes=notes,
        meta=meta_out,
    )

    q_preview = (cq_text[:96] + "…") if len(cq_text) > 96 else cq_text
    print(
        f"[curiosity] theme={res.curiosity_theme} conf={res.curiosity_confidence:.2f} "
        f"triggered={res.curiosity_triggered} question={q_preview!r}"
    )
    print(
        f"[curiosity] next={res.suggested_next_step[:120]!r} observe={observe} clarify={clarify} defer={defer} "
        f"mode={explore_mode}"
    )

    return res


def _derive_outputs(
    *,
    theme: str,
    triggered: bool,
    win_score: float,
    reasons: dict[str, str],
    voice_priority: bool,
    quiet: float,
    idr: IdentityResolutionResult,
    soc: SocialContinuityResult,
) -> tuple[str, str, str, str, list[ExplorationSuggestion], bool, bool, bool]:
    """Returns question, reason, exploration_mode, next_step, suggestions, observe, clarify, defer."""

    if not triggered or theme == THEME_NONE:
        return (
            "",
            "below_threshold_or_insufficient_evidence",
            "none",
            "no_exploration_needed",
            [ExplorationSuggestion(kind="none", summary="Stable — no bounded curiosity output.")],
            False,
            False,
            False,
        )

    reason_tag = reasons.get(theme, "evidence")

    templates: dict[str, tuple[str, str, str, list[ExplorationSuggestion]]] = {
        THEME_ANOMALY_PATTERN: (
            "Some recent signals look unusually shifted versus the usual baseline — worth observing whether this repeats.",
            "internal_pattern_anomaly_soft",
            "observe",
            [
                ExplorationSuggestion(kind="observe", summary="Continue passive observation before interpreting."),
                ExplorationSuggestion(kind="defer_followup", summary="If it repeats, revisit with clearer evidence."),
            ],
        ),
        THEME_UNRESOLVED_IDENTITY: (
            "An unknown face keeps appearing — if it becomes recurring, identification may matter later.",
            reason_tag,
            "observe",
            [
                ExplorationSuggestion(kind="observe", summary="Watch recurring unknown-face events without rushing labels."),
                ExplorationSuggestion(kind="clarify_later", summary="Optionally ask (later) whether naming matters."),
            ],
        ),
        THEME_UNFINISHED_THREAD: (
            "There seems to be an open conversational thread — it may deserve a gentle revisit when timing fits.",
            reason_tag,
            "defer_followup",
            [
                ExplorationSuggestion(kind="revisit_thread", summary="Remember to reconnect softly when appropriate."),
                ExplorationSuggestion(kind="defer_followup", summary="Defer if user pace suggests waiting."),
            ],
        ),
        THEME_WARNING_ROOT: (
            "Repeated warnings suggest checking whether a common cause might exist — observation first.",
            reason_tag,
            "observe",
            [
                ExplorationSuggestion(kind="inspect_warnings", summary="Review recurring warning checks when feasible."),
                ExplorationSuggestion(kind="observe", summary="Avoid jumping to fixes without confirmation."),
            ],
        ),
        THEME_UNCERTAINTY_GAP: (
            "Several internal reads disagree — holding light uncertainty may be safer than committing.",
            reason_tag,
            "observe",
            [
                ExplorationSuggestion(kind="observe", summary="Prefer observation until signals converge."),
                ExplorationSuggestion(kind="clarify_later", summary="Brief clarification only when socially appropriate."),
            ],
        ),
        THEME_MODEL_GAP: (
            "Model routing relied on fallback or unclear availability — worth a calm check later, not mid-turn.",
            reason_tag,
            "observe",
            [
                ExplorationSuggestion(kind="check_model_health", summary="When idle, verify Ollama tags / routing config."),
                ExplorationSuggestion(kind="defer_followup", summary="No user interruption required."),
            ],
        ),
        THEME_SOCIAL_GAP: (
            "Relationship context feels underspecified despite familiarity — proceed gently.",
            reason_tag,
            "observe",
            [
                ExplorationSuggestion(kind="observe", summary="Avoid assumptions; keep tone steady."),
                ExplorationSuggestion(kind="clarify_later", summary="Optional soft check-in only if rapport supports it."),
            ],
        ),
    }

    question, reason_key, base_mode, suggestions = templates.get(
        theme,
        (
            "Internal note: mild curiosity signal present — remain observational.",
            reason_tag,
            "observe",
            [ExplorationSuggestion(kind="observe", summary="Default observe path.")],
        ),
    )

    observe = base_mode in ("observe", "defer_followup") or theme in (
        THEME_ANOMALY_PATTERN,
        THEME_UNRESOLVED_IDENTITY,
        THEME_WARNING_ROOT,
        THEME_UNCERTAINTY_GAP,
        THEME_MODEL_GAP,
        THEME_SOCIAL_GAP,
    )
    defer = theme == THEME_UNFINISHED_THREAD or base_mode == "defer_followup"
    clarify = theme in (THEME_UNCERTAINTY_GAP, THEME_SOCIAL_GAP, THEME_UNRESOLVED_IDENTITY)

    if voice_priority:
        clarify = False
        observe = True
        base_mode = "observe"

    if quiet >= 0.74:
        clarify = False

    reason_full = f"{theme}|{reason_key}|score≈{win_score:.2f}"

    next_step = suggestions[0].summary if suggestions else "observe_softly"

    explore_mode_res = base_mode
    if clarify and not voice_priority:
        explore_mode_res = "clarify_when_suitable"

    return question, reason_full, explore_mode_res, next_step, suggestions, observe, clarify, defer


def apply_curiosity_to_perception_state(state: Any, bundle: Any) -> None:
    """Copy Phase 26 curiosity snapshot onto PerceptionState."""
    cq = getattr(bundle, "curiosity", None)
    if cq is None:
        state.curiosity_triggered = False
        state.curiosity_theme = THEME_NONE
        state.curiosity_question = ""
        state.curiosity_reason = ""
        state.curiosity_confidence = 0.0
        state.curiosity_suggested_next_step = "no_exploration_needed"
        state.curiosity_should_observe = False
        state.curiosity_should_clarify = False
        state.curiosity_should_defer = False
        state.curiosity_meta = {}
        return

    state.curiosity_triggered = bool(cq.curiosity_triggered)
    state.curiosity_theme = str(cq.curiosity_theme or THEME_NONE)
    state.curiosity_question = str(cq.curiosity_question or "")[:520]
    state.curiosity_reason = str(cq.curiosity_reason or "")[:900]
    state.curiosity_confidence = float(cq.curiosity_confidence)
    state.curiosity_suggested_next_step = str(cq.suggested_next_step or "")[:240]
    state.curiosity_should_observe = bool(cq.should_observe)
    state.curiosity_should_clarify = bool(cq.should_clarify)
    state.curiosity_should_defer = bool(cq.should_defer)
    state.curiosity_meta = {
        "exploration_mode": cq.exploration_mode,
        "reason_summary": (cq.curiosity_reason or "")[:600],
        "boundedness_flags": list(cq.boundedness_flags or []),
        "exploration_suggestions": [
            {"kind": s.kind, "summary": s.summary[:400]} for s in (cq.exploration_suggestions or [])[:12]
        ],
        **dict(cq.meta or {}),
    }
