"""
Phase 27 — Outcome learning and advisory behavior adjustment signals.

Produces :class:`~brain.perception_types.OutcomeLearningResult` from structured pipeline evidence.
**Does not** modify configs, approvals, routing tables, or persona — suggestions are descriptive only.
"""
from __future__ import annotations

import math
import traceback
from typing import Any, Optional

from .perception_types import (
    ContemplationResult,
    CuriosityResult,
    IdentityResolutionResult,
    InterpretationLayerResult,
    MemoryImportanceResult,
    MemoryRefinementResult,
    ModelRoutingResult,
    OutcomeLearningResult,
    OutcomeObservation,
    BehaviorAdjustmentSuggestion,
    PatternLearningResult,
    PerceptionMemoryOutput,
    ProactiveTriggerResult,
    QualityOutput,
    ReflectionResult,
    SceneSummaryResult,
    SelfTestRunResult,
    SocialContinuityResult,
    WorkbenchProposalResult,
)

# --- Soft categories (advisory; not forced runtime modes) ---
SUCCESSFUL_INTERACTION_PATTERN = "successful_interaction_pattern"
DEGRADED_INTERACTION_PATTERN = "degraded_interaction_pattern"
BLOCKED_ACTION_PATTERN = "blocked_action_pattern"
SUPPRESSED_TRIGGER_PATTERN = "suppressed_trigger_pattern"
SUCCESSFUL_SUPERVISED_REPAIR_PATTERN = "successful_supervised_repair_pattern"
NOISY_MEMORY_PATTERN = "noisy_memory_pattern"
PERCEPTION_UNCERTAINTY_PATTERN = "perception_uncertainty_pattern"
INTERRUPTION_TIMING_PATTERN = "interruption_timing_pattern"
CURIOSITY_VALUE_PATTERN = "curiosity_value_pattern"
NO_ADJUSTMENT_NEEDED = "no_adjustment_needed"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _safe_default() -> OutcomeLearningResult:
    return OutcomeLearningResult(
        outcome_category=NO_ADJUSTMENT_NEEDED,
        outcome_quality="neutral",
        repeated_outcome_pattern=False,
        suggested_adjustment="No outcome-based adjustment suggested this tick.",
        adjustment_confidence=0.15,
        adjustment_target="none",
        supporting_evidence=[],
        adjustment_suggestions=[
            BehaviorAdjustmentSuggestion(
                posture="keep",
                summary="Maintain current pacing and safety gates.",
                target_subsystem="general",
            )
        ],
        should_strengthen=False,
        should_weaken=False,
        should_keep=True,
        notes=["Phase 27 idle default — advisory only."],
        meta={"phase": 27},
    )


def _tally_inc(g: dict[str, Any], category: str, weight: float = 1.0) -> float:
    acc = g.setdefault("_outcome_learning_tally", {})
    if not isinstance(acc, dict):
        acc = {}
        g["_outcome_learning_tally"] = acc
    prev = float(acc.get(category, 0.0) or 0.0)
    nxt = prev + float(weight)
    acc[category] = nxt
    return nxt


def _workbench_exec_from_g(g: dict[str, Any]) -> Any:
    return g.get("_last_workbench_execution_result")


def _workbench_cmd_from_g(g: dict[str, Any]) -> Any:
    return g.get("_last_workbench_command_result")


