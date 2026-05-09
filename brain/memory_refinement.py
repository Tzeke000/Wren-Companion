"""
Phase 24 — long-term memory refinement (additive layer on Phase 12 scoring).

Produces :class:`~brain.perception_types.MemoryRefinementResult` with conservative
classification, retention/retrieval weights, link **suggestions**, and suppression reasons.

Does **not** write durable storage or replace :mod:`brain.memory_scoring` — it refines what
downstream hooks may use for future persistence and retrieval quality.
"""
from __future__ import annotations

import traceback
from typing import Any, Optional

from .perception_types import (
    ContemplationResult,
    IdentityResolutionResult,
    InterpretationLayerResult,
    MemoryImportanceResult,
    MemoryLinkSuggestion,
    MemoryRefinementResult,
    PatternLearningResult,
    PerceptionMemoryOutput,
    RefinedMemoryDecision,
    ReflectionResult,
    SocialContinuityResult,
)

_VALID_CLASSES = frozenset(
    {
        "ignore",
        "transient_context",
        "episodic_candidate",
        "pattern_candidate",
        "preference_candidate",
        "relationship_candidate",
        "unfinished_thread_candidate",
    }
)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _voice_interrupt_evidence(g: dict | None) -> tuple[bool, float]:
    if not g:
        return False, 0.0
    vc = g.get("_voice_conversation")
    if vc is None:
        return False, 0.0
    try:
        tm = getattr(vc, "timing", None)
        if tm is not None and bool(getattr(tm, "should_interrupt", False)):
            return True, 0.85
        if str(getattr(vc, "interruption_reason", "") or "").strip():
            return True, 0.55
    except Exception:
        pass
    return False, 0.0


