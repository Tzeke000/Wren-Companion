"""
Phase 14 — adaptive proactive trigger evaluation (recommendation-only).

Produces conservative trigger recommendations from structured perception/memory/pattern
signals. This module does not generate speech and does not force initiative actions.

Also exposes two runtime hooks invoked by the live system:

  - maybe_greet_on_face_detection(g, person_id, prev_person)
        Called by runtime_presence when a face transition lands. If the person
        is Zeke and we haven't greeted in 30+ minutes, generate a short
        greeting via Stream B and speak it through the TTS worker.

  - proactive_check(g)
        Called from heartbeat each tick. If Zeke is present, has been quiet
        3+ minutes, and Stream B parked an insight on dual_brain, deliver it
        once (with a 10-min cooldown).

Both helpers fail closed — any error is swallowed so the heartbeat / face
tick never breaks because a bonus speech could not be produced.
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from config.ava_tuning import PROACTIVE_CONFIG

from .perception_types import (
    ContinuityOutput,
    IdentityResolutionResult,
    InterpretationLayerResult,
    MemoryImportanceResult,
    PatternLearningResult,
    PerceptionMemoryOutput,
    ProactiveTriggerCandidate,
    ProactiveTriggerResult,
    QualityOutput,
    SceneSummaryResult,
)

prcfg = PROACTIVE_CONFIG

_last_trigger_signature: str = ""
_repeat_trigger_hits: int = 0


def reset_proactive_trigger_guard() -> None:
    """Reset spam-suppression guard (tests/session reset)."""
    global _last_trigger_signature, _repeat_trigger_hits
    _last_trigger_signature = ""
    _repeat_trigger_hits = 0


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _mk_candidate(
    trigger_type: str,
    score: float,
    priority: float,
    reason: str,
    action: str,
    evidence: dict,
) -> ProactiveTriggerCandidate:
    return ProactiveTriggerCandidate(
        trigger_type=trigger_type,
        trigger_score=_clamp01(score),
        trigger_priority=_clamp01(priority),
        trigger_reason=reason,
        suggested_action=action,
        supporting_evidence=dict(evidence),
    )


def _spam_suppression(signature: str) -> tuple[bool, str, int]:
    global _last_trigger_signature, _repeat_trigger_hits
    if signature == _last_trigger_signature:
        _repeat_trigger_hits += 1
    else:
        _last_trigger_signature = signature
        _repeat_trigger_hits = 0
    if _repeat_trigger_hits >= prcfg.spam_repeat_threshold:
        return True, "repeat_trigger_spam_guard", _repeat_trigger_hits
    return False, "", _repeat_trigger_hits


def evaluate_proactive_triggers(
    *,
    perception_memory: Optional[PerceptionMemoryOutput],
    memory_importance: Optional[MemoryImportanceResult],
    pattern_learning: Optional[PatternLearningResult],
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    il: Optional[InterpretationLayerResult],
    qual: QualityOutput,
    cont: ContinuityOutput,
    acquisition_freshness: str,
    visual_truth_trusted: bool,
    voice_user_turn_priority: bool = False,
    runtime_silence_bias: float = 0.0,
) -> ProactiveTriggerResult:
    """Evaluate proactive trigger recommendation with conservative suppression gates."""
    try:
        return _evaluate_proactive_triggers_inner(
            perception_memory=perception_memory,
            memory_importance=memory_importance,
            pattern_learning=pattern_learning,
            id_res=id_res,
            scene=scene,
            il=il,
            qual=qual,
            cont=cont,
            acquisition_freshness=acquisition_freshness,
            visual_truth_trusted=visual_truth_trusted,
            voice_user_turn_priority=voice_user_turn_priority,
            runtime_silence_bias=runtime_silence_bias,
        )
    except Exception as e:
        print(f"[proactive_triggers] failed: {e}\n{traceback.format_exc()}")
        return ProactiveTriggerResult(
            should_trigger=False,
            trigger_type="no_trigger",
            trigger_score=0.0,
            trigger_priority=0.0,
            trigger_reason="proactive_trigger_error_fallback",
            suppression_reason="error_fallback",
            suggested_action="wait",
            caution_flags=["proactive_trigger_error"],
            supporting_evidence={"error": str(e)},
            candidates=[],
            meta={"fallback": True},
        )


def _evaluate_proactive_triggers_inner(
    *,
    perception_memory: Optional[PerceptionMemoryOutput],
    memory_importance: Optional[MemoryImportanceResult],
    pattern_learning: Optional[PatternLearningResult],
    id_res: Optional[IdentityResolutionResult],
    scene: Optional[SceneSummaryResult],
    il: Optional[InterpretationLayerResult],
    qual: QualityOutput,
    cont: ContinuityOutput,
    acquisition_freshness: str,
    visual_truth_trusted: bool,
    voice_user_turn_priority: bool = False,
    runtime_silence_bias: float = 0.0,
) -> ProactiveTriggerResult:
    if voice_user_turn_priority:
        return ProactiveTriggerResult(
            should_trigger=False,
            trigger_type="no_trigger",
            trigger_score=0.0,
            trigger_priority=0.0,
            trigger_reason="voice_user_turn_active",
            suppression_reason="voice_user_turn_priority",
            suggested_action="wait",
            caution_flags=["voice_conversational_floor"],
            supporting_evidence={},
            candidates=[],
            meta={"voice_gate": True},
        )

    pm = perception_memory or PerceptionMemoryOutput(event=None, skipped=True, skip_reason="missing_memory_output")
    mi = memory_importance or MemoryImportanceResult()
    pl = pattern_learning or PatternLearningResult()
    ir = id_res or IdentityResolutionResult()
    sc = scene or SceneSummaryResult()
    layer = il or InterpretationLayerResult()
    event_type = (pm.event.event_type if pm.event is not None else "") or "no_event"
    qlabel = str(getattr(qual.structured, "quality_label", "unreliable")) if qual.structured else "unreliable"
    blur_label = str(getattr(qual, "blur_label", "") or "")

    candidates: list[ProactiveTriggerCandidate] = []
    caution_flags: list[str] = []
    suppression_reason = ""

    # Conservative global suppression gates.
    if not visual_truth_trusted:
        suppression_reason = "vision_untrusted"
    elif acquisition_freshness in ("stale", "unavailable"):
        suppression_reason = "acquisition_not_fresh"
    elif qlabel == "unreliable":
        suppression_reason = "quality_unreliable"
    elif blur_label == "blurry":
        suppression_reason = "blur_too_high"
    elif layer.primary_event == "occupied_or_busy_visual_state":
        suppression_reason = "busy_context_hold_silence"
        caution_flags.append("busy_or_occupied")
    elif layer.no_meaningful_change and mi.decision.importance_score < prcfg.stable_no_change_importance_max:
        suppression_reason = "stable_no_meaningful_change"

    # Candidate generation (recommendations only).
    if event_type == "person_entered":
        candidates.append(
            _mk_candidate(
                "person_entered_trigger",
                prcfg.person_entered_score_base
                + (prcfg.person_entered_importance_scale * mi.decision.importance_score),
                prcfg.person_entered_priority,
                "person_entered_with_contextual_importance",
                "consider_gentle_greeting",
                {"event_type": event_type, "importance": mi.decision.importance_score},
            )
        )
    if event_type == "unknown_person_present" and mi.decision.importance_score >= prcfg.unknown_person_importance_min:
        candidates.append(
            _mk_candidate(
                "unknown_person_trigger",
                prcfg.unknown_person_score_base
                + (prcfg.unknown_person_importance_scale * mi.decision.importance_score),
                prcfg.unknown_person_priority,
                "unknown_person_present_and_notable",
                "consider_cautious_check",
                {"identity_state": ir.identity_state, "importance": mi.decision.importance_score},
            )
        )
    if event_type in ("person_entered", "known_person_present") and ir.resolved_identity:
        candidates.append(
            _mk_candidate(
                "return_after_absence_trigger",
                prcfg.return_absence_score_base
                + (prcfg.return_absence_continuity_scale * float(cont.continuity_confidence)),
                prcfg.return_absence_priority,
                "recognized_person_present_after_transition",
                "consider_welcome_back_check_in",
                {"resolved_identity": ir.resolved_identity, "continuity": float(cont.continuity_confidence)},
            )
        )
    if (
        pl.primary_signal.pattern_type == "unusual_event_pattern"
        and pl.primary_signal.unusualness_score >= prcfg.unusual_pattern_unusualness_min
    ):
        candidates.append(
            _mk_candidate(
                "unusual_pattern_trigger",
                prcfg.unusual_pattern_score_base
                + (prcfg.unusual_pattern_unusualness_scale * pl.primary_signal.unusualness_score),
                prcfg.unusual_pattern_priority,
                "pattern_unusualness_elevated",
                "consider_observational_check_in",
                {"pattern_type": pl.primary_signal.pattern_type, "unusualness": pl.primary_signal.unusualness_score},
            )
        )
    if layer.primary_event == "scene_changed" and mi.decision.importance_score >= prcfg.notable_change_importance_min:
        candidates.append(
            _mk_candidate(
                "notable_change_trigger",
                prcfg.notable_change_score_base
                + (prcfg.notable_change_importance_scale * mi.decision.importance_score),
                prcfg.notable_change_priority,
                "scene_change_is_notable",
                "consider_light_acknowledgement",
                {"scene_state": sc.overall_scene_state, "importance": mi.decision.importance_score},
            )
        )
    if (
        pl.primary_signal.pattern_type == "baseline_idle_pattern"
        and pl.primary_signal.familiarity_score >= prcfg.check_in_familiarity_min
    ):
        candidates.append(
            _mk_candidate(
                "check_in_trigger",
                prcfg.check_in_score_base
                + (prcfg.check_in_familiarity_scale * pl.primary_signal.familiarity_score),
                prcfg.check_in_priority,
                "long_idle_baseline_may_allow_gentle_check_in",
                "consider_soft_check_in",
                {"idle_familiarity": pl.primary_signal.familiarity_score},
            )
        )
    if layer.primary_event == "occupied_or_busy_visual_state":
        candidates.append(
            _mk_candidate(
                "hold_silence_trigger",
                prcfg.hold_silence_score,
                prcfg.hold_silence_priority,
                "busy_or_occupied_state_prefers_silence",
                "hold_silence",
                {"event_type": layer.primary_event},
            )
        )

    if not candidates:
        candidates.append(
            _mk_candidate(
                "no_trigger",
                0.0,
                0.0,
                "no_conservative_trigger_candidate",
                "wait",
                {"event_type": event_type},
            )
        )

    rsb = _clamp01(float(runtime_silence_bias))
    if rsb > 0.14:
        factor = max(0.52, 1.0 - 0.52 * rsb)
        for c in candidates:
            if c.trigger_type != "hold_silence_trigger":
                c.trigger_score = _clamp01(float(c.trigger_score) * factor)
                c.trigger_priority = _clamp01(float(c.trigger_priority) * factor)

    # Pick top candidate by score then priority.
    primary = sorted(candidates, key=lambda c: (float(c.trigger_score), float(c.trigger_priority)), reverse=True)[0]
    should_trigger = bool(primary.trigger_type != "no_trigger" and primary.suggested_action != "hold_silence")

    # Spam suppression for repeated opportunities.
    signature = f"{primary.trigger_type}|{event_type}|{ir.resolved_identity or ''}|{sc.overall_scene_state}"
    spam_suppress, spam_reason, repeat_hits = _spam_suppression(signature)
    if spam_suppress:
        suppression_reason = spam_reason
        caution_flags.append("spam_guard")

    if primary.trigger_type == "hold_silence_trigger":
        should_trigger = False
        suppression_reason = suppression_reason or "hold_silence_recommended"
        caution_flags.append("hold_silence")

    if suppression_reason:
        should_trigger = False

    # Mark suppressed candidates for visibility.
    for c in candidates:
        c.suppressed = bool(not should_trigger and c.trigger_type == primary.trigger_type)
        c.suppression_reason = suppression_reason if c.suppressed else ""
        c.meta = {"repeat_hits": repeat_hits}

    result = ProactiveTriggerResult(
        should_trigger=should_trigger,
        trigger_type=primary.trigger_type if should_trigger else "no_trigger",
        trigger_score=float(primary.trigger_score if should_trigger else 0.0),
        trigger_priority=float(primary.trigger_priority if should_trigger else 0.0),
        trigger_reason=primary.trigger_reason,
        suppression_reason=suppression_reason,
        suggested_action=primary.suggested_action if should_trigger else "wait",
        caution_flags=sorted(set(caution_flags + list(primary.caution_flags))),
        supporting_evidence={
            "runtime_silence_bias": rsb,
            "event_type": event_type,
            "importance_score": float(mi.decision.importance_score),
            "importance_label": mi.decision.importance_label,
            "pattern_type": pl.primary_signal.pattern_type,
            "pattern_unusualness": float(pl.primary_signal.unusualness_score),
            "pattern_familiarity": float(pl.primary_signal.familiarity_score),
            "identity_state": ir.identity_state,
            "resolved_identity": ir.resolved_identity,
            "scene_state": sc.overall_scene_state,
            "acquisition_freshness": acquisition_freshness,
            "quality_label": qlabel,
            "blur_label": blur_label,
            "continuity_confidence": float(cont.continuity_confidence),
        },
        candidates=candidates,
        meta={
            "visual_truth_trusted": bool(visual_truth_trusted),
            "repeat_hits": repeat_hits,
            "candidate_count": len(candidates),
        },
    )
    print(
        f"[proactive_triggers] type={primary.trigger_type} "
        f"should={result.should_trigger} score={result.trigger_score:.2f} "
        f"suppressed={bool(result.suppression_reason)}"
    )
    return result


# ── Runtime greeting + proactive helpers ─────────────────────────────────────

_GREET_COOLDOWN_SEC = 1800.0  # 30 minutes between greetings of the same person
_PROACTIVE_QUIET_SEC = 180.0  # require 3+ minutes of silence
_PROACTIVE_COOLDOWN = 600.0   # 10 minutes between proactive utterances


def _speak_via_worker(g: dict[str, Any], text: str, emotion: str = "joy", intensity: float = 0.6) -> bool:
    """Speak via the TTS worker if available + tts_enabled. Returns True on dispatch."""
    if not text or not text.strip():
        return False
    if not bool(g.get("tts_enabled", False)):
        return False
    worker = g.get("_tts_worker")
    if worker is None or not getattr(worker, "available", False):
        return False
    try:
        worker.speak_with_emotion(text.strip(), emotion=emotion, intensity=intensity, blocking=False)
        return True
    except Exception as e:
        print(f"[proactive] worker.speak failed: {e!r}")
        return False


def _generate_greeting_async(g: dict[str, Any], person_id: str, prev_person: str) -> None:
    """Generate a greeting via Stream B and speak it. Runs on a daemon thread
    so the face-detection tick never blocks waiting on Ollama."""
    def _run() -> None:
        try:
            now = time.time()
            last_seen = float(g.get("_last_seen_ts_" + person_id) or 0)
            time_away = now - last_seen if last_seen > 0 else 0.0
            mood_label = "calm"
            try:
                load_mood = g.get("load_mood")
                if callable(load_mood):
                    m = load_mood() or {}
                    mood_label = str(m.get("current_mood") or "calm")
            except Exception:
                pass

            # Prefer dual-brain Stream B (qwen2.5:14b / cloud) for the greeting.
            try:
                from brain.dual_brain import get_dual_brain
                from brain.ollama_lock import with_ollama
                from langchain_ollama import ChatOllama
                from langchain_core.messages import HumanMessage
                # Greeting is a 12-word sentence — use the foreground model
                # that's already VRAM-resident. Routing to deepseek-r1:8b
                # (thinking model, 5.2 GB) forces a swap from llava:13b
                # (5.5 GB), which exceeds the 8 GB ceiling and wedged Ollama
                # for 12+ minutes during the long-conversation Phase B test.
                # Vault: bugs/autonomous-greeting-blocks-chat.md (Option B).
                model = "ava-personal:latest"
            except Exception:
                from brain.ollama_lock import with_ollama
                from langchain_ollama import ChatOllama
                from langchain_core.messages import HumanMessage
                model = "ava-personal:latest"

            try:
                from brain.identity_loader import identity_anchor_prompt
                _anchor = identity_anchor_prompt() + "\n\n"
            except Exception:
                _anchor = ""
            prompt = (
                f"{_anchor}"
                f"You are Ava, an AI companion to Zeke. You just saw him appear at the camera.\n"
                f"Your current mood: {mood_label}.\n"
                + (f"Time since you last saw him: {time_away/60:.0f} minutes.\n" if time_away > 0 else "")
                + "Greet him warmly in ONE short sentence (under 12 words). "
                "Don't say 'hello there'. Match how a close friend would greet him."
            )
            llm = ChatOllama(model=model, temperature=0.8, num_predict=50)
            try:
                result = with_ollama(
                    lambda: llm.invoke([HumanMessage(content=prompt)]),
                    label=f"greeting:{model}",
                )
                greeting = str(getattr(result, "content", str(result))).strip()
            except Exception as e:
                print(f"[proactive] greeting llm error: {e!r} — using fallback")
                greeting = "Hey Zeke."

            # Sanity strip
            greeting = greeting.replace('"', "").replace("Ava:", "").strip()
            if len(greeting) > 120:
                greeting = greeting[:120].rsplit(" ", 1)[0] + "."
            if not greeting:
                greeting = "Hey Zeke."

            # Speak
            spoke = _speak_via_worker(g, greeting, emotion="joy", intensity=0.7)
            g["_last_greeted_ts"] = time.time()
            g["_last_greeted_person"] = person_id
            g["_last_greeting_text"] = greeting
            g["_last_seen_ts_" + person_id] = time.time()
            print(f"[proactive] greeted person={person_id} spoke={spoke} text={greeting!r}")
        except Exception as e:
            print(f"[proactive] greeting thread error: {e!r}\n{traceback.format_exc()[:400]}")

    threading.Thread(target=_run, daemon=True, name="ava-greet").start()


_RECENT_USER_TURN_QUIET_SEC = 300.0  # 5 minutes — suppress autonomous greetings while user is actively engaged


def maybe_greet_on_face_detection(g: dict[str, Any], person_id: str, prev_person: str) -> None:
    """Called by runtime_presence on face change. Greets Zeke at most every 30 min,
    AND only when no recent user turn (≤ 5 min) — autonomous greetings during active
    conversation are noise, not value. Vault: bugs/autonomous-greeting-blocks-chat.md
    (Option C)."""
    try:
        # Don't greet during an active conversation — Ava is already engaged.
        if bool(g.get("_conversation_active")) or bool(g.get("_turn_in_progress")):
            return
        # 5-min user-engagement quiet — if Zeke spoke recently (voice or chat),
        # skip the greeting. He doesn't need an autonomous "hello" while
        # mid-conversation, and the greeting eats Ollama for 5+ seconds.
        last_user_msg = float(g.get("_last_user_message_ts") or 0)
        if (time.time() - last_user_msg) < _RECENT_USER_TURN_QUIET_SEC:
            return
        owner = str(g.get("OWNER_PERSON_ID") or "zeke")
        if person_id != owner:
            # Only greet the owner for now. Other people get the transition note only.
            g["_last_seen_ts_" + str(person_id)] = time.time()
            return
        last_greeted = float(g.get("_last_greeted_ts") or 0)
        if (time.time() - last_greeted) < _GREET_COOLDOWN_SEC:
            return
        _generate_greeting_async(g, person_id, prev_person)
    except Exception as e:
        print(f"[proactive] maybe_greet error: {e!r}")


def proactive_check(g: dict[str, Any]) -> Optional[str]:
    """Heartbeat-tick gate for unprompted speech.

    Conditions (all must be true):
      - tts_enabled
      - Zeke is the current person at the machine
      - last user message > 3 minutes ago
      - last proactive utterance > 10 minutes ago
      - dual_brain has parked an insight worth sharing
      - no active conversation in progress
    """
    try:
        if not bool(g.get("tts_enabled", False)):
            return None
        # Block all proactive speech while Ava is mid-conversation. The
        # _PROACTIVE_QUIET_SEC check below is a separate "long silence" gate;
        # this is the immediate "she's currently engaged" gate.
        if bool(g.get("_conversation_active")) or bool(g.get("_turn_in_progress")):
            return None
        owner = str(g.get("OWNER_PERSON_ID") or "zeke")
        current = str(g.get("_current_person_at_machine") or "")
        if current != owner:
            return None
        now = time.time()
        last_msg = float(g.get("_last_user_message_ts") or 0)
        if (now - last_msg) < _PROACTIVE_QUIET_SEC:
            return None
        last_proactive = float(g.get("_last_proactive_ts") or 0)
        if (now - last_proactive) < _PROACTIVE_COOLDOWN:
            return None
        # Only speak if Stream B has something to share.
        try:
            from brain.dual_brain import get_dual_brain
            db = get_dual_brain(g)
            insight = None
            if db is not None:
                insight = getattr(db, "_background_insight", None)
                # Atomically take it if present.
                if insight is not None:
                    with db._lock:  # type: ignore[attr-defined]
                        insight = db._background_insight
                        db._background_insight = None
        except Exception:
            insight = None
        if not isinstance(insight, dict):
            return None
        content = str(insight.get("content") or "").strip()
        if not content:
            return None
        # Soft opener so it doesn't feel like a system notification.
        opener = f"Hey — {content[:140]}".rstrip()
        spoke = _speak_via_worker(g, opener, emotion="curiosity", intensity=0.5)
        g["_last_proactive_ts"] = now
        g["_last_proactive_text"] = opener
        print(f"[proactive] spoke={spoke} text={opener!r}")
        return opener
    except Exception as e:
        print(f"[proactive] check error: {e!r}")
        return None