def build_outcome_learning_result_safe(
    *,
    g: dict[str, Any] | None,
    quality: QualityOutput,
    perception_memory: Optional[PerceptionMemoryOutput],
    memory_importance: Optional[MemoryImportanceResult],
    pattern_learning: Optional[PatternLearningResult],
    proactive_trigger: Optional[ProactiveTriggerResult],
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    social_continuity: Optional[SocialContinuityResult],
    memory_refinement: Optional[MemoryRefinementResult],
    model_routing: Optional[ModelRoutingResult],
    curiosity: Optional[CuriosityResult],
    identity_resolution: Optional[IdentityResolutionResult],
    interpretation_layer: Optional[InterpretationLayerResult],
    scene_summary: Optional[SceneSummaryResult],
) -> OutcomeLearningResult:
    try:
        return _build_outcome_learning_result(
            g=g if isinstance(g, dict) else {},
            quality=quality,
            perception_memory=perception_memory,
            memory_importance=memory_importance,
            pattern_learning=pattern_learning,
            proactive_trigger=proactive_trigger,
            selftests=selftests,
            workbench=workbench,
            reflection=reflection,
            contemplation=contemplation,
            social_continuity=social_continuity,
            memory_refinement=memory_refinement,
            model_routing=model_routing,
            curiosity=curiosity,
            identity_resolution=identity_resolution,
            interpretation_layer=interpretation_layer,
            scene_summary=scene_summary,
        )
    except Exception as e:
        print(f"[outcome_learning] safe_fallback err={e!r}\n{traceback.format_exc()}")
        r = _safe_default()
        r.notes = list(r.notes or []) + [f"safe_fallback:{str(e)[:120]}"]
        r.meta["error"] = str(e)[:160]
        return r


