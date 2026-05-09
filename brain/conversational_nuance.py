"""
Phase 28 — Human-style emotional and conversational nuance (bounded guidance only).

Emits :class:`~brain.perception_types.ConversationalNuanceResult` for future prompt / style hooks.
Does **not** rewrite user replies, configs, or safety — only soft, evidence-based guidance.
"""
from __future__ import annotations

import traceback
from typing import Any, Optional

from .perception_types import (
    ContemplationResult,
    ConversationalNuanceResult,
    NuanceSignal,
    OutcomeLearningResult,
    PatternLearningResult,
    ProactiveTriggerResult,
    QualityOutput,
    ReflectionResult,
    SceneSummaryResult,
    SocialContinuityResult,
    VoiceConversationResult,
    MemoryRefinementResult,
    ToneGuidanceProfile,
    InterpretationLayerResult,
    ModelRoutingResult,
    CuriosityResult,
)

# --- Soft tone labels (not hard voice / mask) ---
TONE_WARM_SUPPORTIVE = "warm_supportive"
TONE_PRACTICAL_DIRECT = "practical_direct"
TONE_QUIET_RESTRAINED = "quiet_restrained"
TONE_REFLECTIVE_DEEP = "reflective_deep"
TONE_STEADY_FAMILIAR = "steady_familiar"
TONE_LIGHT_PLAYFUL = "light_playful"
TONE_SERIOUS_CAREFUL = "serious_careful"
TONE_UNCERTAIN_NEUTRAL = "uncertain_neutral"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _default_result() -> ConversationalNuanceResult:
    tp = ToneGuidanceProfile(preferred_tone_category=TONE_UNCERTAIN_NEUTRAL)
    return ConversationalNuanceResult(
        nuance_tone=TONE_UNCERTAIN_NEUTRAL,
        warmth_level=0.52,
        practicality_level=0.48,
        softness_level=0.52,
        seriousness_level=0.46,
        humor_tolerance=0.34,
        verbosity_bias=0.52,
        pacing_bias=0.5,
        restraint_bias=0.46,
        emotional_pacing_hint="steady",
        nuance_summary="Neutral conversational stance — insufficient evidence for a stronger tone tilt.",
        confidence=0.32,
        signals=[],
        tone_profile=tp,
        notes=["Phase 28 idle default — guidance only."],
        meta={"phase": 28},
    )


def _prior_get(g: dict[str, Any], key: str, default: float) -> float:
    p = g.get("_nuance_prior_levels")
    if not isinstance(p, dict):
        return default
    try:
        return float(p.get(key, default))
    except (TypeError, ValueError):
        return default


def _prior_blend(g: dict[str, Any], key: str, new_val: float, *, alpha: float = 0.38) -> float:
    """Blend toward prior to avoid tone whiplash (alpha = weight on new evidence)."""
    prev = _prior_get(g, key, new_val)
    return _clamp01(alpha * new_val + (1.0 - alpha) * prev)


