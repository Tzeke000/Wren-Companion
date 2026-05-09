"""
Phase 23 — bounded social continuity and soft relationship modeling.

Produces :class:`~brain.perception_types.SocialContinuityResult` from structured pipeline
evidence only (no authoritative trait labels, no sensitive inferences).

Integration: invoked from :mod:`brain.perception_pipeline` **after** contemplation.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .perception_types import (
    ContemplationResult,
    IdentityResolutionResult,
    InterpretationLayerResult,
    InteractionStyleProfile,
    MemoryImportanceResult,
    PatternLearningResult,
    PerceptionMemoryOutput,
    ProactiveTriggerResult,
    ReflectionResult,
    RelationshipSignal,
    SceneSummaryResult,
    SocialContinuityResult,
)
from .profile_store import load_profile

_TOPIC_ALPHANUM = re.compile(r"[^a-z0-9]+")

# Soft rolling topic recurrence (bounded; not persisted across process restarts)
_topic_counts: dict[str, dict[str, int]] = {}

def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _person_id_from_g(g: dict | None) -> str:
    if not g:
        return "unknown"
    fn = g.get("get_active_person_id")
    try:
        if callable(fn):
            pid = fn()
            return str(pid or "unknown").strip() or "unknown"
    except Exception:
        pass
    return "unknown"


def _topic_slug(text: str) -> str:
    t = (text or "").strip().lower()[:56]
    if not t:
        return ""
    s = _TOPIC_ALPHANUM.sub("_", t).strip("_")
    return s[:48] or ""


def _bump_recurring_topic(person_id: str, key: str) -> None:
    if not key:
        return
    d = _topic_counts.setdefault(person_id, {})
    d[key] = int(d.get(key, 0)) + 1


def _top_recurring_topics(person_id: str, limit: int = 5) -> list[str]:
    d = _topic_counts.get(person_id) or {}
    if not d:
        return []
    ranked = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:limit]]


def _voice_hint_from_g(g: dict | None) -> tuple[float, float, list[str]]:
    """Returns (quiet_bias 0..1 addition, depth_bias, evidence strings)."""
    ev: list[str] = []
    if not g:
        return 0.0, 0.0, ev
    vc = g.get("_voice_conversation")
    if vc is None:
        return 0.0, 0.0, ev
    q = 0.0
    try:
        tm = getattr(vc, "timing", None)
        if tm is not None:
            if bool(getattr(tm, "should_wait", False)):
                q += 0.14
                ev.append("voice_bias_wait")
            rd = float(getattr(tm, "response_readiness", 0.55) or 0.55)
            if rd < 0.42:
                q += 0.08
                ev.append("voice_low_readiness")
        if str(getattr(vc, "interruption_reason", "") or "").strip():
            q += 0.06
            ev.append("voice_overlap_hint")
    except Exception:
        pass
    return _clamp01(q), 0.0, ev


def build_social_continuity_result(
    *,
    user_text: str,
    g: dict | None,
    perception_memory: Optional[PerceptionMemoryOutput],
    memory_importance: Optional[MemoryImportanceResult],
    pattern_learning: Optional[PatternLearningResult],
    proactive_trigger: Optional[ProactiveTriggerResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    interpretation_layer: Optional[InterpretationLayerResult],
    scene_summary: Optional[SceneSummaryResult],
    identity_resolution: Optional[IdentityResolutionResult],
) -> SocialContinuityResult:
    ut = (user_text or "").strip()
    pid = _person_id_from_g(g)

    mi = memory_importance or MemoryImportanceResult()
    pl = pattern_learning or PatternLearningResult()
    pt = proactive_trigger or ProactiveTriggerResult()
    rf = reflection or ReflectionResult()
    ct = contemplation or ContemplationResult()
    il = interpretation_layer or InterpretationLayerResult()
    pm = perception_memory or PerceptionMemoryOutput(event=None, skipped=True, skip_reason="")

    prim_event = str(il.primary_event or "") or "uncertain_visual_state"
    _bump_recurring_topic(pid, _topic_slug(prim_event))

    pf = load_profile(pid) if pid not in ("unknown", "") else None
    rel_score = 0.45
    interaction_ct = 0
    unresolved_threads = 0
    if isinstance(pf, dict):
        try:
            rel_score = float(pf.get("relationship_score", rel_score) or rel_score)
        except (TypeError, ValueError):
            rel_score = 0.45
        try:
            interaction_ct = int(pf.get("interaction_count", 0) or 0)
        except (TypeError, ValueError):
            interaction_ct = 0
        threads = pf.get("threads")
        if isinstance(threads, list):
            for t in threads:
                if isinstance(t, dict) and not t.get("resolved", True):
                    unresolved_threads += 1

    ps = pl.primary_signal
    fam_pat = float(getattr(ps, "familiarity_score", 0.0) or 0.0)
    unusual = float(getattr(ps, "unusualness_score", 0.0) or 0.0)
    imp = float(mi.decision.importance_score or 0.0)

    id_state = str(getattr(identity_resolution, "identity_state", "") if identity_resolution else "") or ""

    familiarity = _clamp01(
        0.22 + 0.38 * fam_pat + 0.22 * _clamp01(rel_score) + 0.12 * _clamp01(imp) + (0.06 if id_state == "confirmed_recognition" else 0.0)
    )

    trust = _clamp01(0.35 * _clamp01(rel_score) + 0.25 * float(rf.confidence or 0.35) + 0.18 * fam_pat + 0.15 * (1.0 - unusual * 0.35))

    style = InteractionStyleProfile()
    notes: list[str] = []
    signals: list[RelationshipSignal] = []

    theme = str(ct.contemplation_theme or "")
    if theme == "maintenance_vs_growth":
        style.practicality_preference_signal = _clamp01(style.practicality_preference_signal + 0.18)
        signals.append(RelationshipSignal("practical_interaction_pattern", 0.52, ["contemplation_theme"]))
    elif theme == "continuity_of_self":
        style.warmth_preference_signal = _clamp01(style.warmth_preference_signal + 0.12)
        signals.append(RelationshipSignal("familiar_interaction_pattern", 0.48, ["contemplation_theme"]))
    elif theme == "observation_vs_intervention":
        style.quiet_preference_signal = _clamp01(style.quiet_preference_signal + 0.16)
        signals.append(RelationshipSignal("quiet_or_low_intrusion_pattern", 0.5, ["contemplation_theme"]))
    elif theme == "significance_of_events":
        style.depth_preference_signal = _clamp01(style.depth_preference_signal + 0.14)
        signals.append(RelationshipSignal("deeper_reflective_pattern", 0.46, ["contemplation_theme"]))
    elif theme == "relationship_to_user_context":
        style.warmth_preference_signal = _clamp01(style.warmth_preference_signal + 0.15)
        signals.append(RelationshipSignal("warm_interaction_pattern", 0.44, ["contemplation_theme"]))
    elif theme == "consistency_of_behavior":
        style.practicality_preference_signal = _clamp01(style.practicality_preference_signal + 0.08)
        notes.append("consistency_guardrail_bias")

    rc = str(rf.reflection_category or "")
    if rc == "uncertain_state_reflection":
        signals.append(RelationshipSignal("uncertain_social_state", 0.42, ["reflection_category"]))
        style.quiet_preference_signal = _clamp01(style.quiet_preference_signal + 0.07)
    elif rc in ("failed_operation_reflection", "degraded_operation_reflection", "repeated_warning_reflection"):
        style.practicality_preference_signal = _clamp01(style.practicality_preference_signal + 0.1)

    if pt.suppression_reason and not pt.should_trigger:
        style.quiet_preference_signal = _clamp01(style.quiet_preference_signal + 0.05)
        notes.append("proactive_suppressed_recent")

    vq, _, v_ev = _voice_hint_from_g(g)
    style.quiet_preference_signal = _clamp01(style.quiet_preference_signal + vq)
    notes.extend(v_ev)

    unfinished = False
    if unresolved_threads > 0:
        unfinished = True
        signals.append(
            RelationshipSignal(
                "unfinished_conversation_thread",
                _clamp01(0.35 + 0.08 * min(unresolved_threads, 5)),
                ["profile_unresolved_threads"],
            )
        )
    vc = g.get("_voice_conversation") if g else None
    try:
        if vc is not None and bool(getattr(getattr(vc, "timing", None), "should_interrupt", False)):
            unfinished = True
            signals.append(RelationshipSignal("unfinished_conversation_thread", 0.45, ["voice_interrupt"]))
    except Exception:
        pass
    if pm.event is not None and getattr(pm.event, "suppressed_duplicate", False) and imp > 0.35:
        notes.append("duplicate_memory_event_high_importance")

    recurring = _top_recurring_topics(pid, 5)

    tone = "neutral"
    if style.warmth_preference_signal >= 0.58:
        tone = "warm"
    elif style.practicality_preference_signal >= 0.58:
        tone = "practical"
    elif style.quiet_preference_signal >= 0.58:
        tone = "quiet"
    elif style.depth_preference_signal >= 0.56:
        tone = "reflective"

    scores = {
        "gentle_warmth": style.warmth_preference_signal,
        "practical_direct": style.practicality_preference_signal,
        "quiet_low_intrusion": style.quiet_preference_signal,
        "deeper_reflective": style.depth_preference_signal,
        "steady_familiar_tone": float(familiarity),
    }
    hint = max(scores.items(), key=lambda kv: kv[1])[0]

    evidence_n = len(notes) + len(signals) + (1 if ut else 0) + min(interaction_ct, 12) // 4
    conf = _clamp01(0.32 + 0.06 * min(evidence_n, 8))

    summary = (
        f"Soft social read: familiarity≈{familiarity:.2f}, trust_signal≈{trust:.2f}; "
        f"recent tone tends **{tone}** (probabilistic, single-tick evidence)."
    )

    meta = {
        "person_id": pid,
        "primary_event": prim_event,
        "reflection_category": rc,
        "contemplation_theme": theme,
        "unresolved_thread_count": unresolved_threads,
        "profile_interaction_count": interaction_ct,
        "pattern_unusualness": unusual,
        "scene_overall_state": str(getattr(scene_summary, "overall_scene_state", "") or "")
        if scene_summary is not None
        else "",
    }

    result = SocialContinuityResult(
        familiarity_score=familiarity,
        trust_signal=trust,
        warmth_preference_signal=_clamp01(style.warmth_preference_signal),
        practicality_preference_signal=_clamp01(style.practicality_preference_signal),
        quiet_preference_signal=_clamp01(style.quiet_preference_signal),
        depth_preference_signal=_clamp01(style.depth_preference_signal),
        style_profile=style,
        unfinished_thread_present=unfinished,
        recurring_topics=list(recurring),
        recent_social_tone=tone,
        relationship_summary=summary,
        interaction_style_hint=hint,
        confidence=conf,
        signals=signals,
        notes=notes + ["phase_23_bounded_evidence_only"],
        meta=meta,
    )

    print(
        f"[relationship_model] familiarity={result.familiarity_score:.2f} tone={result.recent_social_tone} "
        f"hint={result.interaction_style_hint} conf={result.confidence:.2f}"
    )
    print(
        f"[relationship_model] unfinished_thread={result.unfinished_thread_present} "
        f"recurring_topics={result.recurring_topics[:5]}"
    )
    return result


def apply_social_continuity_to_perception_state(state: Any, bundle: Any) -> None:
    """Copy bundle.social_continuity onto PerceptionState with safe defaults."""
    sc = getattr(bundle, "social_continuity", None)
    if sc is None:
        _defaults_relationship_fields(state)
        return
    if not isinstance(sc, SocialContinuityResult):
        _defaults_relationship_fields(state)
        return

    state.relationship_familiarity_score = float(sc.familiarity_score)
    state.relationship_trust_signal = float(sc.trust_signal)
    state.relationship_summary = str(sc.relationship_summary or "")[:900]
    state.interaction_style_hint = str(sc.interaction_style_hint or "steady_familiar_tone")[:120]
    state.unfinished_thread_present = bool(sc.unfinished_thread_present)
    state.recurring_topics = list(sc.recurring_topics or [])[:16]
    state.recent_social_tone = str(sc.recent_social_tone or "neutral")[:48]
    state.relationship_confidence = float(sc.confidence)
    state.relationship_meta = {
        "signals": [{"name": s.name, "strength": s.strength} for s in (sc.signals or [])[:12]],
        "notes": list(sc.notes or [])[:16],
        "warmth": float(sc.warmth_preference_signal),
        "practicality": float(sc.practicality_preference_signal),
        "quiet": float(sc.quiet_preference_signal),
        "depth": float(sc.depth_preference_signal),
        "meta": dict(sc.meta or {}),
    }


def _defaults_relationship_fields(state: Any) -> None:
    state.relationship_familiarity_score = 0.5
    state.relationship_trust_signal = 0.5
    state.relationship_summary = ""
    state.interaction_style_hint = "steady_familiar_tone"
    state.unfinished_thread_present = False
    state.recurring_topics = []
    state.recent_social_tone = "neutral"
    state.relationship_confidence = 0.35
    state.relationship_meta = {}


# ── Phase 91: Relationship Memory Depth ───────────────────────────────────────
# Stores the texture of relationships — moments, references, trust, themes.

import json
import time
from pathlib import Path


def _rel_memory_path(base_dir: Path, person_id: str) -> Path:
    return base_dir / "profiles" / f"{person_id}_relationship.json"


def load_relationship_memory(base_dir: Path, person_id: str) -> dict[str, Any]:
    path = _rel_memory_path(base_dir, person_id)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "person_id": person_id,
        "first_meeting": "",
        "memorable_moments": [],
        "inside_references": [],
        "emotional_history": [],
        "trust_events": [],
        "conversation_themes": {},
        "growth_notes": [],
    }


def save_relationship_memory(base_dir: Path, person_id: str, mem: dict[str, Any]) -> None:
    path = _rel_memory_path(base_dir, person_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")


def record_memorable_moment(
    base_dir: Path, person_id: str, summary: str, emotional_context: str, memorability: float
) -> None:
    """Record a moment worth remembering about this relationship."""
    if memorability < 0.5:
        return
    mem = load_relationship_memory(base_dir, person_id)
    moments = list(mem.get("memorable_moments") or [])
    moments.append({
        "ts": time.time(),
        "summary": str(summary or "")[:300],
        "emotional_context": str(emotional_context or "")[:100],
        "memorability": round(memorability, 3),
    })
    # Keep top 20 by memorability
    moments = sorted(moments, key=lambda m: float(m.get("memorability") or 0), reverse=True)[:20]
    mem["memorable_moments"] = moments
    save_relationship_memory(base_dir, person_id, mem)


def record_emotion_with_person(base_dir: Path, person_id: str, emotion: str) -> None:
    """Track how Ava feels when in this person's presence."""
    mem = load_relationship_memory(base_dir, person_id)
    history = list(mem.get("emotional_history") or [])
    history.append({"ts": time.time(), "emotion": str(emotion or "")[:40]})
    mem["emotional_history"] = history[-50:]  # Keep last 50
    save_relationship_memory(base_dir, person_id, mem)