def _build_outcome_learning_result(
    *,
    g: dict[str, Any],
    quality: QualityOutput,
    perception_memory: Optional[PerceptionMemoryOutput],
    memory_importance: Optional[MemoryImportanceResult],
    pattern_learning: Optional[PatternLearningResult],
    proactive_trigger: Optional[ProactiveTriggerResult],
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    social_continuity: Optional[SocialContinuityResult],
    memory_refinement: Optional[MemoryRefinementResult],
    model_routing: Optional[ModelRoutingResult],
    curiosity: Optional[CuriosityResult],
    identity_resolution: Optional[IdentityResolutionResult],
    interpretation_layer: Optional[InterpretationLayerResult],
    scene_summary: Optional[SceneSummaryResult],
) -> OutcomeLearningResult:
    observations: list[OutcomeObservation] = []
    scores: dict[str, float] = {NO_ADJUSTMENT_NEEDED: 0.08}

    pt = proactive_trigger or ProactiveTriggerResult()
    rf = reflection or ReflectionResult()
    ct = contemplation or ContemplationResult()
    soc = social_continuity or SocialContinuityResult()
    st = selftests or SelfTestRunResult()
    wb = workbench or WorkbenchProposalResult()
    pl = pattern_learning or PatternLearningResult()
    mi = memory_importance or MemoryImportanceResult()
    pm = perception_memory or PerceptionMemoryOutput()
    mr = memory_refinement or MemoryRefinementResult()
    cq = curiosity or CuriosityResult()
    idr = identity_resolution or IdentityResolutionResult()
    il = interpretation_layer or InterpretationLayerResult()
    ss = scene_summary or SceneSummaryResult()
    route = model_routing

    trusted = bool(getattr(quality, "visual_truth_trusted", True))

    # --- Proactive suppression ---
    sup = str(getattr(pt, "suppression_reason", "") or "").strip()
    if sup and not bool(getattr(pt, "should_trigger", False)):
        w = 0.52 + 0.08 * min(1.0, float(getattr(pt, "trigger_score", 0.0) or 0.0))
        scores[SUPPRESSED_TRIGGER_PATTERN] = scores.get(SUPPRESSED_TRIGGER_PATTERN, 0.0) + w
        observations.append(
            OutcomeObservation(source="proactive_trigger", signal_strength=w, detail=f"suppressed:{sup[:120]}")
        )
        _tally_inc(g, SUPPRESSED_TRIGGER_PATTERN, 1.0)

    # --- Workbench executed / commands ---
    er = _workbench_exec_from_g(g)
    cr = _workbench_cmd_from_g(g)
    if er is not None:
        blocked = bool(getattr(er, "blocked", False))
        ok = bool(getattr(er, "success", False)) and not blocked
        if blocked or (not ok and str(getattr(er, "error_message", "") or "").strip()):
            w = 0.62
            scores[BLOCKED_ACTION_PATTERN] = scores.get(BLOCKED_ACTION_PATTERN, 0.0) + w
            observations.append(
                OutcomeObservation(
                    source="workbench_execution",
                    signal_strength=w,
                    detail=f"blocked={blocked} success={getattr(er, 'success', False)}",
                )
            )
            _tally_inc(g, BLOCKED_ACTION_PATTERN, 1.0)
        elif ok and bool(getattr(er, "approved", False)):
            w = 0.55
            scores[SUCCESSFUL_SUPERVISED_REPAIR_PATTERN] = scores.get(
                SUCCESSFUL_SUPERVISED_REPAIR_PATTERN, 0.0
            ) + w
            observations.append(
                OutcomeObservation(source="workbench_execution", signal_strength=w, detail="approved_success")
            )
            _tally_inc(g, SUCCESSFUL_SUPERVISED_REPAIR_PATTERN, 1.0)

    if cr is not None:
        br = str(getattr(cr, "blocked_reason", "") or "").strip()
        if br or not bool(getattr(cr, "success", False)):
            w = 0.48
            scores[BLOCKED_ACTION_PATTERN] = scores.get(BLOCKED_ACTION_PATTERN, 0.0) + w * 0.85
            observations.append(
                OutcomeObservation(source="workbench_command", signal_strength=w, detail=br[:160] or "command_not_success")
            )
            _tally_inc(g, BLOCKED_ACTION_PATTERN, 0.7)

    # --- Self-tests degraded ---
    summ = getattr(st, "summary", None)
    overall = str(getattr(summ, "overall_status", "") or "")
    warns = list(getattr(summ, "warning_checks", []) or [])
    fails = list(getattr(summ, "failed_checks", []) or [])
    if overall == "failed" or fails:
        w = 0.58 + 0.06 * min(len(fails), 4)
        scores[DEGRADED_INTERACTION_PATTERN] = scores.get(DEGRADED_INTERACTION_PATTERN, 0.0) + w
        observations.append(
            OutcomeObservation(source="selftests", signal_strength=w, detail=f"failed={fails[:3]}")
        )
        _tally_inc(g, DEGRADED_INTERACTION_PATTERN, 1.0)
    elif len(warns) >= 3:
        w = 0.42
        scores[DEGRADED_INTERACTION_PATTERN] = scores.get(DEGRADED_INTERACTION_PATTERN, 0.0) + w
        observations.append(OutcomeObservation(source="selftests", signal_strength=w, detail="multi_warnings"))
        _tally_inc(g, DEGRADED_INTERACTION_PATTERN, 0.6)

    # --- Reflection / contemplation poor ---
    oq = str(getattr(rf, "outcome_quality", "") or "").lower()
    if oq == "poor" or float(getattr(rf, "confidence", 0.5) or 0.5) < 0.38:
        w = 0.5
        scores[DEGRADED_INTERACTION_PATTERN] = scores.get(DEGRADED_INTERACTION_PATTERN, 0.0) + w * 0.9
        observations.append(OutcomeObservation(source="reflection", signal_strength=w, detail=f"quality={oq}"))
        _tally_inc(g, DEGRADED_INTERACTION_PATTERN, 0.7)
    if float(getattr(ct, "contemplation_confidence", 0.5) or 0.5) < 0.36:
        w = 0.35
        scores[PERCEPTION_UNCERTAINTY_PATTERN] = scores.get(PERCEPTION_UNCERTAINTY_PATTERN, 0.0) + w
        observations.append(OutcomeObservation(source="contemplation", signal_strength=w, detail="low_confidence"))

    # --- Perception / interpretation uncertainty ---
    if not trusted:
        w = 0.55
        scores[PERCEPTION_UNCERTAINTY_PATTERN] = scores.get(PERCEPTION_UNCERTAINTY_PATTERN, 0.0) + w
        observations.append(OutcomeObservation(source="vision_trust", signal_strength=w, detail="untrusted_frame"))
        _tally_inc(g, PERCEPTION_UNCERTAINTY_PATTERN, 1.0)
    if float(getattr(ss, "summary_confidence", 0.5) or 0.5) < 0.4:
        scores[PERCEPTION_UNCERTAINTY_PATTERN] = scores.get(PERCEPTION_UNCERTAINTY_PATTERN, 0.0) + 0.35
        observations.append(OutcomeObservation(source="scene_summary", signal_strength=0.35, detail="low_scene_conf"))
    if float(getattr(il, "event_confidence", 0.5) or 0.5) < 0.38 and not il.no_meaningful_change:
        scores[PERCEPTION_UNCERTAINTY_PATTERN] = scores.get(PERCEPTION_UNCERTAINTY_PATTERN, 0.0) + 0.32

    if idr.identity_state == "unknown_face":
        scores[PERCEPTION_UNCERTAINTY_PATTERN] = scores.get(PERCEPTION_UNCERTAINTY_PATTERN, 0.0) + 0.18
        observations.append(
            OutcomeObservation(source="identity_resolution", signal_strength=0.18, detail="unknown_face_state")
        )

    # --- Memory noise ---
    dec = getattr(mi, "decision", None)
    if dec is not None and str(getattr(dec, "memory_class", "") or "") == "ignore":
        imp = float(getattr(dec, "importance_score", 0.0) or 0.0)
        if imp > 0.42 and not bool(getattr(dec, "memory_worthy", False)):
            w = 0.44
            scores[NOISY_MEMORY_PATTERN] = scores.get(NOISY_MEMORY_PATTERN, 0.0) + w
            observations.append(OutcomeObservation(source="memory_importance", signal_strength=w, detail="high_imp_but_ignore"))
            _tally_inc(g, NOISY_MEMORY_PATTERN, 0.8)

    mr_dec = getattr(mr, "decision", None)
    if mr_dec is not None and str(getattr(mr_dec, "suppression_reason", "") or "").strip():
        scores[NOISY_MEMORY_PATTERN] = scores.get(NOISY_MEMORY_PATTERN, 0.0) + 0.38
        observations.append(
            OutcomeObservation(
                source="memory_refinement",
                signal_strength=0.38,
                detail=str(getattr(mr_dec, "suppression_reason", ""))[:120],
            )
        )
        _tally_inc(g, NOISY_MEMORY_PATTERN, 0.6)

    if pm.skipped and str(getattr(pm, "skip_reason", "") or ""):
        scores[NOISY_MEMORY_PATTERN] = scores.get(NOISY_MEMORY_PATTERN, 0.0) + 0.22

    # --- Model routing strain ---
    if route is not None:
        rmeta = dict(getattr(route, "meta", {}) or {})
        if rmeta.get("availability_clamp") or rmeta.get("availability_unknown"):
            w = 0.4
            scores[PERCEPTION_UNCERTAINTY_PATTERN] = scores.get(PERCEPTION_UNCERTAINTY_PATTERN, 0.0) + w * 0.75
            observations.append(OutcomeObservation(source="model_routing", signal_strength=w, detail="fallback_or_unknown_tags"))
            _tally_inc(g, PERCEPTION_UNCERTAINTY_PATTERN, 0.55)

    # --- Curiosity value (repeated low/no value) ---
    cq_meta = dict(getattr(cq, "meta", {}) or {})
    if not cq.curiosity_triggered and float(cq_meta.get("winner_score", 0.0) or 0.0) > 0.35:
        # strong internal score but gated off — worth noting as calibration friction
        scores[CURIOSITY_VALUE_PATTERN] = scores.get(CURIOSITY_VALUE_PATTERN, 0.0) + 0.28
        observations.append(OutcomeObservation(source="curiosity", signal_strength=0.28, detail="high_internal_score_not_triggered"))
    if "recent_duplicate_softened" in (cq.boundedness_flags or []):
        scores[CURIOSITY_VALUE_PATTERN] = scores.get(CURIOSITY_VALUE_PATTERN, 0.0) + 0.34
        _tally_inc(g, CURIOSITY_VALUE_PATTERN, 0.9)

    # --- Voice interruption / timing ---
    vc = g.get("_voice_conversation")
    if vc is not None:
        tm = getattr(vc, "timing", None)
        if tm is not None and bool(getattr(tm, "should_interrupt", False)):
            w = 0.46
            scores[INTERRUPTION_TIMING_PATTERN] = scores.get(INTERRUPTION_TIMING_PATTERN, 0.0) + w
            observations.append(OutcomeObservation(source="voice_timing", signal_strength=w, detail="interrupt_pressure"))
            _tally_inc(g, INTERRUPTION_TIMING_PATTERN, 1.0)
        reason = str(getattr(vc, "interruption_reason", "") or "").strip()
        if reason:
            scores[INTERRUPTION_TIMING_PATTERN] = scores.get(INTERRUPTION_TIMING_PATTERN, 0.0) + 0.22
            _tally_inc(g, INTERRUPTION_TIMING_PATTERN, 0.45)

    # --- Successful interaction (conservative) ---
    if (
        trusted
        and overall == "ok"
        and not fails
        and len(warns) == 0
        and float(getattr(soc, "trust_signal", 0.5) or 0.5) >= 0.62
        and oq in ("", "good", "mixed")
    ):
        w = 0.35
        scores[SUCCESSFUL_INTERACTION_PATTERN] = scores.get(SUCCESSFUL_INTERACTION_PATTERN, 0.0) + w
        observations.append(OutcomeObservation(source="aggregate", signal_strength=w, detail="stable_ok_context"))
        _tally_inc(g, SUCCESSFUL_INTERACTION_PATTERN, 0.5)

    # Pattern unusualness reinforcing degradation
    ps = getattr(pl, "primary_signal", None)
    if ps is not None and float(getattr(ps, "unusualness_score", 0.0) or 0.0) >= 0.62:
        scores[DEGRADED_INTERACTION_PATTERN] = scores.get(DEGRADED_INTERACTION_PATTERN, 0.0) + 0.24

    # Pick winner
    winner = NO_ADJUSTMENT_NEEDED
    win_score = scores.get(NO_ADJUSTMENT_NEEDED, 0.0)
    for k, v in scores.items():
        if k != NO_ADJUSTMENT_NEEDED and v > win_score:
            winner = k
            win_score = v

    second = sorted((v for kk, v in scores.items() if kk != winner), reverse=True)
    margin = win_score - (second[0] if second else 0.0)

    tally_n = float(g.get("_outcome_learning_tally", {}).get(winner, 0.0) or 0.0) if isinstance(g.get("_outcome_learning_tally"), dict) else 0.0
    repeated = tally_n >= 2.8

    rep_boost = min(0.38, 0.11 * math.sqrt(max(0.0, tally_n)))
    mix_penalty = max(0.0, (max(scores.values()) - win_score + 0.05)) if len(scores) > 3 else 0.0
    unc_penalty = 0.08 if not trusted else 0.0

    conf = _clamp01(0.22 + 0.52 * win_score + rep_boost + 0.14 * margin - 0.12 * mix_penalty - unc_penalty)

    threshold = 0.42
    if winner == NO_ADJUSTMENT_NEEDED or win_score < threshold:
        res = _safe_default()
        res.supporting_evidence = observations[:24]
        res.meta.update({"scores": {k: round(float(v), 4) for k, v in scores.items()}, "margin": round(margin, 4)})
        print(f"[outcome_learning] category={res.outcome_category} conf={res.adjustment_confidence:.2f} adjustment=idle")
        print("[outcome_learning] strengthen=False weaken=False keep=True")
        return res

    sug, adj_text, target, strengthen, weaken, keep, qual = _derive_adjustment(winner, observations, tally_n)

    result = OutcomeLearningResult(
        outcome_category=winner,
        outcome_quality=qual,
        repeated_outcome_pattern=repeated,
        suggested_adjustment=adj_text[:900],
        adjustment_confidence=conf,
        adjustment_target=target[:240],
        supporting_evidence=observations[:28],
        adjustment_suggestions=sug,
        should_strengthen=strengthen,
        should_weaken=weaken,
        should_keep=keep,
        notes=[
            "Advisory Phase 27 signal — does not alter configs, approvals, or persona automatically.",
        ],
        meta={
            "scores": {k: round(float(v), 4) for k, v in scores.items()},
            "margin": round(margin, 4),
            "tally_hint": round(tally_n, 3),
            "winner_score": round(win_score, 4),
        },
    )

    print(
        f"[outcome_learning] category={winner} conf={conf:.2f} adjustment={adj_text[:140]!r}"
    )
    print(
        f"[outcome_learning] strengthen={strengthen} weaken={weaken} keep={keep} target={target[:80]!r}"
    )

    return result