def build_memory_refinement_result(
    *,
    user_text: str,
    g: dict | None,
    perception_memory: Optional[PerceptionMemoryOutput],
    memory_importance: Optional[MemoryImportanceResult],
    pattern_learning: Optional[PatternLearningResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    social_continuity: Optional[SocialContinuityResult],
    interpretation_layer: Optional[InterpretationLayerResult],
    identity_resolution: Optional[IdentityResolutionResult],
) -> MemoryRefinementResult:
    mi = memory_importance or MemoryImportanceResult()
    pm = perception_memory or PerceptionMemoryOutput(event=None, skipped=True, skip_reason="missing")
    pl = pattern_learning or PatternLearningResult()
    rf = reflection or ReflectionResult()
    ct = contemplation or ContemplationResult()
    soc = social_continuity or SocialContinuityResult()
    il = interpretation_layer or InterpretationLayerResult()

    dec = mi.decision
    base_worthy = bool(dec.memory_worthy)
    base_class = str(dec.memory_class or "ignore")
    imp = float(dec.importance_score or 0.0)

    evt = pm.event
    notes: list[str] = []
    suppression = ""

    link_targets: list[MemoryLinkSuggestion] = []

    # --- relevance components (bounded)
    social_rel = _clamp01(
        float(soc.confidence or 0.35) * 0.45
        + float(soc.familiarity_score or 0.5) * 0.35
        + float(soc.trust_signal or 0.5) * 0.20
    )
    nmc = bool(il.no_meaningful_change)
    episodic_rel = _clamp01(
        imp * (0.42 if nmc else 0.72)
        + (0.12 if str(getattr(ct, "contemplation_theme", "")).startswith("significance") else 0.0)
    )
    ps = pl.primary_signal
    pat_rel = _clamp01(
        float(getattr(ps, "pattern_strength", 0.0) or 0.0) * 0.55
        + float(getattr(ps, "familiarity_score", 0.0) or 0.0) * 0.45
    )

    interrupt, interrupt_w = _voice_interrupt_evidence(g)
    unfinished_soft = bool(soc.unfinished_thread_present) or interrupt

    # --- junk / weak suppression (conservative)
    if pm.skipped or evt is None:
        suppression = suppression or "no_perception_memory_event"
        base_worthy = False
        base_class = "ignore"
        notes.append("skipped_or_empty_event")
    if evt is not None and getattr(evt, "suppressed_duplicate", False) and imp < 0.34:
        suppression = suppression or "weak_duplicate_candidate"
        base_worthy = False
        notes.append("duplicate_suppress_weak_importance")
    if nmc and imp < 0.28:
        suppression = suppression or "stable_no_meaningful_change_weak"
        base_worthy = False
        notes.append("no_meaningful_change_low_importance")
    rc = str(rf.reflection_category or "")
    if rc == "uncertain_state_reflection" and float(rf.confidence or 0.0) < 0.48 and imp < 0.33:
        suppression = suppression or "uncertain_reflection_low_importance"
        base_worthy = False
        notes.append("reflection_uncertain_durable_suppressed")

    # --- refined class (starts from scored class, then Phase 24 nudges)
    refined_class = base_class if base_class in _VALID_CLASSES else "ignore"
    worthy = bool(base_worthy)

    uthread = False
    if unfinished_soft and imp > 0.24 and (social_rel > 0.38 or interrupt or imp > 0.38):
        uthread = True
        notes.append("unfinished_thread_signal")
        if refined_class in ("ignore", "transient_context") and imp > 0.30:
            refined_class = "unfinished_thread_candidate"
            worthy = worthy or (imp > 0.33)

    if uthread and refined_class == "unfinished_thread_candidate":
        worthy = worthy and imp > 0.26

    if (
        refined_class in ("ignore", "transient_context")
        and social_rel >= 0.52
        and imp >= 0.36
        and not nmc
    ):
        refined_class = "relationship_candidate"
        worthy = worthy or True
        notes.append("social_boost_relationship_candidate")

    if refined_class in ("ignore", "transient_context") and pat_rel >= 0.48 and imp >= 0.32:
        refined_class = "pattern_candidate"
        worthy = worthy or True
        notes.append("pattern_boost")

    if refined_class == "ignore" and episodic_rel >= 0.48 and imp >= 0.34 and not nmc:
        refined_class = "episodic_candidate"
        worthy = True
        notes.append("episodic_promotion_evidence")

    if refined_class == "relationship_candidate" and social_rel < 0.42 and imp < 0.40:
        refined_class = "transient_context"
        notes.append("relationship_guard_weak_evidence")
        if imp < 0.32:
            worthy = False

    worthy = worthy and refined_class != "ignore"

    # --- retention / retrieval (weighted, capped — avoid runaway persistence signals)
    retention = _clamp01(
        0.12
        + 0.42 * imp
        + 0.18 * social_rel
        + 0.18 * episodic_rel
        + 0.14 * pat_rel
        + (0.06 if uthread else 0.0)
        + (0.05 if interrupt else 0.0)
        - (0.14 if suppression and worthy is False else 0.0)
    )
    retrieval = _clamp01(
        0.10
        + 0.38 * imp
        + 0.22 * episodic_rel
        + 0.18 * social_rel
        + 0.14 * pat_rel
        + (0.08 if uthread else 0.0)
        + (0.06 * float(rf.confidence or 0.0)) * 0.15
    )

    if suppression and not worthy:
        retention *= 0.55
        retrieval *= 0.55

    # --- link suggestions (soft hints only)
    pid = None
    if identity_resolution is not None:
        pid = identity_resolution.resolved_identity or identity_resolution.stable_identity
        if pid:
            link_targets.append(
                MemoryLinkSuggestion(
                    link_kind="related_identity_person",
                    target_hint=str(pid)[:120],
                    strength=_clamp01(0.55 + 0.25 * float(social_rel)),
                    meta={"identity_state": identity_resolution.identity_state},
                )
            )

    prim = str(il.primary_event or "")[:160]
    if prim and prim != "uncertain_visual_state":
        link_targets.append(
            MemoryLinkSuggestion(
                link_kind="related_recent_topic",
                target_hint=prim,
                strength=_clamp01(0.35 + 0.4 * imp),
                meta={"interpretation": True},
            )
        )

    if soc.recurring_topics:
        link_targets.append(
            MemoryLinkSuggestion(
                link_kind="related_recurring_topic",
                target_hint=str(soc.recurring_topics[0])[:120],
                strength=0.42,
                meta={"topics": soc.recurring_topics[:5]},
            )
        )

    if uthread:
        link_targets.append(
            MemoryLinkSuggestion(
                link_kind="related_unfinished_thread",
                target_hint="profile_or_voice_open_thread",
                strength=_clamp01(0.40 + 0.35 * interrupt_w),
                meta={"voice_interrupt": interrupt},
            )
        )

    if ps.pattern_type:
        link_targets.append(
            MemoryLinkSuggestion(
                link_kind="related_pattern",
                target_hint=str(ps.pattern_type)[:120],
                strength=_clamp01(pat_rel),
                meta={"unusualness": ps.unusualness_score},
            )
        )

    link_targets.append(
        MemoryLinkSuggestion(
            link_kind="related_reflection_category",
            target_hint=rc or "unknown",
            strength=_clamp01(0.28 + 0.35 * float(rf.confidence or 0.0)),
            meta={},
        )
    )

    rdec = RefinedMemoryDecision(
        refined_memory_worthy=worthy,
        refined_memory_class=refined_class,
        retention_strength=retention,
        retrieval_priority=retrieval,
        unfinished_thread_candidate=uthread,
        social_relevance_score=social_rel,
        episodic_relevance_score=episodic_rel,
        pattern_relevance_score=pat_rel,
        suppression_reason=suppression,
    )

    meta = {
        "base_memory_class": str(dec.memory_class or ""),
        "base_memory_worthy": bool(dec.memory_worthy),
        "interpretation_no_meaningful_change": nmc,
        "reflection_category": rc,
        "phase": "24",
    }

    result = MemoryRefinementResult(
        decision=rdec,
        link_targets=link_targets,
        notes=notes + ["memory_refinement_additive_only"],
        meta=meta,
    )

    print(
        f"[memory_refinement] class={rdec.refined_memory_class} worthy={rdec.refined_memory_worthy} "
        f"retention={rdec.retention_strength:.2f} retrieval={rdec.retrieval_priority:.2f}"
    )
    print(
        f"[memory_refinement] links={len(link_targets)} unfinished={uthread} "
        f"suppression={suppression or 'none'}"
    )
    return result


def safe_memory_refinement_fallback(reason: str) -> MemoryRefinementResult:
    """Neutral refinement when upstream fails."""
    return MemoryRefinementResult(
        decision=RefinedMemoryDecision(
            refined_memory_worthy=False,
            refined_memory_class="ignore",
            retention_strength=0.15,
            retrieval_priority=0.12,
            unfinished_thread_candidate=False,
            social_relevance_score=0.35,
            episodic_relevance_score=0.25,
            pattern_relevance_score=0.25,
            suppression_reason=reason,
        ),
        link_targets=[],
        notes=["fallback"],
        meta={"error": reason},
    )


def build_memory_refinement_result_safe(**kwargs: Any) -> MemoryRefinementResult:
    """Wrapper that never raises."""
    try:
        return build_memory_refinement_result(**kwargs)
    except Exception as e:
        print(f"[memory_refinement] failed: {e}\n{traceback.format_exc()}")
        return safe_memory_refinement_fallback(str(e))


def apply_memory_refinement_to_perception_state(state: Any, bundle: Any) -> None:
    mr = getattr(bundle, "memory_refinement", None)
    if mr is None or not isinstance(mr, MemoryRefinementResult):
        _defaults_refined_memory(state)
        return
    d = mr.decision
    state.refined_memory_class = str(d.refined_memory_class or "ignore")[:64]
    state.refined_memory_worthy = bool(d.refined_memory_worthy)
    state.refined_memory_retention_strength = float(d.retention_strength)
    state.refined_memory_retrieval_priority = float(d.retrieval_priority)
    state.refined_memory_unfinished_thread_candidate = bool(d.unfinished_thread_candidate)
    state.refined_memory_social_relevance = float(d.social_relevance_score)
    state.refined_memory_episodic_relevance = float(d.episodic_relevance_score)
    state.refined_memory_pattern_relevance = float(d.pattern_relevance_score)
    state.refined_memory_meta = {
        "suppression_reason": str(d.suppression_reason or ""),
        "link_targets": [
            {"kind": lt.link_kind, "hint": lt.target_hint[:200], "strength": lt.strength}
            for lt in (mr.link_targets or [])[:14]
        ],
        "notes": list(mr.notes or [])[:16],
        "meta": dict(mr.meta or {}),
    }


def _defaults_refined_memory(state: Any) -> None:
    state.refined_memory_class = "ignore"
    state.refined_memory_worthy = False
    state.refined_memory_retention_strength = 0.2
    state.refined_memory_retrieval_priority = 0.15
    state.refined_memory_unfinished_thread_candidate = False
    state.refined_memory_social_relevance = 0.35
    state.refined_memory_episodic_relevance = 0.25
    state.refined_memory_pattern_relevance = 0.25
    state.refined_memory_meta = {}