def build_conversational_nuance_safe(
    *,
    g: dict[str, Any] | None,
    quality: QualityOutput,
    interpretation_layer: Optional[InterpretationLayerResult],
    scene_summary: Optional[SceneSummaryResult],
    pattern_learning: Optional[PatternLearningResult],
    proactive_trigger: Optional[ProactiveTriggerResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    social_continuity: Optional[SocialContinuityResult],
    memory_refinement: Optional[MemoryRefinementResult],
    model_routing: Optional[ModelRoutingResult],
    curiosity: Optional[CuriosityResult],
    outcome_learning: Optional[OutcomeLearningResult],
) -> ConversationalNuanceResult:
    try:
        return _build_conversational_nuance(
            g=g if isinstance(g, dict) else {},
            quality=quality,
            interpretation_layer=interpretation_layer,
            scene_summary=scene_summary,
            pattern_learning=pattern_learning,
            proactive_trigger=proactive_trigger,
            reflection=reflection,
            contemplation=contemplation,
            social_continuity=social_continuity,
            memory_refinement=memory_refinement,
            model_routing=model_routing,
            curiosity=curiosity,
            outcome_learning=outcome_learning,
        )
    except Exception as e:
        print(f"[conversational_nuance] safe_fallback err={e!r}\n{traceback.format_exc()}")
        r = _default_result()
        r.notes = list(r.notes or []) + [f"safe_fallback:{str(e)[:120]}"]
        r.meta["error"] = str(e)[:160]
        return r


def _build_conversational_nuance(
    *,
    g: dict[str, Any],
    quality: QualityOutput,
    interpretation_layer: Optional[InterpretationLayerResult],
    scene_summary: Optional[SceneSummaryResult],
    pattern_learning: Optional[PatternLearningResult],
    proactive_trigger: Optional[ProactiveTriggerResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    social_continuity: Optional[SocialContinuityResult],
    memory_refinement: Optional[MemoryRefinementResult],
    model_routing: Optional[ModelRoutingResult],
    curiosity: Optional[CuriosityResult],
    outcome_learning: Optional[OutcomeLearningResult],
) -> ConversationalNuanceResult:
    sigs: list[NuanceSignal] = []
    scores: dict[str, float] = {TONE_UNCERTAIN_NEUTRAL: 0.12}

    trusted = bool(getattr(quality, "visual_truth_trusted", True))
    il = interpretation_layer or InterpretationLayerResult()
    ss = scene_summary or SceneSummaryResult()
    soc = social_continuity or SocialContinuityResult()
    rf = reflection or ReflectionResult()
    ct = contemplation or ContemplationResult()
    pt = proactive_trigger or ProactiveTriggerResult()
    pl = pattern_learning or PatternLearningResult()
    mr = memory_refinement or MemoryRefinementResult()
    cq = curiosity or CuriosityResult()
    ol = outcome_learning or OutcomeLearningResult()
    route = model_routing

    fam = float(getattr(soc, "familiarity_score", 0.5) or 0.5)
    trust = float(getattr(soc, "trust_signal", 0.5) or 0.5)
    warmth_p = float(getattr(soc, "warmth_preference_signal", 0.5) or 0.5)
    practical_p = float(getattr(soc, "practicality_preference_signal", 0.5) or 0.5)
    quiet_p = float(getattr(soc, "quiet_preference_signal", 0.5) or 0.5)
    depth_p = float(getattr(soc, "depth_preference_signal", 0.5) or 0.5)
    style_hint = str(getattr(soc, "interaction_style_hint", "") or "").lower()
    unfinished = bool(getattr(soc, "unfinished_thread_present", False))

    il_conf = float(getattr(il, "event_confidence", 0.5) or 0.5)
    ss_conf = float(getattr(ss, "summary_confidence", 0.5) or 0.5)
    ps = getattr(pl, "primary_signal", None)
    unusual = float(getattr(ps, "unusualness_score", 0.0) or 0.0) if ps is not None else 0.0

    vc: Optional[VoiceConversationResult] = None
    raw_vc = g.get("_voice_conversation")
    if isinstance(raw_vc, VoiceConversationResult):
        vc = raw_vc

    voice_wait = bool(vc is not None and getattr(vc.timing, "should_wait", False))
    voice_yield = bool(vc is not None and getattr(vc.timing, "should_yield", False))
    voice_interrupt = bool(vc is not None and getattr(vc.timing, "should_interrupt", False))
    voice_ready = float(getattr(vc.timing, "response_readiness", 0.55) or 0.55) if vc else 0.55

    # --- Category scores (conservative) ---
    if fam >= 0.56 and trust >= 0.48 and warmth_p >= 0.52:
        scores[TONE_WARM_SUPPORTIVE] = scores.get(TONE_WARM_SUPPORTIVE, 0.0) + 0.42 + 0.12 * fam
        sigs.append(NuanceSignal(source="social", weight=0.5, detail="familiarity_trust_warmth"))

    if practical_p >= 0.58 or bool(g.get("_last_workbench_execution_result")):
        scores[TONE_PRACTICAL_DIRECT] = scores.get(TONE_PRACTICAL_DIRECT, 0.0) + 0.38 + 0.05 * practical_p
        sigs.append(NuanceSignal(source="social_or_task", weight=0.45, detail="practicality_bias"))

    if quiet_p >= 0.62 or voice_wait or voice_interrupt or str(getattr(pt, "suppression_reason", "") or ""):
        scores[TONE_QUIET_RESTRAINED] = scores.get(TONE_QUIET_RESTRAINED, 0.0) + 0.48
        sigs.append(NuanceSignal(source="voice_or_proactive", weight=0.55, detail="quiet_or_suppressed"))

    theme = str(getattr(ct, "contemplation_theme", "") or "")
    if depth_p >= 0.56 or any(
        theme.startswith(p) for p in ("significance", "boundary", "ethics", "meaning")
    ):
        scores[TONE_REFLECTIVE_DEEP] = scores.get(TONE_REFLECTIVE_DEEP, 0.0) + 0.45 + 0.08 * depth_p
        sigs.append(NuanceSignal(source="contemplation", weight=0.48, detail=theme[:80]))

    if "steady" in style_hint or "familiar" in style_hint or style_hint == "steady_familiar_tone":
        scores[TONE_STEADY_FAMILIAR] = scores.get(TONE_STEADY_FAMILIAR, 0.0) + 0.36
        sigs.append(NuanceSignal(source="social_style", weight=0.4, detail=style_hint[:80]))

    oq = str(getattr(ol, "outcome_quality", "") or "").lower()
    humor_ok = (
        fam >= 0.58
        and trust >= 0.52
        and unusual < 0.48
        and float(getattr(rf, "confidence", 0.5) or 0.5) >= 0.42
        and oq not in ("poor", "degraded")
    )
    if humor_ok and quiet_p < 0.68 and not voice_interrupt:
        scores[TONE_LIGHT_PLAYFUL] = scores.get(TONE_LIGHT_PLAYFUL, 0.0) + 0.33 + 0.05 * trust
        sigs.append(NuanceSignal(source="aggregate", weight=0.35, detail="humor_gate_passed"))

    serious_stack = 0
    if not trusted:
        serious_stack += 1
    if il_conf < 0.42 or ss_conf < 0.42:
        serious_stack += 1
    ocat = str(getattr(ol, "outcome_category", "") or "")
    if ocat in ("degraded_interaction_pattern", "blocked_action_pattern"):
        serious_stack += 1
    if route is not None:
        rmeta = dict(getattr(route, "meta", {}) or {})
        if rmeta.get("availability_unknown") or rmeta.get("availability_clamp"):
            serious_stack += 1
    if serious_stack >= 2:
        scores[TONE_SERIOUS_CAREFUL] = scores.get(TONE_SERIOUS_CAREFUL, 0.0) + 0.52 + 0.06 * serious_stack
        sigs.append(NuanceSignal(source="uncertainty_stack", weight=0.55, detail=f"stack={serious_stack}"))

    if unfinished and fam >= 0.5:
        scores[TONE_WARM_SUPPORTIVE] = scores.get(TONE_WARM_SUPPORTIVE, 0.0) + 0.18

    mr_dec = getattr(mr, "decision", None)
    if mr_dec is not None and float(getattr(mr_dec, "social_relevance_score", 0.0) or 0.0) >= 0.48:
        scores[TONE_WARM_SUPPORTIVE] = scores.get(TONE_WARM_SUPPORTIVE, 0.0) + 0.12

    if bool(getattr(cq, "curiosity_triggered", False)) and str(
        getattr(cq, "curiosity_theme", "") or ""
    ) not in ("", "no_curiosity_needed"):
        scores[TONE_REFLECTIVE_DEEP] = scores.get(TONE_REFLECTIVE_DEEP, 0.0) + 0.1
        sigs.append(NuanceSignal(source="curiosity", weight=0.3, detail="curiosity_active"))

    # Mixed evidence lowers peak confidence later
    winner = TONE_UNCERTAIN_NEUTRAL
    win_v = scores.get(TONE_UNCERTAIN_NEUTRAL, 0.12)
    for k, v in scores.items():
        if v > win_v:
            winner = k
            win_v = v

    second = sorted((v for kk, v in scores.items() if kk != winner), reverse=True)
    margin = win_v - (second[0] if second else 0.0)
    mix_penalty = max(0.0, (sum(scores.values()) - win_v) / max(1.0, len(scores) - 1) - 0.25)

    conf = _clamp01(0.26 + 0.52 * win_v + 0.14 * margin - 0.18 * mix_penalty)

    # Map tone → scalar guidance (then blend with prior)
    warmth = practicality = softness = seriousness = 0.5
    humor_tol = 0.34
    verbosity = 0.52
    pacing = 0.5
    restraint = 0.46
    pacing_hint = "steady"

    if winner == TONE_WARM_SUPPORTIVE:
        warmth, softness, practicality = 0.72, 0.62, 0.44
        nuance_summary = "Lean slightly warmer and supportive — rapport cues are favorable."
    elif winner == TONE_PRACTICAL_DIRECT:
        practicality, verbosity, warmth = 0.74, 0.38, 0.42
        nuance_summary = "Lean concise and practical — task or clarity-focused context."
    elif winner == TONE_QUIET_RESTRAINED:
        restraint, pacing, verbosity = 0.76, 0.38, 0.42
        warmth = 0.44
        pacing_hint = "gentle_slow"
        nuance_summary = "Prefer quiet, low-intrusion pacing — yield or suppression cues present."
    elif winner == TONE_REFLECTIVE_DEEP:
        seriousness, pacing, softness = 0.62, 0.42, 0.58
        pacing_hint = "slow_reflective"
        nuance_summary = "Room for a deeper, reflective tone — depth/contemplation signals."
    elif winner == TONE_STEADY_FAMILIAR:
        warmth, practicality = 0.56, 0.52
        nuance_summary = "Steady familiar rhythm — avoid sharp shifts."
    elif winner == TONE_LIGHT_PLAYFUL:
        humor_tol, warmth, seriousness = 0.52, 0.58, 0.42
        nuance_summary = "Light humor may be acceptable — only with existing rapport signals."
    elif winner == TONE_SERIOUS_CAREFUL:
        seriousness, restraint, humor_tol = 0.74, 0.68, 0.22
        pacing_hint = "careful_slow"
        nuance_summary = "Mixed uncertainty — stay serious, careful, avoid over-committing emotion."
    else:
        nuance_summary = "Neutral stance — evidence mixed or weak for a strong tilt."

    if voice_interrupt or voice_yield:
        restraint = max(restraint, 0.68)
        pacing = min(pacing, 0.42)
        pacing_hint = "gentle_slow"
        humor_tol = min(humor_tol, 0.36)

    if voice_ready >= 0.72 and not voice_interrupt and winner not in (TONE_SERIOUS_CAREFUL, TONE_QUIET_RESTRAINED):
        pacing = max(pacing, 0.58)
        pacing_hint = "responsive_brisk"

    # Blend with prior levels for stability
    warmth = _prior_blend(g, "warmth_level", warmth)
    practicality = _prior_blend(g, "practicality_level", practicality)
    softness = _prior_blend(g, "softness_level", softness)
    seriousness = _prior_blend(g, "seriousness_level", seriousness)
    humor_tol = _prior_blend(g, "humor_tolerance", humor_tol, alpha=0.28)
    verbosity = _prior_blend(g, "verbosity_bias", verbosity)
    pacing = _prior_blend(g, "pacing_bias", pacing)
    restraint = _prior_blend(g, "restraint_bias", restraint)

    last_tone = str(g.get("_nuance_prior_tone") or "")
    if last_tone and last_tone != winner and margin < 0.14:
        winner = last_tone
        conf *= 0.88
        nuance_summary += " (prior tone held — weak margin)."

    g["_nuance_prior_tone"] = winner
    g["_nuance_prior_levels"] = {
        "warmth_level": warmth,
        "practicality_level": practicality,
        "softness_level": softness,
        "seriousness_level": seriousness,
        "humor_tolerance": humor_tol,
        "verbosity_bias": verbosity,
        "pacing_bias": pacing,
        "restraint_bias": restraint,
    }

    tp = ToneGuidanceProfile(
        preferred_tone_category=winner,
        warmth_bias=warmth,
        practicality_bias=practicality,
        softness_bias=softness,
        seriousness_bias=seriousness,
        humor_tolerance=humor_tol,
    )

    res = ConversationalNuanceResult(
        nuance_tone=winner,
        warmth_level=warmth,
        practicality_level=practicality,
        softness_level=softness,
        seriousness_level=seriousness,
        humor_tolerance=humor_tol,
        verbosity_bias=verbosity,
        pacing_bias=pacing,
        restraint_bias=restraint,
        emotional_pacing_hint=pacing_hint,
        nuance_summary=nuance_summary[:520],
        confidence=conf,
        signals=sigs[:28],
        tone_profile=tp,
        notes=[
            "Phase 28 guidance is descriptive — prompts or future layers may consume it; "
            "no automatic personality rewrite.",
        ],
        meta={
            "scores": {k: round(float(v), 4) for k, v in sorted(scores.items())},
            "margin": round(margin, 4),
            "mix_penalty": round(mix_penalty, 4),
            "voice_snapshot": {
                "wait": voice_wait,
                "yield": voice_yield,
                "interrupt": voice_interrupt,
                "readiness": round(voice_ready, 3),
            },
        },
    )

    print(
        f"[conversational_nuance] tone={winner} conf={conf:.2f} warmth={warmth:.2f} practical={practicality:.2f}"
    )
    print(
        f"[conversational_nuance] pacing={pacing_hint} pacing_bias={pacing:.2f} restraint={restraint:.2f} humor={humor_tol:.2f}"
    )

    return res


def apply_conversational_nuance_to_perception_state(state: Any, bundle: Any) -> None:
    """Phase 28 — map bundle conversational nuance onto flat PerceptionState."""
    cn = getattr(bundle, "conversational_nuance", None)
    if cn is None:
        state.nuance_tone = "uncertain_neutral"
        state.nuance_summary = (
            "Neutral conversational stance — insufficient evidence for a stronger tone tilt."
        )
        state.nuance_confidence = 0.32
        state.warmth_level = 0.52
        state.practicality_level = 0.48
        state.softness_level = 0.52
        state.seriousness_level = 0.46
        state.humor_tolerance = 0.34
        state.verbosity_bias = 0.52
        state.pacing_bias = 0.5
        state.restraint_bias = 0.46
        state.nuance_meta = {"phase": 28, "idle": True}
        return

    state.nuance_tone = str(cn.nuance_tone or "uncertain_neutral")
    state.nuance_summary = str(cn.nuance_summary or "")[:520]
    state.nuance_confidence = float(cn.confidence)
    state.warmth_level = float(cn.warmth_level)
    state.practicality_level = float(cn.practicality_level)
    state.softness_level = float(cn.softness_level)
    state.seriousness_level = float(cn.seriousness_level)
    state.humor_tolerance = float(cn.humor_tolerance)
    state.verbosity_bias = float(cn.verbosity_bias)
    state.pacing_bias = float(cn.pacing_bias)
    state.restraint_bias = float(cn.restraint_bias)
    state.nuance_meta = {
        "emotional_pacing_hint": cn.emotional_pacing_hint,
        "signals": [
            {"source": s.source, "weight": round(s.weight, 4), "detail": s.detail[:240]}
            for s in (cn.signals or [])[:16]
        ],
        "tone_profile": {
            "preferred_tone_category": cn.tone_profile.preferred_tone_category,
            "warmth_bias": cn.tone_profile.warmth_bias,
            "practicality_bias": cn.tone_profile.practicality_bias,
            "softness_bias": cn.tone_profile.softness_bias,
            "seriousness_bias": cn.tone_profile.seriousness_bias,
            "humor_tolerance": cn.tone_profile.humor_tolerance,
        },
        **dict(cn.meta or {}),
    }
