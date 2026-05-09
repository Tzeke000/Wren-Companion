"""
brain/turn_handler.py — Post-reply turn finalization.

finalize_ava_turn extracted from avaagent.py.
Handles logging, memory, episodic memory, TTS, self-critique, concept graph,
relationship update, and all other post-reply hooks.
"""
from __future__ import annotations

import re
import time
from typing import Any


def finalize_ava_turn(
    user_input: str,
    ai_reply: str,
    visual: dict,
    active_profile: dict,
    actions: list[str],
    *,
    turn_route: str | None = None,
) -> tuple[str, dict, dict, list[str], dict]:
    import avaagent as _av
    _g = vars(_av)
    from brain.turn_visual import normalize_visual_payload

    person_id = active_profile["person_id"]

    try:
        from brain.person_cadence import update_cadence_on_visit
        prof = dict(_av.load_profile_by_id(person_id))
        prof = update_cadence_on_visit(prof)
        _av.save_profile(prof)
        active_profile["cadence"] = prof.get("cadence")
        active_profile["last_activity_at"] = prof.get("last_activity_at")
        active_profile["threads"] = prof.get("threads", [])
    except Exception as e:
        print(f"[cadence] update failed: {e}")

    try:
        from brain.profile_store import touch_last_seen
        touch_last_seen(person_id, topic=user_input)
    except Exception:
        pass

    print(f"[turn_handler] DIAG entered finalize_ava_turn person={person_id} reply_chars={len(ai_reply or '')}", flush=True)
    try:
        _av.log_chat("user", user_input, {"person_id": person_id, "person_name": active_profile.get("name", person_id)})
        _av.log_chat("assistant", ai_reply, {"person_id": person_id, "person_name": active_profile.get("name", person_id), "actions": actions})
        print(f"[turn_handler] DIAG log_chat OK", flush=True)
    except Exception as _lc_e:
        print(f"[turn_handler] DIAG log_chat FAILED: {_lc_e!r}", flush=True)

    # 2026-05-07: maybe_autoremember calls enrich_memory_metadata_llm
    # synchronously, which spawns a 30-60s local LLM call for any
    # enrich-worthy turn. This blocks finalize_ava_turn's return and
    # therefore blocks the HTTP caller (inject_transcript). Move to
    # a background thread so the reply path returns ASAP. The memory
    # write still happens; it just doesn't block conversation latency.
    try:
        import threading as _th_mar
        _th_mar.Thread(
            target=_av.maybe_autoremember,
            args=(user_input, ai_reply, person_id),
            daemon=True,
            name="maybe-autoremember",
        ).start()
    except Exception as _mar_e:
        # Fall back to synchronous if thread spawn fails (very unlikely).
        try:
            _av.maybe_autoremember(user_input, ai_reply, person_id)
        except Exception as _mar_e2:
            print(f"[turn_handler] maybe_autoremember failed: {_mar_e2!r}")

    # Reflect Ava's verbalized emotion back into her tracked mood. Closes the
    # dialogue→emotion gap surfaced 2026-05-02 (Ava could say "frustrated"
    # while the orb showed 80-90% calm because nothing analyzed her own
    # reply text). Best-effort; swallow errors so a mood update bug never
    # breaks turn finalization.
    try:
        _av.update_internal_emotions_from_reply(ai_reply)
    except Exception as _emo_exc:
        print(f"[turn_handler] reply-emotion update failed: {_emo_exc!r}")

    # Persist turn to state/chat_history.jsonl so the UI can hydrate across restarts.
    # Bug 0.2 fix (2026-05-02): also stamp source labels (zeke / claude_code /
    # ava_response) so every entry is auditable and inner monologue can never
    # be confused with user input.
    try:
        import json as _json
        from pathlib import Path as _Path
        _hist_path = _Path(_av.BASE_DIR) / "state" / "chat_history.jsonl"
        _hist_path.parent.mkdir(parents=True, exist_ok=True)
        _now = time.time()
        try:
            _emo = str(_av.load_mood().get("current_mood") or "neutral")
        except Exception:
            _emo = "neutral"
        _used_model = str(_g.get("_last_invoked_model") or _av.LLM_MODEL or "")
        # Refuse to persist 💭-prefixed content to chat history — same
        # isolation rule as log_chat. If somehow leaked here, drop it loud.
        _user_safe = str(user_input or "")
        _reply_safe = str(ai_reply or "")
        if "💭" in _user_safe or "💭" in _reply_safe:
            print("[chat_history] REFUSED: 💭-prefixed content blocked from chat_history (inner monologue isolation)")
            raise RuntimeError("inner monologue text leaked into chat_history persist path")
        # Resolve source. person_id is already populated by reply_engine's
        # active-profile flow; "claude_code" is the dev-injection profile.
        _user_source = _av._resolve_chat_source("user", {"person_id": person_id})
        _ava_source = _av._resolve_chat_source("assistant", {})
        with _hist_path.open("a", encoding="utf-8") as _f:
            _f.write(_json.dumps({
                "ts": _now, "role": "user", "source": _user_source,
                "content": _user_safe,
                "person_id": person_id, "person_name": active_profile.get("name", ""),
            }, ensure_ascii=False) + "\n")
            _f.write(_json.dumps({
                "ts": _now, "role": "assistant", "source": _ava_source,
                "content": _reply_safe,
                "person_id": person_id, "person_name": active_profile.get("name", ""),
                "model": _used_model, "emotion": _emo,
                "turn_route": str(turn_route or ""),
            }, ensure_ascii=False) + "\n")
        print(f"[turn_handler] DIAG chat_history.jsonl wrote OK path={_hist_path}", flush=True)
    except Exception as _e:
        print(f"[chat_history] persist failed: {_e}", flush=True)
        import traceback as _tb_ch; print(_tb_ch.format_exc(), flush=True)

    # mem0 fact extraction. Runs in a background thread because mem0 calls
    # the LLM to decide what's worth remembering — we don't want to block
    # finalize_ava_turn waiting for that. Best-effort; errors are swallowed.
    try:
        _ava_memory = _g.get("_ava_memory")
        if _ava_memory is not None and getattr(_ava_memory, "available", False):
            import threading as _t
            def _bg_mem():
                try:
                    _ava_memory.add_conversation_turn(
                        user_text=str(user_input or ""),
                        ava_text=str(ai_reply or ""),
                        user_id=str(person_id or "zeke"),
                    )
                except Exception as _me:
                    print(f"[ava_memory] add_conversation_turn error: {_me}")
            _t.Thread(target=_bg_mem, daemon=True, name="ava-memory-add").start()
    except Exception as _me:
        print(f"[ava_memory] dispatch error: {_me}")

    try:
        from brain.event_extractor import maybe_extract_prospective_events
        st = _av.load_session_state()
        turn = int(st.get("total_message_count", 0) or 0) + 1
        maybe_extract_prospective_events(user_input, person_id, _g, source_turn=turn)
    except Exception as e:
        print(f"[prospective] extract failed: {e}")

    reflection = _av.reflect_on_last_reply(user_input, ai_reply, person_id, actions=actions)
    OWNER_PERSON_ID = _av.OWNER_PERSON_ID
    if person_id == OWNER_PERSON_ID:
        summary = (reflection or {}).get("summary") or ""
        importance = float((reflection or {}).get("importance", 0.0))
        if summary and importance >= 0.72:
            try:
                from brain.identity_loader import append_to_user_file
                append_to_user_file(summary)
            except Exception:
                pass

    canon = list(_av._get_canonical_history())
    canon.append({"role": "assistant", "content": ai_reply})
    _av._set_canonical_history(canon)
    _av._maybe_update_relationship_on_turn(active_profile)

    try:
        _av.refresh_self_model_pending_threads(person_id)
    except Exception:
        pass

    try:
        sess = _av.load_session_state()
        n = int(sess.get("total_message_count", 0) or 0) + 1
        if n > 0 and n % 10 == 0:
            _av._trigger_narrative_update_async()
    except Exception:
        pass

    # 2026-05-07 latency fix: _update_concept_graph_from_turn calls
    # _extract_concepts_with_mistral (a 30-60s mistral:7b LLM call) for
    # every turn. Same blocking pattern as maybe_autoremember. Move to
    # background thread so the reply path returns ASAP.
    try:
        import threading as _th_cg
        _th_cg.Thread(
            target=_av._update_concept_graph_from_turn,
            args=(user_input, ai_reply),
            daemon=True,
            name="concept-graph-update",
        ).start()
    except Exception:
        pass

    # Phase 64: episodic memory
    try:
        BASE_DIR = _av.BASE_DIR
        _ep_importance = float((reflection or {}).get("importance", 0.4))
        _ep_topic = _av._extract_simple_topic(user_input) or "conversation"
        _ep_summary = f"Zeke: {user_input[:150]} | Ava: {ai_reply[:200]}"
        _ep_emotion = str(_av.load_mood().get("current_mood") or "neutral")
        from brain.episodic_memory import get_episodic_memory
        get_episodic_memory(BASE_DIR).store_episode(
            topic=_ep_topic,
            summary=_ep_summary,
            emotional_context=_ep_emotion,
            importance=_ep_importance,
            people_present=[person_id],
            novelty=0.5,
        )
    except Exception:
        pass

    try:
        from brain.deep_self import update_mind_model_async
        update_mind_model_async(user_input, ai_reply[:1200], _g)
    except Exception:
        pass

    try:
        from brain.deep_self import self_critique_async
        self_critique_async(ai_reply, user_input, str(actions), _g)
    except Exception:
        pass

    try:
        from brain.deep_self import check_repair_needed
        global _last_repair_check_ts
        now = time.time()
        if now - float(_g.get("_last_repair_check_ts") or 0.0) >= 2 * 3600:
            _g["_last_repair_check_ts"] = now
            check_repair_needed(_g)
    except Exception:
        pass

    visual_out = normalize_visual_payload(visual, turn_route=turn_route)
    try:
        _fs = str(visual_out.get("face_status", ""))[:80]
        print(
            f"[run_ava] finalize route={visual_out.get('turn_route')} "
            f"reply_len={len(ai_reply or '')} actions={len(actions)} face_line={_fs!r}"
        )
    except Exception:
        pass

    # TTS speak — prefer COM-safe worker directly so we can use emotion-aware
    # voice modulation, fall back to tts_engine.
    _tts_spoke = False
    try:
        _tts_enabled = bool(_g.get("tts_enabled", False))
        if _tts_enabled:
            _reply_text = str(ai_reply or "")
            _clean = re.sub(r"[*_`#\[\]()]", "", _reply_text)
            _clean = re.sub(r"\s+", " ", _clean).strip()
            if len(_clean) > 300:
                _clean = _clean[:300].rstrip()
            if _clean and re.search(r"[A-Za-z0-9]", _clean):
                _worker = _g.get("_tts_worker")
                if _worker is not None and getattr(_worker, "available", False):
                    try:
                        _emotion = "neutral"
                        _intensity = 0.5
                        try:
                            _mood = _av.load_mood() or {}
                            _emotion = str(_mood.get("current_mood") or _mood.get("primary_emotion") or "neutral")
                            _intensity = float(_mood.get("energy") or _mood.get("intensity") or 0.5)
                        except Exception:
                            pass
                        _worker.speak_with_emotion(_clean, emotion=_emotion, intensity=_intensity, blocking=False)
                        _tts_spoke = True
                    except Exception:
                        _tts_spoke = False
                if not _tts_spoke:
                    _tts = _g.get("tts_engine")
                    if _tts is not None and callable(getattr(_tts, "is_available", None)) and _tts.is_available():
                        _tts.speak(_clean, blocking=False)
                        _tts_spoke = True
    except Exception:
        pass

    # Phase 87: voice style adaptation — called after TTS turn
    if _tts_spoke:
        try:
            from brain.tts_engine import voice_style_adapt
            _pos = len(str(user_input)) > 5  # simple positive signal: user sent meaningful input
            voice_style_adapt(_pos, _g)
        except Exception:
            pass

    # Phase 91: relationship memory depth — record emotion + conversation theme
    try:
        from pathlib import Path as _Path91
        from brain.relationship_model import record_emotion_with_person, record_conversation_theme
        _base91 = _Path91(_g.get("BASE_DIR") or ".")
        _mood91 = str((_g.get("_current_mood") or {}).get("current_mood") or "neutral")
        record_emotion_with_person(_base91, person_id, _mood91)
        _topic91 = _av._extract_simple_topic(user_input)
        if _topic91:
            record_conversation_theme(_base91, person_id, _topic91)
    except Exception:
        pass

    # ── Memory reflection scoring (Phase 2 step 4) ─────────────────────
    # Fire post-turn LLM scorer in a daemon thread. Examines retrieved
    # memories vs the final reply, logs scores to
    # state/memory_reflection_log.jsonl. Does NOT modify node levels —
    # that's step 5. This commit is data-gathering only.
    # Gated by AVA_REFLECTION_DISABLED to allow opt-out.
    try:
        from brain.memory_reflection import run_in_background as _reflect_bg
        _reflect_bg(
            _g, str(user_input or ""), str(ai_reply or ""),
            person_id=person_id, turn_id=f"turn_{int(time.time() * 1000)}",
        )
    except Exception as _re:
        print(f"[memory_reflection] hook failed (non-fatal): {_re!r}")

    # 2026-05-08: bump per-session turn counter (used by handoff
    # is_session_fresh — we stop injecting the prior-session handoff
    # summary into the system prompt after a few real turns since the
    # running conversation has its own context by then).
    try:
        _g["_turns_this_session"] = int(_g.get("_turns_this_session") or 0) + 1
    except Exception:
        pass

    # B5: theory-of-mind topic tracking.
    try:
        from brain.theory_of_mind import post_turn_record
        post_turn_record(str(person_id or "zeke"), str(ai_reply or ""))
    except Exception:
        pass

    # C12: auto-tag confidential disclosures from the user's input.
    try:
        from brain.discretion import auto_tag_from_user_input
        auto_tag_from_user_input(str(person_id or "zeke"), str(user_input or ""))
    except Exception:
        pass

    # B1: capture factual/process corrections.
    try:
        from brain.active_learning import auto_capture_from_turn
        auto_capture_from_turn(_g, str(person_id or "zeke"), str(user_input or ""))
    except Exception:
        pass

    # D14: snapshot mood for comparative memory.
    try:
        from brain.comparative_memory import snapshot_mood
        snapshot_mood(_g, person_id=str(person_id or "zeke"))
    except Exception:
        pass

    # D16: auto-detect anchor moments in this turn.
    try:
        from brain.anchor_moments import auto_detect_anchor_in_turn
        auto_detect_anchor_in_turn(
            str(person_id or "zeke"),
            str(user_input or ""),
            str(ai_reply or ""),
        )
    except Exception:
        pass

    return ai_reply, visual_out, active_profile, actions, reflection
