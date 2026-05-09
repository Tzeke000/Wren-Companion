"""
Phase 31 — Bounded adaptive learning: evidence-weighted preference adjustments.

Persists soft scores under ``state/learning/`` — **does not** auto-edit ``ava_core`` files,
safety rules, workbench approvals, or global config. Outputs are advisory signals only.
"""
from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any, Optional

from .perception_types import (
    AdaptiveLearningResult,
    ContemplationResult,
    ConversationalNuanceResult,
    CuriosityResult,
    HeartbeatTickResult,
    LearningPreferenceShift,
    MemoryRefinementResult,
    ModelRoutingResult,
    OutcomeLearningResult,
    ReflectionResult,
    SocialContinuityResult,
)
from .shared import now_ts

_BASE = Path(__file__).resolve().parent.parent
LEARNING_DIR = _BASE / "state" / "learning"
PREFERENCES_PATH = LEARNING_DIR / "adaptive_preferences.json"


class LearningFocus:
    """Evidence-tagged focus areas (soft; all values kept in [0, 1] with 0.5 neutral)."""

    CONVERSATION_PACING = "conversation_pacing"
    INTERRUPTION_YIELD = "interruption_yield_habits"
    CURIOSITY_USEFULNESS = "curiosity_usefulness"
    MEMORY_USEFULNESS = "memory_usefulness"
    PROACTIVE_TRIGGER_USEFULNESS = "proactive_trigger_usefulness"
    REPAIR_PROPOSAL_USEFULNESS = "repair_proposal_usefulness"
    SOCIAL_CONTINUITY_USEFULNESS = "social_continuity_usefulness"
    PREFERRED_RESPONSE_STYLE = "preferred_response_style_tendencies"
    USER_COMFORT_SIGNAL = "user_comfort_annoy_vs_help"
    SILENCE_WHEN_BETTER = "silence_when_better_response"


ALL_FOCUS = (
    LearningFocus.CONVERSATION_PACING,
    LearningFocus.INTERRUPTION_YIELD,
    LearningFocus.CURIOSITY_USEFULNESS,
    LearningFocus.MEMORY_USEFULNESS,
    LearningFocus.PROACTIVE_TRIGGER_USEFULNESS,
    LearningFocus.REPAIR_PROPOSAL_USEFULNESS,
    LearningFocus.SOCIAL_CONTINUITY_USEFULNESS,
    LearningFocus.PREFERRED_RESPONSE_STYLE,
    LearningFocus.USER_COMFORT_SIGNAL,
    LearningFocus.SILENCE_WHEN_BETTER,
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _trunc(s: str, n: int = 200) -> str:
    t = " ".join((s or "").split())
    if len(t) <= n:
        return t
    return t[: n - 1].rstrip() + "…"


def _ensure_dir() -> None:
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)