def _derive_adjustment(
    winner: str,
    _observations: list[OutcomeObservation],
    tally_n: float,
) -> tuple[list[BehaviorAdjustmentSuggestion], str, str, bool, bool, bool, str]:
    strengthen = weaken = False
    keep = False
    qual = "mixed"

    suggestions: list[BehaviorAdjustmentSuggestion] = []
    adj = ""
    target = "general"

    if winner == SUPPRESSED_TRIGGER_PATTERN:
        qual = "degraded"
        strengthen = False
        weaken = True
        keep = False
        target = "proactive_initiative"
        adj = "When triggers keep getting suppressed in similar contexts, bias toward waiting longer before surfacing proactive speech."
        suggestions = [
            BehaviorAdjustmentSuggestion(
                posture="wait_longer_before_speaking",
                summary="Prefer observe/defer for proactive prompts until context stabilizes.",
                target_subsystem=target,
            )
        ]
    elif winner == BLOCKED_ACTION_PATTERN:
        qual = "degraded"
        weaken = True
        adj = "Blocked supervised actions suggest tightening preconditions before proposing similar repairs again."
        target = "workbench_execution"
        suggestions = [
            BehaviorAdjustmentSuggestion(
                posture="respect_approval_gates",
                summary="Do not assume execution paths are available — surface intent without forcing apply.",
                target_subsystem=target,
            )
        ]
    elif winner == SUCCESSFUL_SUPERVISED_REPAIR_PATTERN:
        qual = "good"
        strengthen = True
        keep = True
        adj = "Recent approved supervised repairs succeeded — narrow confidence in similar staged paths may be warranted later."
        target = "workbench_execution"
        suggestions = [
            BehaviorAdjustmentSuggestion(
                posture="narrow_confidence_in_similar_paths",
                summary="Treat successful staged repairs as evidence only for comparable proposals (human approval still required).",
                target_subsystem=target,
            )
        ]
    elif winner == NOISY_MEMORY_PATTERN:
        qual = "mixed"
        weaken = True
        adj = "Memory signals look noisy — favor stronger filtering before treating weak events as durable."
        target = "memory_scoring_refinement"
        suggestions = [
            BehaviorAdjustmentSuggestion(
                posture="suppress_low_value_memory_candidates",
                summary="Prefer deferring persistence when refinement suppression repeats.",
                target_subsystem=target,
            )
        ]
    elif winner == PERCEPTION_UNCERTAINTY_PATTERN:
        qual = "uncertain"
        weaken = True
        adj = "Uncertain vision or routing strain — bias toward observe/defer until perception stabilizes."
        target = "perception_and_routing"
        suggestions = [
            BehaviorAdjustmentSuggestion(
                posture="observe_and_defer",
                summary="Hold definitive claims; rely on observation until trust improves.",
                target_subsystem=target,
            )
        ]
    elif winner == INTERRUPTION_TIMING_PATTERN:
        qual = "mixed"
        weaken = True
        adj = "Frequent overlap/interrupt cues — consider shorter, faster responses in voice when safe."
        target = "voice_pacing"
        suggestions = [
            BehaviorAdjustmentSuggestion(
                posture="shorter_voice_replies_when_overlapping",
                summary="Reduce floor-holding when interruption pressure is high (advisory pacing only).",
                target_subsystem=target,
            )
        ]
    elif winner == CURIOSITY_VALUE_PATTERN:
        qual = "mixed"
        weaken = True
        adj = "Curiosity signals repeat without triggering — dampen that curiosity path slightly to avoid churn."
        target = "curiosity_layer"
        suggestions = [
            BehaviorAdjustmentSuggestion(
                posture="dampen_low_value_curiosity",
                summary="Prefer fewer internal prompts when duplicate-softening fires often.",
                target_subsystem=target,
            )
        ]
    elif winner == DEGRADED_INTERACTION_PATTERN:
        qual = "poor"
        weaken = True
        adj = "Health or reflection signals degraded — slow down initiative and favor observation."
        target = "general"
        suggestions = [
            BehaviorAdjustmentSuggestion(
                posture="reduce_initiative_bias",
                summary="Wait for clearer evidence before assertive moves.",
                target_subsystem=target,
            )
        ]
    elif winner == SUCCESSFUL_INTERACTION_PATTERN:
        qual = "good"
        strengthen = True
        keep = True
        adj = "Interaction context looks stable — keeping current pacing and tone is reasonable."
        target = "relationship_layer"
        suggestions = [
            BehaviorAdjustmentSuggestion(
                posture="maintain_current_behavior",
                summary="No forced change; preserve rapport-sensitive pacing.",
                target_subsystem=target,
            )
        ]
    else:
        keep = True
        adj = "No strong adjustment signal."
        suggestions = [
            BehaviorAdjustmentSuggestion(posture="keep", summary="Hold course.", target_subsystem="general")
        ]

    if tally_n < 1.4 and winner not in (SUCCESSFUL_SUPERVISED_REPAIR_PATTERN, BLOCKED_ACTION_PATTERN):
        strengthen = False

    return suggestions, adj, target, strengthen, weaken, keep, qual


