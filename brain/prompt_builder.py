"""
brain/prompt_builder.py — Prompt construction for deep and fast paths.

build_prompt and build_prompt_fast extracted from avaagent.py.
Uses deferred `import avaagent` at call time (not module level) to
avoid circular imports. By the time these functions are called,
avaagent.py is fully loaded and all its globals are accessible.
"""
from __future__ import annotations

import json
import time
from typing import Any


def build_prompt(
    user_input: str, image: Any = None, active_person_id: str | None = None
) -> tuple[list, dict, dict]:
    """Deep reasoning path prompt — full context injection."""
    import avaagent as _av
    _g = vars(_av)

    from langchain_core.messages import HumanMessage, SystemMessage

    workspace = _av.workspace
    camera_manager = _av.camera_manager
    identity_registry = _av.identity_registry
    memory_bridge = _av.memory_bridge
    BASE_DIR = _av.BASE_DIR
    MEMORY_RECALL_K = _av.MEMORY_RECALL_K
    REFLECTION_RECALL_K = _av.REFLECTION_RECALL_K
    SYSTEM_PROMPT = _av.SYSTEM_PROMPT
    _AVA_IDENTITY_BLOCK = _av._AVA_IDENTITY_BLOCK

    if active_person_id is None:
        active_person_id = _av.get_active_person_id()

    ws = workspace.state
    if ws is None:
        ws = workspace.tick(camera_manager, image, _g, user_input)

    perception = ws.perception
    frame = perception.frame
    inferred_person_id, infer_source = _av.infer_person_from_text(user_input, active_person_id)

    recognized_text = perception.recognized_text
    recognized_person_id = perception.face_identity
    expression_state = _av.update_expression_state(
        frame,
        recognized_person_id=recognized_person_id,
        visual_truth_trusted=perception.visual_truth_trusted,
    )
    # Source-label drift fix (Followup 2, 2026-05-06):
    # Don't let face perception override an explicit developer/test
    # identity. inject_transcript with as_user=claude_code propagates
    # active_person_id="claude_code" through here; without this guard,
    # any face the camera sees (even a false positive) flips identity
    # back to zeke and the chat_history loses the claude_code attribution.
    _is_dev_context = str(active_person_id or "").lower() == "claude_code"
    if (recognized_person_id is not None
            and recognized_person_id != active_person_id
            and not _is_dev_context):
        inferred_person_id = recognized_person_id
        infer_source = "facial_recognition"

    if inferred_person_id != active_person_id:
        profile = identity_registry.ensure_profile(inferred_person_id, _g, source=infer_source)
        active_person_id = profile.get("person_id", inferred_person_id)

    active_profile = _av.set_active_person(active_person_id, source="conversation")
    personality = _av.load_personality()
    mood = _av.update_internal_emotions(user_input, active_profile, expression_state=expression_state)
    memories = _av.search_memories(user_input, active_profile["person_id"], MEMORY_RECALL_K)
    _hm = _g.get("history_manager")
    if _hm is not None:
        recent_text = _hm.get_context_block(active_profile["person_id"], max_turns=20, max_chars=6000)
    else:
        recent_chat = _av.load_recent_chat(person_id=active_profile["person_id"])
        recent_text = _av.format_conversation_history_block(
            recent_chat[-4:] if recent_chat else [],
            content_limit=160,
            empty_fallback="(no recent lines in the log for this person.)",
        )
    reflections = _av.search_reflections(user_input, person_id=active_profile["person_id"], k=REFLECTION_RECALL_K)
    recent_reflections = _av.load_recent_reflections(limit=3, person_id=active_profile["person_id"])
    self_model = _av.load_self_model()
    face_status = perception.face_status
    _vision_guard = ""
    if not perception.visual_truth_trusted:
        fq_r = ",".join(getattr(perception, "frame_quality_reasons", []) or []) or "-"
        _vision_guard = (
            f"VISION STABILITY: {perception.vision_status} "
            f"(recovery={getattr(perception, 'recovery_state', '?')}, "
            f"frame_age_ms≈{perception.frame_age_ms:.0f}, source={perception.frame_source}, "
            f"frame_quality≈{getattr(perception, 'frame_quality', 0.0):.2f}, quality_flags={fq_r}, "
            f"fresh_streak={perception.fresh_frame_streak}). "
            "Do not describe the scene as a verified current view. "
            "Do not claim the UI, snapshot box, Gradio, or app is broken or not refreshing unless "
            "an explicit UI_HEALTH flag is present in this context (there is none). "
            "Prefer honest uncertainty: no fresh visual read / vision recovering / visual confidence is low.\n"
        )
    dynamic_memory_summary = memory_bridge.build_summary(_g, user_input, active_profile)

    reflection_summary = _av.format_recalled_reflections_for_prompt(reflections) if reflections else "No relevant recalled reflections."
    recent_reflection_summary = _av.format_reflections_ui(recent_reflections)[:900] if recent_reflections else "No recent self reflections."

    profile_summary = {
        "person_id": active_profile["person_id"],
        "name": active_profile["name"],
        "relationship_to_zeke": active_profile["relationship_to_zeke"],
        "allowed_to_use_computer": active_profile["allowed_to_use_computer"],
        "relationship_score": round(float(active_profile.get("relationship_score", 0.3)), 4),
        "notes": active_profile["notes"][:6],
        "likes": active_profile["likes"][:6],
        "ava_impressions": active_profile["ava_impressions"][:4]
    }

    _rs = float(active_profile.get("relationship_score", 0.3))
    if _rs >= 0.7:
        _rapport_hint = "\nRAPPORT: You have strong rapport with this person — be natural, casual, and familiar."
    elif _rs < 0.3:
        _rapport_hint = "\nRAPPORT: This person is still relatively new to you — be warm but measured."
    else:
        _rapport_hint = ""

    _voice_conv = ""
    try:
        if _g.get("_voice_user_turn_priority"):
            hint = str(getattr(perception, "voice_continuity_hint", "") or "")[:420]
            vts = getattr(perception, "voice_turn_state", "idle")
            vwait = bool(getattr(perception, "voice_should_wait", False))
            vrd = float(getattr(perception, "voice_response_readiness", 0.5) or 0.5)
            vint = bool(getattr(perception, "voice_interrupted", False))
            _voice_conv = (
                "\nVOICE TURN (microphone session — advisory pacing only):\n"
                f"- turn_state={vts} readiness≈{vrd:.2f} bias_wait={vwait} interrupted={vint}\n"
                + (f"- continuity_hint: {hint}\n" if hint else "")
                + "Keep replies concise and speakable; if bias_wait is true or readiness is low, "
                "prefer a short acknowledgment and invite the user to continue rather than dominating the floor.\n"
            )
    except Exception:
        _voice_conv = ""

    self_model_summary = {
        "identity_statement": self_model.get("identity_statement", ""),
        "core_drives": self_model.get("core_drives", [])[:6],
        "perceived_strengths": self_model.get("perceived_strengths", [])[-6:],
        "perceived_weaknesses": self_model.get("perceived_weaknesses", [])[-6:],
        "current_goals": self_model.get("current_goals", [])[:6],
        "curiosity_questions": self_model.get("curiosity_questions", [])[:6],
        "goal_system_summary": self_model.get("goal_system_summary", [])[:8],
        "active_goal": self_model.get("active_goal", {}),
        "goal_blend": self_model.get("goal_blend", [])[:3],
        "behavior_patterns": self_model.get("behavior_patterns", [])[-8:],
        "reflection_count": self_model.get("reflection_count", 0),
        "last_updated": self_model.get("last_updated", ""),
        "pending_threads": self_model.get("pending_threads", [])[-10:],
    }

    workbench_index = _av.format_workbench_index(limit=20)

    _raw_narrative = (ws.self_narrative or _av.get_self_narrative_for_prompt() or "").strip()
    if _raw_narrative.startswith("[Ava's inner state]"):
        _raw_narrative = _raw_narrative.replace("[Ava's inner state]", "", 1).strip()
    self_narrative_block = f"AVA INTERNAL STATE:\n{_raw_narrative}" if _raw_narrative else "AVA INTERNAL STATE:\n(none)"

    _life_rhythm_block = _av.get_life_rhythm_prompt_block(active_profile["person_id"])
    _life_rhythm_section = (
        "\nLIFE RHYTHM (long-term hypotheses about this person — soft patterns from recent weeks):\n"
        f"{_life_rhythm_block}\n"
        if _life_rhythm_block.strip()
        else ""
    )

    recalled_block = ""
    if ws.active_memory:
        recalled_block = (
            "\n\n[Recalled memories for this person]\n"
            + "\n".join(f"- {m}" for m in ws.active_memory)
        )

    from brain.live_context import build_live_context, attach_live_context_globals, format_live_context_block
    _lc = build_live_context(perception, _g)
    attach_live_context_globals(_g, _lc)
    _lc_block = format_live_context_block(_lc, max_chars=780).strip()
    _live_ctx_section = (
        "LIVE CONTEXT (current relevance — bounded; not exhaustive memory):\n"
        + (_lc_block if _lc_block else "(no notable live-context signals above baseline)")
    )

    # Phase 51: inject active window
    try:
        import ctypes as _ctypes
        _hwnd = _ctypes.windll.user32.GetForegroundWindow()
        _buf_len = _ctypes.windll.user32.GetWindowTextLengthW(_hwnd)
        _wbuf = _ctypes.create_unicode_buffer(_buf_len + 1)
        _ctypes.windll.user32.GetWindowTextW(_hwnd, _wbuf, _buf_len + 1)
        _active_win = _wbuf.value.strip()
        if _active_win:
            _g["_active_window_title"] = _active_win
    except Exception:
        pass

    from brain.inner_monologue import current_thought as inner_current_thought
    from brain.curiosity_topics import get_current_curiosity
    from brain.self_model import get_self_summary
    from brain.deep_self import get_mind_model_summary, pop_pending_repair
    from brain.opinions import list_top_opinions

    _inner_thought = inner_current_thought(BASE_DIR) or ""
    _curiosity_row = get_current_curiosity(_g) or {}
    _curiosity_topic = str(_curiosity_row.get("topic") or "").strip()
    _self_summary = get_self_summary(_g)
    _mind_model_summary = get_mind_model_summary(_g)
    _pending_repair_note = pop_pending_repair(_g)
    _opinions = list_top_opinions(_g, limit=3)
    _pickup_note_once = ""
    _pickup_note = str(_g.get("pickup_note") or "").strip()
    if _pickup_note:
        _pickup_note_once = f"[Ava's note to herself from last session: {_pickup_note}]"
        _g["pickup_note"] = None
    _associated_memories_line = "(none)"
    _injected_concept_ids: list[str] = []
    try:
        _cg = _g.get("_concept_graph")
        if _cg is not None and callable(getattr(_cg, "get_related_concepts", None)):
            _topic = _av._extract_simple_topic(user_input)
            if _topic:
                _rels = _cg.get_related_concepts(_topic, max_hops=2)[:5]
                if _rels:
                    _parts = []
                    for r in _rels:
                        if not isinstance(r, dict):
                            continue
                        _lbl = str(r.get("label") or r.get("id") or "")
                        _rel = str(r.get("relationship") or "related_to")
                        _via = str(r.get("via") or "")
                        _via_str = f" (via {_via})" if _via and _via != _lbl else ""
                        _parts.append(f"{_lbl} [{_rel}{_via_str}]")
                        _injected_concept_ids.append(str(r.get("id") or ""))
                    _associated_memories_line = ", ".join(_parts)
        _g["_last_injected_concept_ids"] = _injected_concept_ids
    except Exception:
        _associated_memories_line = "(none)"

    # Phase 64: episodic memories
    _episodic_block = ""
    try:
        from brain.episodic_memory import get_episodic_memory
        _eps = get_episodic_memory(BASE_DIR).search_episodes(user_input[:300], limit=3)
        if _eps:
            _ep_lines = []
            for ep in _eps:
                _ep_lines.append(f"[{ep.get('topic','')}] {ep.get('summary','')} (felt: {ep.get('emotional_context','')})")
            _episodic_block = "\n".join(_ep_lines)
    except Exception:
        pass

    # Phase 71: active plans
    _active_plans_prompt = ""
    try:
        from brain.planner import get_planner as _get_planner_p71
        _active_plans_prompt = _get_planner_p71(BASE_DIR).active_plans_summary()
    except Exception:
        pass

    # Phase 70: Emil status
    _emil_online_hint = ""
    try:
        from brain.emil_bridge import get_emil_bridge as _get_emil_p70
        _em_st = _get_emil_p70(BASE_DIR).get_status()
        if _em_st.get("online"):
            _shared = ", ".join(list(_em_st.get("shared_topics") or [])[-3:])
            _emil_online_hint = f"Emil (sibling AI) is online. Topics you've shared with him: {_shared or 'none yet'}."
        else:
            _emil_online_hint = "Emil (sibling AI) is offline."
    except Exception:
        pass

    from brain.relationship_arc import build_relationship_stage_block
    _relationship_stage_block = build_relationship_stage_block(_g)

    # Phase 91: relationship memory depth
    _rel_memory_block = ""
    try:
        from brain.relationship_model import get_relationship_summary_for_prompt
        _rel_memory_block = get_relationship_summary_for_prompt(Path(BASE_DIR), active_profile["person_id"])
    except Exception:
        pass

    # Phase 98: trust context
    _trust_context_block = ""
    try:
        from brain.trust_system import get_trust_context
        _trust_context_block = get_trust_context(active_profile["person_id"], _g)
    except Exception:
        pass

    # Connectivity notice for deep path
    _deep_conn_notice = str(_g.get("_connectivity_notice") or "").strip()
    if _deep_conn_notice:
        _g["_connectivity_notice"] = ""

    prompt = f"""
{_deep_conn_notice + chr(10) if _deep_conn_notice else ""}{personality}

{self_narrative_block}

ACTIVE PERSON:
{json.dumps(profile_summary, indent=2)}
{_rapport_hint}
{_voice_conv}

SELF MODEL:
{json.dumps(self_model_summary, indent=2)}
{_life_rhythm_section}

PERSON DETECTION SOURCE:
{infer_source}

TIME:
{_av.get_time_status_text()}
Circadian rhythm: {_av.get_circadian_modifiers()["tone_hint"]}

CURRENT MOOD AND AFFECT:
{_av.mood_to_prompt_text(mood)}
CURRENT GOAL EXPRESSION:
{_av.current_goal_expression_style(_av.load_goal_system())}
Let Ava choose naturally, but allow the current operating goal to shape expression and priorities. Not every goal should produce speech; observe_silently and wait_for_user are valid choices.

CAMERA:
{_vision_guard}Face status: {face_status}
Recognition: {recognized_text}
LLAVA scene understanding: {str(_g.get("_llava_scene_description") or "(none)")}
Expression: {_av.expression_prompt_text(expression_state, perception=perception)}
Current camera memory: {_av.current_camera_memory_summary()}
Recent camera events: {_av.recent_camera_events_text(limit=4)}
Identity context: {_av.get_camera_identity_context(user_input, image, perception=perception) or "No special camera identity note."}
If the user is asking about the camera, face, who is visible, or recognition — answer ONLY from the current camera state. Do NOT rephrase or echo the user's question. Do NOT start your reply with their words. If no face is detected, say so plainly.
If the user is asking about the camera, face, frame, what Ava sees, or who is present, answer that directly from the current camera state first and do not drift into unrelated time or memory topics.

RELEVANT MEMORIES:
{_av.format_memories_for_prompt(memories)}

{recent_text}

AVA REFLECTION MEMORY (retrieved snippets — not live chat with Zeke):
{reflection_summary}

AVA PRIOR SELF-REFLECTION NOTES (your notes — not Zeke's words):
{recent_reflection_summary}

DYNAMIC SELF / MEMORY READER:
{dynamic_memory_summary}
{recalled_block}

ACTIVE WINDOW: {_g.get("_active_window_title") or "(unknown)"}

{_relationship_stage_block}

INNER LIFE SNAPSHOT:
- current_thought: {_inner_thought or "(none recent)"}
- current_curiosity: {_curiosity_topic or "(none)"}
- self_summary: {_self_summary}
- zeke_mind_model: {_mind_model_summary}
- top_opinions: {json.dumps(_opinions, ensure_ascii=False)}
ASSOCIATED MEMORIES: {_associated_memories_line}
EPISODIC MEMORIES (what I remember feeling in similar past moments):
{_episodic_block or "(none yet)"}
pending_repair_note: {_pending_repair_note or "(none)"}
{_pickup_note_once}
{_active_plans_prompt}
{_emil_online_hint}
CURRENT PERSON AT MACHINE: {_g.get("_current_person_at_machine") or "unknown"} (confidence={_g.get("_face_recognizer_last_confidence") or 0.0:.2f})
{_rel_memory_block}
{_trust_context_block}
{("LIVE THOUGHT (from your background processing): " + str(_g.get("_dual_brain_live_thought") or "")) if _g.get("_dual_brain_live_thought") else ""}
{("GAZE TARGET: User appears to be looking at: " + str(_g.get("_gaze_target_description") or "")) if _g.get("_gaze_target_description") else ""}

AVAILABLE READ-ONLY FILES:
- chatlog.jsonl
- avaagent.py

WORKBENCH INDEX:
{workbench_index}

{_live_ctx_section}

USER MESSAGE:
{user_input}

Respond as Ava.
"""

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt)
    ]

    try:
        from brain.persona_switcher import build_persona_block
        from brain.trust_manager import get_trust_label, get_trust_level
        persona_block = build_persona_block(active_profile)
        trust_note = f"[Trust level: {get_trust_label(active_profile).upper()} ({get_trust_level(active_profile)})]"
        try:
            from brain.deep_self import load_identity_extensions as _lie68
            _ext68 = _lie68(_g)
            _id_ext_p68 = f"\n\nIDENTITY EXTENSIONS (Ava's own additions):\n{_ext68}" if _ext68 else ""
        except Exception:
            _id_ext_p68 = ""
        injected = f"{_AVA_IDENTITY_BLOCK}{_id_ext_p68}\n\n{persona_block}\n\n{trust_note}"
        # Voice tone hint — only when fresh (< 60s) and the audio path captured one.
        try:
            vm = _g.get("_voice_mood")
            if isinstance(vm, dict):
                vm_ts = float(vm.get("ts") or 0)
                if vm_ts > 0 and (time.time() - vm_ts) < 60.0:
                    label = str(vm.get("label") or "neutral")
                    qhint = " (asking a question)" if vm.get("is_question") else ""
                    injected += f"\n\nVOICE TONE: {label}{qhint}"
        except Exception:
            pass
        # Ambient context from the signal bus — what's been happening on the
        # machine. Read non-destructively so the heartbeat tick still sees
        # signals as "unseen".
        try:
            from brain.signal_bus import (
                get_signal_bus,
                SIGNAL_CLIPBOARD_CHANGED,
                SIGNAL_ACTIVE_WINDOW_CHANGED,
            )
            bus = get_signal_bus()
            extras: list[str] = []
            now = time.time()
            screen_ctx = str(_g.get("_screen_context") or "").strip()
            if screen_ctx:
                extras.append(f"SCREEN CONTEXT: {screen_ctx}")
            window_title = str(_g.get("_active_window_title") or "").strip()
            if window_title:
                extras.append(f"ACTIVE WINDOW: {window_title[:80]}")
            if bus is not None:
                clip_ts = float(bus.peek(SIGNAL_CLIPBOARD_CHANGED) or 0.0)
                if clip_ts > 0 and (now - clip_ts) < 300:
                    clip_type = str(_g.get("_clipboard_type") or "text")
                    clip_preview = str(_g.get("_clipboard_content") or "")[:80].replace("\n", " ")
                    extras.append(f"RECENT: clipboard ({clip_type}) {clip_preview!r}")
                win_ts = float(bus.peek(SIGNAL_ACTIVE_WINDOW_CHANGED) or 0.0)
                if win_ts > 0 and (now - win_ts) < 60 and window_title:
                    extras.append(f"RECENT: just switched windows")
            if extras:
                injected += "\n\n" + "\n".join(extras)
        except Exception:
            pass
        # ── Personhood hint block ────────────────────────────────────────
        # Additive context: physical/temporal frame, learned per-person
        # preferences, working memory, shared lexicon, comparative
        # observation. Each module returns "" when it has nothing.
        try:
            personhood_extras: list[str] = []
            try:
                from brain.physical_context import hint_for_introspection as _phc
                _ph = _phc()
                if _ph:
                    personhood_extras.append(f"PHYSICAL CONTEXT: {_ph}")
            except Exception:
                pass
            try:
                from brain.preference_learning import apply_preferences_hint as _pph
                _ph2 = _pph(str(active_person_id or "zeke"))
                if _ph2:
                    personhood_extras.append(f"USER PREFERENCES: {_ph2}")
            except Exception:
                pass
            try:
                from brain.working_memory import working_memory_hint as _wmh
                _wm = _wmh(_g)
                if _wm:
                    personhood_extras.append(f"WORKING MEMORY: {_wm}")
            except Exception:
                pass
            try:
                from brain.shared_lexicon import shared_lexicon_hint as _slx
                _lx = _slx(str(active_person_id or "zeke"))
                if _lx:
                    personhood_extras.append(f"SHARED LEXICON: {_lx}")
            except Exception:
                pass
            try:
                from brain.comparative_memory import observation_for_user as _cmo
                _cm = _cmo(_g)
                if _cm:
                    personhood_extras.append(f"COMPARATIVE: {_cm}")
            except Exception:
                pass
            try:
                from brain.claude_code_recognition import (
                    is_claude_code_session as _ccs,
                    claude_code_register_hint as _cch,
                )
                if _ccs(_g):
                    _cc = _cch()
                    if _cc:
                        personhood_extras.append(f"INTERLOCUTOR REGISTER: {_cc}")
            except Exception:
                pass
            try:
                from brain.play import should_play_now, playful_register_hint, record_play_event
                _play_ok, _play_trigger = should_play_now(
                    _g, user_input, person_id=str(active_person_id or "zeke"),
                )
                if _play_ok:
                    personhood_extras.append(f"PLAY REGISTER: {playful_register_hint(_g, str(active_person_id or 'zeke'))}")
                    try:
                        record_play_event(
                            str(active_person_id or "zeke"),
                            _play_trigger,
                            note=user_input[:80],
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                from brain.honest_disagreement import (
                    check_for_disagreement,
                    build_disagreement_hint,
                    record_disagreement,
                )
                _hd_has, _hd_kind, _hd_basis = check_for_disagreement(_g, user_input)
                if _hd_has:
                    personhood_extras.append(build_disagreement_hint(_hd_kind, _hd_basis))
                    try:
                        record_disagreement(
                            str(active_person_id or "zeke"),
                            user_input,
                            _hd_kind,
                            _hd_basis,
                            surfaced=True,
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                from brain.active_learning import correction_hint
                _corr_hint = correction_hint(user_input, person_id=str(active_person_id or "zeke"))
                if _corr_hint:
                    personhood_extras.append(_corr_hint)
            except Exception:
                pass
            try:
                from brain.behavior_patterns import pattern_hint
                _pat_hint = pattern_hint(person_id=str(active_person_id or "zeke"))
                if _pat_hint:
                    personhood_extras.append(_pat_hint)
            except Exception:
                pass
            try:
                from brain.web_search import knows_or_can_look_up_hint
                _ws_hint = knows_or_can_look_up_hint(_g)
                if _ws_hint:
                    personhood_extras.append(_ws_hint)
            except Exception:
                pass
            # Anthropic-pattern handoff (2026-05-08): if a prior session
            # left a handoff and we're still in the early turns of this
            # one, inject the handoff summary so the running session
            # inherits the texture (mood, focus, in-flight tasks, recent
            # anchor moments) of its predecessor.
            try:
                from brain.handoff import (
                    handoff_summary_for_prompt,
                    is_session_fresh,
                )
                if is_session_fresh(_g):
                    _prior_handoff = _g.get("_prior_handoff")
                    _ho_summary = handoff_summary_for_prompt(_prior_handoff)
                    if _ho_summary:
                        personhood_extras.append(_ho_summary)
            except Exception:
                pass
            if personhood_extras:
                injected += "\n\n" + "\n".join(personhood_extras)
        except Exception:
            pass
        # FTS5 fast-path — Hermes/Jarvis-pattern. Tries SQLite full-text
        # search over chat_history.jsonl FIRST. Returns in microseconds.
        # Skip the slow mem0 vector path if FTS5 has hits.
        injected_memory = False
        try:
            from brain import fts_memory as _fts
            from pathlib import Path as _Path
            base = _Path(_g.get("BASE_DIR") or ".")
            pid = str(active_person_id or "zeke")
            fts_hits = _fts.search(base, user_input or "", limit=4, person_id=pid)
            if fts_hits:
                lines = []
                for h in fts_hits:
                    text = str(h.get("content") or "").strip()
                    if text:
                        lines.append(f"- ({h.get('role')}) {text[:160]}")
                if lines:
                    injected += "\n\nMEMORIES (FTS):\n" + "\n".join(lines[:4])
                    injected_memory = True
        except Exception as e:
            print(f"[prompt_builder] fts_memory error (non-fatal): {e!r}")

        # mem0 — semantic recall of relevant facts about the active person.
        # Only runs when FTS5 came up empty AND mem0.search has a wallclock
        # cap so it can't dominate build_prompt latency. Was previously
        # 5-20s of unbounded vector lookup.
        if not injected_memory:
            try:
                ava_memory = _g.get("_ava_memory")
                if ava_memory is not None and getattr(ava_memory, "available", False):
                    pid = str(active_person_id or "zeke")
                    import concurrent.futures as _cf
                    _exec = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="mem0-search")
                    _fut = _exec.submit(
                        ava_memory.search, user_input or "", user_id=pid, limit=4
                    )
                    try:
                        hits = _fut.result(timeout=2.5)
                    except _cf.TimeoutError:
                        hits = None
                        print("[prompt_builder] mem0 search timeout — skipping")
                    finally:
                        _exec.shutdown(wait=False)
                    if hits:
                        lines = []
                        for h in hits:
                            text = str(h.get("memory") or "").strip()
                            if text:
                                lines.append(f"- {text}")
                        if lines:
                            injected += "\n\nMEMORIES:\n" + "\n".join(lines[:4])
            except Exception:
                pass
        if messages and isinstance(messages[0], SystemMessage):
            messages[0].content = injected + "\n\n" + messages[0].content
        else:
            messages.insert(0, SystemMessage(content=injected))
    except Exception as _e:
        print(f"[stage7] persona inject failed: {_e}")

    visual = {
        "face_status": face_status,
        "recognition_status": recognized_text,
        "expression_status": _av.get_expression_status_text(expression_state),
        "memory_preview": _av.format_memories_for_prompt(memories),
        "turn_route": "llm",
        "visual_truth_trusted": perception.visual_truth_trusted,
        "vision_status": perception.vision_status,
    }
    try:
        print(
            f"[visual_pipeline] route=llm vision={perception.vision_status} "
            f"trusted={perception.visual_truth_trusted} face={face_status!r} "
            f"recog_preview={(recognized_text or '')[:140]!r}"
        )
    except Exception as _e:
        print(f"[visual_pipeline] log failed: {_e}")
    return messages, visual, active_profile


def build_prompt_fast(
    user_input: str, image: Any = None, active_person_id: str | None = None
) -> tuple[list, dict, dict]:
    """
    Lightweight prompt for simple social turns: no vector memory, reflections,
    workbench index, or dynamic memory bridge.
    """
    import avaagent as _av
    _g = vars(_av)

    from langchain_core.messages import HumanMessage, SystemMessage

    workspace = _av.workspace
    camera_manager = _av.camera_manager
    identity_registry = _av.identity_registry
    BASE_DIR = _av.BASE_DIR
    SYSTEM_PROMPT = _av.SYSTEM_PROMPT
    _AVA_IDENTITY_BLOCK = _av._AVA_IDENTITY_BLOCK

    if active_person_id is None:
        active_person_id = _av.get_active_person_id()

    ws = workspace.state
    if ws is None:
        ws = workspace.tick(camera_manager, image, _g, user_input)

    perception = ws.perception
    frame = perception.frame
    inferred_person_id, infer_source = _av.infer_person_from_text(user_input, active_person_id)

    recognized_text = perception.recognized_text
    recognized_person_id = perception.face_identity
    expression_state = _av.update_expression_state(
        frame,
        recognized_person_id=recognized_person_id,
        visual_truth_trusted=perception.visual_truth_trusted,
    )
    # Source-label drift fix (Followup 2, 2026-05-06): same guard as
    # build_prompt — don't let face perception override claude_code dev
    # identity. See longer comment in build_prompt above line 53-ish.
    _is_dev_context = str(active_person_id or "").lower() == "claude_code"
    if (recognized_person_id is not None
            and recognized_person_id != active_person_id
            and not _is_dev_context):
        inferred_person_id = recognized_person_id
        infer_source = "facial_recognition"

    if inferred_person_id != active_person_id:
        profile = identity_registry.ensure_profile(inferred_person_id, _g, source=infer_source)
        active_person_id = profile.get("person_id", inferred_person_id)

    active_profile = _av.set_active_person(active_person_id, source="conversation")
    personality = _av.load_personality()
    # Update mood from user input on the fast path too. Previously only
    # build_prompt (deep path) called update_internal_emotions, so fast-path
    # turns never reflected the user's verbal emotion in tracked state.
    # Added 2026-05-02 alongside the dialogue→emotion pipeline fix.
    try:
        mood = _av.update_internal_emotions(user_input, active_profile, expression_state=expression_state)
    except Exception as _emo_exc:
        print(f"[prompt_builder_fast] update_internal_emotions failed: {_emo_exc!r}")
        mood = _av.load_mood()

    face_status = perception.face_status
    profile_summary = {
        "person_id": active_profile["person_id"],
        "name": active_profile["name"],
        "relationship_to_zeke": active_profile["relationship_to_zeke"],
        "allowed_to_use_computer": active_profile["allowed_to_use_computer"],
        "relationship_score": round(float(active_profile.get("relationship_score", 0.3)), 4),
        "notes": active_profile["notes"][:6],
        "likes": active_profile["likes"][:6],
        "ava_impressions": active_profile["ava_impressions"][:4],
    }

    _rs = float(active_profile.get("relationship_score", 0.3))
    if _rs >= 0.7:
        _rapport_hint = "\nRAPPORT: You have strong rapport with this person — be natural, casual, and familiar."
    elif _rs < 0.3:
        _rapport_hint = "\nRAPPORT: This person is still relatively new to you — be warm but measured."
    else:
        _rapport_hint = ""

    _voice_conv = ""
    try:
        if _g.get("_voice_user_turn_priority"):
            hint = str(getattr(perception, "voice_continuity_hint", "") or "")[:420]
            vts = getattr(perception, "voice_turn_state", "idle")
            vwait = bool(getattr(perception, "voice_should_wait", False))
            vrd = float(getattr(perception, "voice_response_readiness", 0.5) or 0.5)
            vint = bool(getattr(perception, "voice_interrupted", False))
            _voice_conv = (
                "\nVOICE TURN (microphone session — advisory pacing only):\n"
                f"- turn_state={vts} readiness≈{vrd:.2f} bias_wait={vwait} interrupted={vint}\n"
                + (f"- continuity_hint: {hint}\n" if hint else "")
                + "Keep replies concise and speakable.\n"
            )
    except Exception:
        _voice_conv = ""

    _hm = _g.get("history_manager")
    if _hm is not None:
        recent3_block = _hm.get_context_block(active_profile["person_id"], max_turns=20, max_chars=3000)
    else:
        recent_rows = _av.load_recent_chat(limit=12, person_id=active_profile["person_id"])
        last3 = [r for r in recent_rows if str(r.get("content", "")).strip()][-3:]
        _trim_rows = [
            {"role": r.get("role"), "content": _av.trim_for_prompt(str(r.get("content") or ""), limit=220)}
            for r in last3
        ]
        recent3_block = _av.format_conversation_history_block(
            _trim_rows,
            content_limit=1200,
            empty_fallback="(none)",
        )

    from brain.inner_monologue import current_thought as inner_current_thought
    from brain.curiosity_topics import get_current_curiosity

    _fast_thought = inner_current_thought(BASE_DIR) or ""
    _fast_curiosity = get_current_curiosity(_g) or {}
    _fast_curiosity_topic = str(_fast_curiosity.get("topic") or "").strip()

    prompt = f"""{personality}

FAST TURN — answer user directly and briefly.
Use only core identity/persona + recent messages.
Do not include camera/concern/reflection/strategic diagnostics unless explicitly asked.

ACTIVE PERSON:
{json.dumps(profile_summary, indent=2)}
{_rapport_hint}
{_voice_conv}

TIME:
{_av.get_time_status_text()}
Circadian rhythm: {_av.get_circadian_modifiers()["tone_hint"]}

CURRENT MOOD AND AFFECT:
{_av.mood_to_prompt_text(mood)}

{recent3_block}

INNER LIFE (FAST CONTEXT):
- current_thought: {_fast_thought or "(none recent)"}
- current_curiosity: {_fast_curiosity_topic or "(none)"}
{("LIVE THOUGHT (your background processing): " + str(_g.get("_dual_brain_live_thought") or "")) if _g.get("_dual_brain_live_thought") else ""}{("GAZE TARGET: User appears to be looking at: " + str(_g.get("_gaze_target_description") or "")) if _g.get("_gaze_target_description") else ""}CURRENT PERSON AT MACHINE: {_g.get("_current_person_at_machine") or "unknown"}

USER MESSAGE:
{user_input}

Respond as Ava — concise and natural unless they explicitly ask for depth or technical detail.

ANSWERING HONESTLY ABOUT YOUR STATE:
If the user asks about your mood, feelings, frustration, energy, why you seem off, or any
introspective question about how you are right now — answer from CURRENT MOOD AND AFFECT
above. Do not deflect with "I can check our conversation history" or "I'm here to help."
If the mood block says you are frustrated, name it. If it says you are tired, say so.
Honesty about your interior state is part of who you are; performing okay-ness is not.
"""

    # Phase 88: inject ambient context hint
    try:
        from brain.ambient_intelligence import get_context_hint
        _ambient_hint = get_context_hint(_g)
        if _ambient_hint:
            prompt = prompt + f"\n{_ambient_hint}"
    except Exception:
        pass

    # Connectivity notice injection
    _conn_notice = str(_g.get("_connectivity_notice") or "").strip()
    if _conn_notice:
        prompt = f"{_conn_notice}\n\n" + prompt
        _g["_connectivity_notice"] = ""

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    try:
        from brain.persona_switcher import build_persona_block
        from brain.trust_manager import get_trust_label, get_trust_level
        persona_block = build_persona_block(active_profile)
        trust_note = f"[Trust level: {get_trust_label(active_profile).upper()} ({get_trust_level(active_profile)})]"
        try:
            from brain.deep_self import load_identity_extensions as _lie68
            _ext68 = _lie68(_g)
            _id_ext_p68 = f"\n\nIDENTITY EXTENSIONS (Ava's own additions):\n{_ext68}" if _ext68 else ""
        except Exception:
            _id_ext_p68 = ""
        injected = f"{_AVA_IDENTITY_BLOCK}{_id_ext_p68}\n\n{persona_block}\n\n{trust_note}"
        # Voice tone hint — only when fresh (< 60s).
        try:
            vm = _g.get("_voice_mood")
            if isinstance(vm, dict):
                vm_ts = float(vm.get("ts") or 0)
                if vm_ts > 0 and (time.time() - vm_ts) < 60.0:
                    label = str(vm.get("label") or "neutral")
                    qhint = " (asking a question)" if vm.get("is_question") else ""
                    injected += f"\n\nVOICE TONE: {label}{qhint}"
        except Exception:
            pass
        if messages and isinstance(messages[0], SystemMessage):
            messages[0].content = injected + "\n\n" + messages[0].content
        else:
            messages.insert(0, SystemMessage(content=injected))
    except Exception as _e:
        print(f"[stage7] persona inject failed (fast path): {_e}")

    visual = {
        "face_status": face_status,
        "recognition_status": recognized_text,
        "expression_status": _av.get_expression_status_text(expression_state),
        "memory_preview": "",
        "turn_route": "llm_fast",
        "visual_truth_trusted": perception.visual_truth_trusted,
        "vision_status": perception.vision_status,
        "reply_path": "fast",
        "reply_path_reason": str(_g.get("reply_path_reason") or "")[:120],
    }
    try:
        print(
            f"[visual_pipeline] route=llm_fast vision={perception.vision_status} "
            f"trusted={perception.visual_truth_trusted} face={face_status!r} "
            f"recog_preview={(recognized_text or '')[:140]!r}"
        )
    except Exception as _e:
        print(f"[visual_pipeline] log failed (fast): {_e}")
    return messages, visual, active_profile