def load_preferences() -> dict[str, dict[str, Any]]:
    _ensure_dir()
    if not PREFERENCES_PATH.is_file():
        return _default_preferences()
    try:
        with open(PREFERENCES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return _default_preferences()
        out = _default_preferences()
        weights = raw.get("weights")
        if isinstance(weights, dict):
            for k in ALL_FOCUS:
                if k in weights and isinstance(weights[k], (int, float)):
                    out["weights"][k] = _clamp01(float(weights[k]))
        ev = raw.get("evidence_counts")
        if isinstance(ev, dict):
            for k in ALL_FOCUS:
                if k in ev and isinstance(ev[k], (int, float)):
                    out["evidence_counts"][k] = max(0, int(ev[k]))
        for k in ALL_FOCUS:
            if k not in out["weights"]:
                out["weights"][k] = 0.5
            if k not in out["evidence_counts"]:
                out["evidence_counts"][k] = 0
        out["updated_at"] = float(raw.get("updated_at") or 0.0)
        return out
    except Exception:
        return _default_preferences()


def _default_preferences() -> dict[str, dict[str, Any]]:
    return {
        "weights": {k: 0.5 for k in ALL_FOCUS},
        "evidence_counts": {k: 0 for k in ALL_FOCUS},
        "updated_at": 0.0,
    }


def save_preferences(prefs: dict[str, dict[str, Any]]) -> None:
    _ensure_dir()
    try:
        payload = {
            "weights": {k: _clamp01(float(prefs["weights"].get(k, 0.5))) for k in ALL_FOCUS},
            "evidence_counts": {k: int(prefs["evidence_counts"].get(k, 0)) for k in ALL_FOCUS},
            "updated_at": float(now_ts()),
            "schema": "phase31_adaptive_v1",
            "respect_identity_anchors": True,
            "never_writes_ava_core": True,
        }
        tmp = PREFERENCES_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp.replace(PREFERENCES_PATH)
    except Exception as e:
        print(f"[adaptive_learning] save failed: {e}")


def _ewma(prev: float, target: float, alpha: float) -> float:
    return _clamp01(prev * (1.0 - alpha) + target * alpha)


def run_adaptive_learning_safe(
    *,
    g: dict[str, Any] | None,
    heartbeat: Optional[HeartbeatTickResult],
    outcome_learning: Optional[OutcomeLearningResult],
    curiosity: Optional[CuriosityResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    social_continuity: Optional[SocialContinuityResult],
    conversational_nuance: Optional[ConversationalNuanceResult],
    model_routing: Optional[ModelRoutingResult] = None,
    memory_refinement: Optional[MemoryRefinementResult] = None,
) -> AdaptiveLearningResult:
    try:
        return _run_adaptive_learning(
            g=g if isinstance(g, dict) else {},
            heartbeat=heartbeat,
            outcome_learning=outcome_learning,
            curiosity=curiosity,
            reflection=reflection,
            contemplation=contemplation,
            social_continuity=social_continuity,
            conversational_nuance=conversational_nuance,
            model_routing=model_routing,
            memory_refinement=memory_refinement,
        )
    except Exception as e:
        print(f"[adaptive_learning] failed: {e}\n{traceback.format_exc()}")
        return AdaptiveLearningResult(
            learning_summary="Adaptive learning skipped (internal error).",
            notes=[_trunc(str(e), 120)],
            meta={"error": True},
            preference_shifts=[],
        )


def _run_adaptive_learning(
    *,
    g: dict[str, Any],
    heartbeat: Optional[HeartbeatTickResult],
    outcome_learning: Optional[OutcomeLearningResult],
    curiosity: Optional[CuriosityResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    social_continuity: Optional[SocialContinuityResult],
    conversational_nuance: Optional[ConversationalNuanceResult],
    model_routing: Optional[ModelRoutingResult],
    memory_refinement: Optional[MemoryRefinementResult],
) -> AdaptiveLearningResult:
    if g.get("_adaptive_learning_disabled"):
        return AdaptiveLearningResult(
            learning_summary="Adaptive learning disabled via runtime flag.",
            meta={"disabled": True},
            preference_shifts=[],
        )

    prefs = load_preferences()
    w = prefs["weights"]
    ev = prefs["evidence_counts"]
    w_initial = {k: float(w[k]) for k in ALL_FOCUS}
    alpha_base = 0.045
    changed: list[str] = []
    focus = ""
    max_delta = 0.0

    # --- Structured fast path from outcome learning (Phase 27) ---
    ol = outcome_learning
    if ol is not None:
        tgt = str(ol.adjustment_target or "").lower()
        oc = str(ol.outcome_category or "")
        conf = _clamp01(float(ol.adjustment_confidence or 0.0))
        alpha = alpha_base * (0.55 + 0.45 * conf)

        if "workbench" in tgt or "repair" in oc:
            before = w[LearningFocus.REPAIR_PROPOSAL_USEFULNESS]
            tgt_v = 0.62 if bool(ol.should_strengthen) else 0.42 if bool(ol.should_weaken) else before
            w[LearningFocus.REPAIR_PROPOSAL_USEFULNESS] = _ewma(before, tgt_v, alpha)
            ev[LearningFocus.REPAIR_PROPOSAL_USEFULNESS] += 1
            d = abs(w[LearningFocus.REPAIR_PROPOSAL_USEFULNESS] - before)
            if d > max_delta:
                max_delta = d
                focus = LearningFocus.REPAIR_PROPOSAL_USEFULNESS
            if d > 0.015:
                changed.append("repair_proposals")

        if "proactive" in tgt or "trigger" in oc:
            before = w[LearningFocus.PROACTIVE_TRIGGER_USEFULNESS]
            tgt_v = 0.58 if bool(ol.should_strengthen) else 0.44 if bool(ol.should_weaken) else before
            w[LearningFocus.PROACTIVE_TRIGGER_USEFULNESS] = _ewma(before, tgt_v, alpha)
            ev[LearningFocus.PROACTIVE_TRIGGER_USEFULNESS] += 1
            d = abs(w[LearningFocus.PROACTIVE_TRIGGER_USEFULNESS] - before)
            if d > max_delta:
                max_delta = d
                focus = LearningFocus.PROACTIVE_TRIGGER_USEFULNESS
            if d > 0.015:
                changed.append("proactive_triggers")

        if "memory" in tgt:
            before = w[LearningFocus.MEMORY_USEFULNESS]
            tgt_v = 0.58 if bool(ol.should_strengthen) else 0.45 if bool(ol.should_weaken) else before
            w[LearningFocus.MEMORY_USEFULNESS] = _ewma(before, tgt_v, alpha)
            ev[LearningFocus.MEMORY_USEFULNESS] += 1
            d = abs(w[LearningFocus.MEMORY_USEFULNESS] - before)
            if d > max_delta:
                max_delta = d
                focus = LearningFocus.MEMORY_USEFULNESS
            if d > 0.015:
                changed.append("memory_use")

    # Curiosity ↔ outcome coupling (bounded damp/strengthen)
    cq = curiosity
    if cq is not None and bool(cq.curiosity_triggered):
        repeat = bool(outcome_learning.repeated_outcome_pattern) if outcome_learning else False
        swo = outcome_learning
        strengthen = bool(swo.should_strengthen) if swo else False
        weaken = bool(swo.should_weaken) if swo else False
        conf_sw = _clamp01(float(swo.adjustment_confidence or 0.35)) if swo else 0.35
        alpha_cq = alpha_base * (0.55 + 0.45 * conf_sw)
        before = w[LearningFocus.CURIOSITY_USEFULNESS]
        tgt_v = 0.52 if repeat and weaken else 0.56 if strengthen else before
        w[LearningFocus.CURIOSITY_USEFULNESS] = _ewma(before, tgt_v, alpha_cq * 0.85)
        ev[LearningFocus.CURIOSITY_USEFULNESS] += 1

    # --- Reflection / contemplation soft signals ---
    rf = reflection
    if rf is not None:
        oq = str(rf.outcome_quality or "")
        alpha_r = alpha_base * 0.78
        if oq == "good":
            before = w[LearningFocus.PREFERRED_RESPONSE_STYLE]
            w[LearningFocus.PREFERRED_RESPONSE_STYLE] = _ewma(before, 0.58, alpha_r)
            ev[LearningFocus.PREFERRED_RESPONSE_STYLE] += 1
        elif oq == "poor":
            before = w[LearningFocus.PREFERRED_RESPONSE_STYLE]
            w[LearningFocus.PREFERRED_RESPONSE_STYLE] = _ewma(before, 0.44, alpha_r)
            ev[LearningFocus.PREFERRED_RESPONSE_STYLE] += 1

    ct = contemplation
    if ct is not None:
        pw = ct.priority_weights
        if pw is not None:
            remain = float(getattr(pw, "remain_silent", 0.5) or 0.5)
            before = w[LearningFocus.CONVERSATION_PACING]
            w[LearningFocus.CONVERSATION_PACING] = _ewma(before, _clamp01(1.0 - remain * 0.35), alpha_base * 0.55)
            ev[LearningFocus.CONVERSATION_PACING] += 1

    nu = conversational_nuance
    if nu is not None:
        pb = float(nu.pacing_bias or 0.5)
        before = w[LearningFocus.CONVERSATION_PACING]
        w[LearningFocus.CONVERSATION_PACING] = _ewma(before, pb, alpha_base * 0.42)
        ev[LearningFocus.CONVERSATION_PACING] += 1

    soc = social_continuity
    if soc is not None:
        before = w[LearningFocus.SOCIAL_CONTINUITY_USEFULNESS]
        fam = float(soc.familiarity_score or 0.5)
        w[LearningFocus.SOCIAL_CONTINUITY_USEFULNESS] = _ewma(before, 0.45 + 0.22 * fam, alpha_base * 0.38)
        ev[LearningFocus.SOCIAL_CONTINUITY_USEFULNESS] += 1

        warmth = float(soc.warmth_preference_signal or 0.5)
        prac = float(soc.practicality_preference_signal or 0.5)
        comfort = _clamp01(0.52 + 0.16 * warmth - 0.08 * (1.0 - prac))
        before = w[LearningFocus.USER_COMFORT_SIGNAL]
        w[LearningFocus.USER_COMFORT_SIGNAL] = _ewma(before, comfort, alpha_base * 0.33)
        ev[LearningFocus.USER_COMFORT_SIGNAL] += 1

    remain_c = 0.5
    if ct is not None and ct.priority_weights is not None:
        remain_c = float(getattr(ct.priority_weights, "remain_silent", 0.5) or 0.5)
    restraint_b = float(nu.restraint_bias or 0.48) if nu is not None else 0.48
    quiet_pr = float(soc.quiet_preference_signal or 0.5) if soc is not None else 0.5
    hb_streak = int((heartbeat.meta or {}).get("route_fallback_streak", 0)) if heartbeat else 0
    sil_tgt = _clamp01(
        0.46 + 0.22 * remain_c + 0.18 * restraint_b + 0.14 * quiet_pr + 0.025 * min(hb_streak, 8)
    )
    before = w[LearningFocus.SILENCE_WHEN_BETTER]
    w[LearningFocus.SILENCE_WHEN_BETTER] = _ewma(before, sil_tgt, alpha_base * 0.36)
    ev[LearningFocus.SILENCE_WHEN_BETTER] += 1

    mref_lr = memory_refinement
    if mref_lr is not None and bool(getattr(mref_lr.decision, "unfinished_thread_candidate", False)):
        before = w[LearningFocus.MEMORY_USEFULNESS]
        w[LearningFocus.MEMORY_USEFULNESS] = _ewma(before, 0.56, alpha_base * 0.34)
        ev[LearningFocus.MEMORY_USEFULNESS] += 1

    vc = g.get("_voice_conversation")
    if vc is not None:
        intr = str(getattr(vc, "interruption_reason", "") or "")
        timing = getattr(vc, "timing", None)
        wait = bool(getattr(timing, "should_wait", False)) if timing is not None else False
        before = w[LearningFocus.INTERRUPTION_YIELD]
        tgt_v = 0.58 if wait or len(intr) > 1 else 0.5
        w[LearningFocus.INTERRUPTION_YIELD] = _ewma(before, tgt_v, alpha_base * 0.4)
        ev[LearningFocus.INTERRUPTION_YIELD] += 1

    # Heartbeat: learning-review ticks get a tiny exploration nudge (meta only)
    hb = heartbeat
    if hb is not None and str(hb.heartbeat_mode or "") == "learning_review" and bool(
        getattr(hb, "heartbeat_active", False)
    ):
        before = w[LearningFocus.CURIOSITY_USEFULNESS]
        w[LearningFocus.CURIOSITY_USEFULNESS] = _ewma(before, 0.54, alpha_base * 0.25)
        ev[LearningFocus.CURIOSITY_USEFULNESS] += 1

    drift_total = sum(abs(float(w[k]) - w_initial[k]) for k in ALL_FOCUS)
    update_applied = bool(changed) or max_delta >= 0.018 or drift_total >= 0.024
    lconf = _clamp01(
        0.28 + 0.42 * max_delta + 0.06 * min(8, len(changed)) + 0.09 * min(1.0, drift_total)
    )

    if update_applied:
        save_preferences(prefs)

    summ = _trunc(
        ("updated=" + ",".join(changed))
        if changed
        else f"aggregate_drift={drift_total:.3f} max_delta={max_delta:.3f}",
        400,
    )
    meta = {
        "weights_preview": {k: round(float(w[k]), 3) for k in ALL_FOCUS},
        "evidence_totals": {k: int(ev[k]) for k in ALL_FOCUS},
        "respect_identity_anchors": True,
    }

    shifts: list[LearningPreferenceShift] = []
    for fk in ALL_FOCUS:
        b0 = float(w_initial[fk])
        b1 = float(w[fk])
        if abs(b1 - b0) >= 0.007:
            shifts.append(
                LearningPreferenceShift(
                    focus=fk,
                    before=b0,
                    after=b1,
                    reason="bounded_ewma",
                    evidence=_trunc(fk, 96),
                )
            )
    shifts = shifts[:20]

    if update_applied and (changed or max_delta >= 0.02):
        extr = f" shifts={len(shifts)}" if shifts else ""
        print(
            f"[adaptive_learning] focus={focus or 'multi'} update={'yes' if changed else 'drift'} "
            f"conf={lconf:.2f}{extr}"
        )

    return AdaptiveLearningResult(
        learning_update_applied=update_applied,
        learning_focus=focus or ("multi_focus" if len(changed) > 1 else (changed[0] if changed else "")),
        learning_summary=summ,
        learning_confidence=lconf,
        preference_shifts=shifts,
        notes=[],
        meta=meta,
    )


def apply_adaptive_learning_to_perception_state(state: Any, bundle: Any) -> None:
    """Map :class:`AdaptiveLearningResult` onto :class:`~brain.perception.PerceptionState`."""
    al = getattr(bundle, "adaptive_learning", None)
    if al is None:
        state.learning_focus = ""
        state.learning_summary = ""
        state.learning_confidence = 0.0
        state.learning_preference_shifts = []
        return
    state.learning_focus = str(al.learning_focus or "")[:120]
    state.learning_summary = str(al.learning_summary or "")[:600]
    state.learning_confidence = float(al.learning_confidence or 0.0)
    hm = getattr(state, "heartbeat_meta", None)
    if not isinstance(hm, dict):
        state.heartbeat_meta = {}
    state.learning_preference_shifts = [
        {
            "focus": s.focus,
            "before": float(s.before),
            "after": float(s.after),
            "reason": str(s.reason or "")[:120],
            "evidence": str(s.evidence or "")[:160],
        }
        for s in list(getattr(al, "preference_shifts", None) or [])[:16]
    ]
    state.heartbeat_meta["learning"] = {
        "update_applied": bool(al.learning_update_applied),
        "focus": state.learning_focus,
        "confidence": state.learning_confidence,
        "summary": state.learning_summary[:400],
        "notes": list(al.notes or [])[:6],
        "meta": dict(al.meta or {}),
        "preference_shift_count": len(state.learning_preference_shifts),
    }