def apply_outcome_learning_to_perception_state(state: Any, bundle: Any) -> None:
    """Copy Phase 27 outcome snapshot onto PerceptionState."""
    ol = getattr(bundle, "outcome_learning", None)
    if ol is None:
        state.outcome_learning_category = NO_ADJUSTMENT_NEEDED
        state.outcome_learning_quality = "neutral"
        state.suggested_behavior_adjustment = ""
        state.adjustment_confidence = 0.0
        state.adjustment_target = ""
        state.outcome_learning_meta = {}
        return

    state.outcome_learning_category = str(ol.outcome_category or NO_ADJUSTMENT_NEEDED)
    state.outcome_learning_quality = str(ol.outcome_quality or "neutral")
    state.suggested_behavior_adjustment = str(ol.suggested_adjustment or "")[:900]
    state.adjustment_confidence = float(ol.adjustment_confidence)
    state.adjustment_target = str(ol.adjustment_target or "")[:240]
    state.outcome_learning_meta = {
        "repeated_outcome_pattern": bool(ol.repeated_outcome_pattern),
        "should_strengthen": bool(ol.should_strengthen),
        "should_weaken": bool(ol.should_weaken),
        "should_keep": bool(ol.should_keep),
        "supporting_evidence": [
            {"source": o.source, "strength": round(o.signal_strength, 4), "detail": o.detail[:400]}
            for o in (ol.supporting_evidence or [])[:20]
        ],
        "adjustment_suggestions": [
            {"posture": s.posture, "summary": s.summary[:400], "target": s.target_subsystem}
            for s in (ol.adjustment_suggestions or [])[:12]
        ],
        **dict(ol.meta or {}),
    }