def record_conversation_theme(base_dir: Path, person_id: str, topic: str) -> None:
    """Track recurring topics with this person."""
    mem = load_relationship_memory(base_dir, person_id)
    themes = dict(mem.get("conversation_themes") or {})
    key = str(topic or "")[:60]
    themes[key] = int(themes.get(key) or 0) + 1
    mem["conversation_themes"] = themes
    save_relationship_memory(base_dir, person_id, mem)


def get_relationship_summary_for_prompt(base_dir: Path, person_id: str) -> str:
    """Return a brief summary for prompt injection."""
    mem = load_relationship_memory(base_dir, person_id)
    parts: list[str] = []
    moments = mem.get("memorable_moments") or []
    if moments:
        top = moments[0].get("summary") or ""
        parts.append(f"Memorable: {str(top)[:120]}")
    themes = mem.get("conversation_themes") or {}
    if themes:
        top_themes = sorted(themes.items(), key=lambda x: int(x[1]), reverse=True)[:3]
        parts.append("Recurring topics: " + ", ".join(t for t, _ in top_themes))
    growth = mem.get("growth_notes") or []
    if growth:
        parts.append(f"Growth: {str(growth[-1])[:100]}")
    if not parts:
        return ""
    return "RELATIONSHIP MEMORY: " + " | ".join(parts)
