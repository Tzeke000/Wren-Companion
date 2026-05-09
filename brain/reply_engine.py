"""
brain/reply_engine.py — Main Ava response pipeline.

run_ava extracted from avaagent.py. Handles routing, model selection,
reply guards, tool execution, and delegates to turn_handler for finalization.
"""
from __future__ import annotations

import concurrent.futures
import time
import uuid
from pathlib import Path
from typing import Any

_RUN_AVA_TUNING_SOURCE_LOGGED = False
_PROMPT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="ava-prompt")


def _trace(label: str) -> None:  # TRACE-PHASE1
    """Timestamped diagnostic trace for the run_ava path. Removed/gated in Phase 3."""  # TRACE-PHASE1
    ts = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"  # TRACE-PHASE1
    print(f"[trace] {ts} {label}")  # TRACE-PHASE1

# Simple greetings / mood checks / quick factual questions — bypass full
# pipeline for sub-5s response.
_SIMPLE_PATTERNS = (
    "how are you", "how do you feel", "what are you doing",
    "hey ava", "hello ava", "hi ava", "hello", "hi ", "hey ",
    "what's up", "whats up", "sup ava",
    "good morning", "good afternoon", "good evening", "good night",
    "how is your day", "how's it going", "hows it going",
    "what are you thinking", "you there", "you awake", "you up",
    # Time/date — these have voice_command_router builtins, but if the user
    # phrases it indirectly ("can you tell me what time"), the router may
    # miss; route to the fast-path so we never hit the deep LLM path.
    "what time", "what's the time", "whats the time", "what is the time",
    "what day", "what's the date", "what is the date", "what's today",
    # Quick "tell me" prompts that don't need memory or tools.
    "tell me a joke", "one sentence joke", "tell a joke",
    # Single-word probes
    "thanks", "thank you", "ok ava", "okay ava", "got it",
)


def _is_simple_question(text: str) -> bool:
    t = (text or "").lower().strip()
    if not t:
        return False
    if len(t.split()) > 15:
        return False
    # Strip trailing punctuation for matching
    t = t.rstrip("?!.,").strip()
    if t in ("hi", "hey", "hello", "yo"):
        return True
    return any(p in t for p in _SIMPLE_PATTERNS)


def _with_timeout(fn, timeout: float, fallback=None, label: str = ""):
    """Run fn() in a thread; return fallback if it exceeds timeout seconds."""
    try:
        fut = _PROMPT_EXECUTOR.submit(fn)
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        print(f"[run_ava] timeout ({timeout}s) in {label} — using fallback")
        return fallback
    except Exception as e:
        print(f"[run_ava] error in {label}: {e!r}")
        return fallback


def run_ava(
    user_input: str, image: Any = None, active_person_id: str | None = None
) -> tuple[str, dict, dict, list[str], dict]:
    _trace(f"re.run_ava.entered chars={len(user_input or '')}")  # TRACE-PHASE1
    global _RUN_AVA_TUNING_SOURCE_LOGGED
    _import_t0 = time.time()  # TRACE-PHASE1

    _t = time.time()  # TRACE-PHASE1
    _trace("re.import.avaagent.start")  # TRACE-PHASE1
    import avaagent as _av
    _trace(f"re.import.avaagent.done ms={int((time.time()-_t)*1000)}")  # TRACE-PHASE1
    _g = vars(_av)

    _t = time.time()  # TRACE-PHASE1
    _trace("re.import.prompt_builder.start")  # TRACE-PHASE1
    from brain.prompt_builder import build_prompt, build_prompt_fast
    _trace(f"re.import.prompt_builder.done ms={int((time.time()-_t)*1000)}")  # TRACE-PHASE1

    _t = time.time()  # TRACE-PHASE1
    _trace("re.import.turn_handler.start")  # TRACE-PHASE1
    from brain.turn_handler import finalize_ava_turn
    _trace(f"re.import.turn_handler.done ms={int((time.time()-_t)*1000)}")  # TRACE-PHASE1

    _t = time.time()  # TRACE-PHASE1
    _trace("re.import.langchain_ollama.start")  # TRACE-PHASE1
    from langchain_ollama import ChatOllama
    _trace(f"re.import.langchain_ollama.done ms={int((time.time()-_t)*1000)}")  # TRACE-PHASE1

    _t = time.time()  # TRACE-PHASE1
    _trace("re.import.turn_visual.start")  # TRACE-PHASE1
    from brain.turn_visual import default_visual_payload
    _trace(f"re.import.turn_visual.done ms={int((time.time()-_t)*1000)}")  # TRACE-PHASE1

    _t = time.time()  # TRACE-PHASE1
    _trace("re.import.output_guard.start")  # TRACE-PHASE1
    from brain.output_guard import scrub_visible_reply
    _trace(f"re.import.output_guard.done ms={int((time.time()-_t)*1000)}")  # TRACE-PHASE1

    _t = time.time()  # TRACE-PHASE1
    _trace("re.import.selfstate.start")  # TRACE-PHASE1
    from brain.selfstate import is_selfstate_query, build_selfstate_reply
    _trace(f"re.import.selfstate.done ms={int((time.time()-_t)*1000)}")  # TRACE-PHASE1

    _t = time.time()  # TRACE-PHASE1
    _trace("re.import.shutdown_ritual.start")  # TRACE-PHASE1
    from brain.shutdown_ritual import is_shutdown_trigger, run_shutdown_ritual
    _trace(f"re.import.shutdown_ritual.done ms={int((time.time()-_t)*1000)}")  # TRACE-PHASE1

    _trace(f"re.imports_done ms={int((time.time()-_import_t0)*1000)}")  # TRACE-PHASE1

    llm = _av.llm
    workspace = _av.workspace
    LLM_MODEL = _av.LLM_MODEL
    BASE_DIR = _av.BASE_DIR
    OWNER_PERSON_ID = _av.OWNER_PERSON_ID

    if not _RUN_AVA_TUNING_SOURCE_LOGGED:
        _RUN_AVA_TUNING_SOURCE_LOGGED = True
        print("[run_ava] tuning layer: config/ava_tuning.py")

    _person_t0 = time.time()  # TRACE-PHASE1
    active_person_id = active_person_id or _av.get_active_person_id()
    _trace(f"re.active_person_resolved ms={int((time.time()-_person_t0)*1000)}")  # TRACE-PHASE1
    _inp = (user_input or "").strip()

    # ── A6: Pipeline-stage telemetry ─────────────────────────────────────────
    # Each turn gets a telemetry record with timing per stage. Lets us
    # answer "what was slow about that turn?" without log-grepping.
    _turn_id = ""
    try:
        from brain.telemetry import telemetry as _telemetry
        _turn_source = str(_g.get("_wake_source") or "unknown")
        _turn_id = _telemetry.start_turn(
            input_text=_inp,
            source=_turn_source,
            person_id=str(active_person_id or ""),
        )
        _g["_telemetry_turn_id"] = _turn_id
        _telemetry.mark(_turn_id, "router_entry")
    except Exception as _te:
        print(f"[run_ava] telemetry start failed: {_te!r}")

    _g["_last_user_interaction_ts"] = time.time()
    _g["_last_user_message_ts"] = time.time()
    _g["_last_user_input"] = _inp

    # B6: scan user input for preference-correction signals + persist.
    # Runs every turn; non-matching inputs are no-ops.
    try:
        from brain.preference_learning import learn_from_correction
        n_prefs = learn_from_correction(str(active_person_id or "zeke"), _inp)
        if n_prefs > 0:
            print(f"[preference_learning] recorded {n_prefs} preferences from user input")
    except Exception as _ple:
        print(f"[preference_learning] error (non-fatal): {_ple!r}")

    # C11: scan for shared-lexicon definitions ("let's call X Y" etc).
    try:
        from brain.shared_lexicon import learn_from_text
        learned = learn_from_text(str(active_person_id or "zeke"), _inp)
        if learned:
            print(f"[shared_lexicon] learned term: {learned[0]!r}")
    except Exception as _sle:
        print(f"[shared_lexicon] error (non-fatal): {_sle!r}")

    # B7: conversational repair — if the user has corrected Ava 2+
    # times in a row, switch register to "let me back up — what did
    # you actually want?" Short-circuits the normal reply path with
    # an honest clarification question.
    try:
        from brain.conversation_repair import maybe_apply_repair, mark_success, is_correction
        repair_reply = maybe_apply_repair(_g, _inp)
        if repair_reply is not None:
            print(f"[conversation_repair] firing repair register")
            try:
                from brain.turn_visual import default_visual_payload as _rep_vis
                _vis_rep = _rep_vis(
                    face_status="—", recognition_status="—", expression_status="—",
                    memory_preview="", turn_route="conversation_repair",
                    visual_truth_trusted=False, vision_status="conversation_repair",
                )
                _profile_rep = _av.load_profile_by_id(active_person_id)
                _g["_ava_thinking"] = False
                return repair_reply, _vis_rep, _profile_rep, [], {"conversation_repair": True}
            except Exception:
                pass
        elif not is_correction(_inp):
            mark_success(_g)
    except Exception as _cre:
        print(f"[conversation_repair] error (non-fatal): {_cre!r}")
    _u_low = _inp.lower()
    _g["_desktop_tier3_approved"] = any(
        k in _u_low for k in ("yes do it", "go ahead", "yes, do it", "go ahead and do it")
    )

    _t_start = time.time()  # used to time every step

    def _elapsed() -> str:
        return f"{time.time()-_t_start:.2f}s"

    _trace(f"re.run_ava.start chars={len(_inp)}")  # TRACE-PHASE1
    print(f"[perf] run_ava start person={active_person_id} has_image={image is not None} input_chars={len(_inp)}")

    # ── VOICE COMMAND ROUTER (intercept before LLM) ──────────────────────────
    # Pattern-matches the input against built-in + custom commands. If it
    # matches, the action runs, the response is spoken via the TTS worker, the
    # turn is logged to chat_history, and we return without invoking the LLM.
    try:
        from brain.voice_commands import get_voice_command_router
        from brain.turn_visual import default_visual_payload
        from brain.output_guard import scrub_visible_reply
        _router = get_voice_command_router(BASE_DIR)
        if _router is not None and _inp:
            _handled, _response = _router.route(_inp, _g)
            if _handled and _response:
                print(f"[voice_commands] matched: {_inp[:60]!r} → {_response[:80]!r}")
                # Persist to canonical history + chat_history.jsonl so the UI
                # picks it up like any other turn.
                try:
                    _canon = list(_av._get_canonical_history())
                    _canon.append({"role": "user", "content": user_input})
                    _canon.append({"role": "assistant", "content": _response})
                    _av._set_canonical_history(_canon)
                except Exception:
                    pass
                try:
                    import json as _json
                    from pathlib import Path as _Path
                    _hp = _Path(BASE_DIR) / "state" / "chat_history.jsonl"
                    _hp.parent.mkdir(parents=True, exist_ok=True)
                    _now = time.time()
                    _emo = "neutral"
                    try:
                        _emo = str(_av.load_mood().get("current_mood") or "neutral")
                    except Exception:
                        pass
                    # Resolve source labels for chat_history (Bug 0.2 fix
                    # 2026-05-02). active_person_id is already set by the
                    # earlier active_profile resolution; voice command router
                    # responses are ava_response.
                    _user_source = _av._resolve_chat_source("user", {"person_id": active_person_id})
                    _ava_source = "ava_response"
                    if "💭" in str(user_input or "") or "💭" in str(_response or ""):
                        print("[chat_history] REFUSED: 💭 content blocked (voice_command path)")
                    else:
                        with _hp.open("a", encoding="utf-8") as _f:
                            _f.write(_json.dumps({
                                "ts": _now, "role": "user", "source": _user_source,
                                "content": user_input,
                                "person_id": active_person_id,
                            }, ensure_ascii=False) + "\n")
                            _f.write(_json.dumps({
                                "ts": _now, "role": "assistant", "source": _ava_source,
                                "content": _response,
                                "person_id": active_person_id,
                                "model": "voice_command_router", "emotion": _emo,
                                "turn_route": "voice_command",
                            }, ensure_ascii=False) + "\n")
                except Exception:
                    pass
                _profile_vc = _av.load_profile_by_id(active_person_id)
                _vis_vc = default_visual_payload(
                    face_status="—", recognition_status="—", expression_status="—",
                    memory_preview="", turn_route="voice_command",
                    visual_truth_trusted=False, vision_status="voice_command",
                )
                _g["_ava_thinking"] = False
                # Voice command replies are clean strings produced by handlers
                # — they don't contain unfinished LLM thought-tails or internal
                # MEMORY/GOAL/ACTION blocks that scrub_visible_reply targets.
                # Running them through the scrubber is unsafe: it strips short
                # final lines that lack terminal punctuation. The weather
                # handler's "Beaufort, SC: 55°F" got trimmed to empty and the
                # caller saw "I'm here." (Phase B retry 2026-05-05). Skip the
                # scrub for voice command responses; trust the handler.
                try:
                    from brain.telemetry import telemetry as _tm
                    _tm.mark(_turn_id, "voice_command_match")
                    _tm.end_turn(_turn_id, reply_chars=len(_response or ""), route="voice_command", ok=True)
                except Exception:
                    pass
                # B5: track topics told to this person.
                try:
                    from brain.theory_of_mind import post_turn_record
                    post_turn_record(str(active_person_id or "zeke"), _response)
                except Exception:
                    pass
                # D14: snapshot mood for comparative memory.
                try:
                    from brain.comparative_memory import snapshot_mood
                    snapshot_mood(_g, person_id=str(active_person_id or "zeke"))
                except Exception:
                    pass
                # D16: auto-detect anchor moments.
                try:
                    from brain.anchor_moments import auto_detect_anchor_in_turn
                    auto_detect_anchor_in_turn(
                        str(active_person_id or "zeke"),
                        user_input or "",
                        _response,
                    )
                except Exception:
                    pass
                return _response, _vis_vc, _profile_vc, [], {"voice_command": True}
    except Exception as _vce:
        print(f"[run_ava] voice_command router error (non-fatal): {_vce!r}")

    # ── ACTION-TAG ROUTER (Jarvis Pattern 1, fallback after regex misses) ────
    # When the regex voice_command_router doesn't match, ask the fast LLM to
    # emit action tags like [OPEN_APP:Notes][TYPE_TEXT:hello]. Dispatch each
    # through existing handlers and skip the slow deep-path build_prompt +
    # invoke. Catches compound commands ("open Notes and type X") and
    # phrasing variations ("could you launch Chrome for me") that the
    # strict regex misses. See brain/action_tag_router.py.
    _trace("re.action_tag.start")  # TRACE-PHASE1
    try:
        from brain.action_tag_router import route as _atr_route
        from brain.turn_visual import default_visual_payload as _atr_vis
        from brain.output_guard import scrub_visible_reply as _atr_scrub  # noqa: F401
        _at_handled, _at_response = _atr_route(_inp, _g)
        _trace(f"re.action_tag.done handled={_at_handled} resp_chars={len(_at_response or '')}")  # TRACE-PHASE1
        if _at_handled and _at_response:
            print(f"[action_tag] dispatched: {_inp[:60]!r} → {_at_response[:80]!r}")
            try:
                _canon = list(_av._get_canonical_history())
                _canon.append({"role": "user", "content": user_input})
                _canon.append({"role": "assistant", "content": _at_response})
                _av._set_canonical_history(_canon)
            except Exception:
                pass
            try:
                import json as _json2
                from pathlib import Path as _Path2
                _hp = _Path2(BASE_DIR) / "state" / "chat_history.jsonl"
                _hp.parent.mkdir(parents=True, exist_ok=True)
                _now = time.time()
                _user_source = _av._resolve_chat_source("user", {"person_id": active_person_id})
                with _hp.open("a", encoding="utf-8") as _f:
                    _f.write(_json2.dumps({
                        "ts": _now, "role": "user", "source": _user_source,
                        "content": user_input,
                        "person_id": active_person_id,
                    }, ensure_ascii=False) + "\n")
                    _f.write(_json2.dumps({
                        "ts": _now, "role": "assistant", "source": "ava_response",
                        "content": _at_response,
                        "person_id": active_person_id,
                        "model": "action_tag_router", "emotion": "neutral",
                        "turn_route": "action_tag",
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass
            _profile_at = _av.load_profile_by_id(active_person_id)
            _vis_at = _atr_vis(
                face_status="—", recognition_status="—", expression_status="—",
                memory_preview="", turn_route="action_tag",
                visual_truth_trusted=False, vision_status="action_tag",
            )
            _g["_ava_thinking"] = False
            # Same trust as voice_command path: handler-controlled string,
            # skip the LLM-thought-tail scrubber.
            try:
                from brain.telemetry import telemetry as _tm
                _tm.mark(_turn_id, "action_tag_match")
                _tm.end_turn(_turn_id, reply_chars=len(_at_response or ""), route="action_tag", ok=True)
            except Exception:
                pass
            try:
                from brain.theory_of_mind import post_turn_record
                post_turn_record(str(active_person_id or "zeke"), _at_response)
            except Exception:
                pass
            # D14: snapshot mood for comparative memory.
            try:
                from brain.comparative_memory import snapshot_mood
                snapshot_mood(_g, person_id=str(active_person_id or "zeke"))
            except Exception:
                pass
            # D16: auto-detect anchor moments.
            try:
                from brain.anchor_moments import auto_detect_anchor_in_turn
                auto_detect_anchor_in_turn(
                    str(active_person_id or "zeke"),
                    user_input or "",
                    _at_response,
                )
            except Exception:
                pass
            return _at_response, _vis_at, _profile_at, [], {"action_tag": True}
    except Exception as _ate:
        print(f"[run_ava] action_tag_router error (non-fatal): {_ate!r}")

    # ── KNOWLEDGE QUERY SUBAGENT (Hermes-pattern, 2026-05-05) ────────────────
    # For open-ended factual questions ("tell me about polar bears"), the
    # deep path takes 60-120s on this hardware — way past the harness/voice
    # tolerance. Delegate to a background thread + return an immediate
    # acknowledgement. Result is announced via TTS when ready and persisted
    # to chat_history. See brain/subagent.py.
    try:
        from brain.subagent import looks_like_knowledge_query, delegate as _sub_delegate
        from brain.turn_visual import default_visual_payload as _sub_vis
        if looks_like_knowledge_query(_inp):
            _ack = _sub_delegate(_inp, _g)
            print(f"[subagent] delegated knowledge query — ack={_ack!r}")
            try:
                _canon = list(_av._get_canonical_history())
                _canon.append({"role": "user", "content": user_input})
                _canon.append({"role": "assistant", "content": _ack})
                _av._set_canonical_history(_canon)
            except Exception:
                pass
            try:
                import json as _json3
                from pathlib import Path as _Path3
                _hp = _Path3(BASE_DIR) / "state" / "chat_history.jsonl"
                _hp.parent.mkdir(parents=True, exist_ok=True)
                _now = time.time()
                _user_source = _av._resolve_chat_source("user", {"person_id": active_person_id})
                with _hp.open("a", encoding="utf-8") as _f:
                    _f.write(_json3.dumps({
                        "ts": _now, "role": "user", "source": _user_source,
                        "content": user_input, "person_id": active_person_id,
                    }, ensure_ascii=False) + "\n")
                    _f.write(_json3.dumps({
                        "ts": _now, "role": "assistant", "source": "ava_response",
                        "content": _ack, "person_id": active_person_id,
                        "model": "subagent_ack", "emotion": "curiosity",
                        "turn_route": "subagent_ack",
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass
            _profile_sub = _av.load_profile_by_id(active_person_id)
            _vis_sub = _sub_vis(
                face_status="—", recognition_status="—", expression_status="—",
                memory_preview="", turn_route="subagent_ack",
                visual_truth_trusted=False, vision_status="subagent_ack",
            )
            _g["_ava_thinking"] = False
            try:
                from brain.telemetry import telemetry as _tm
                _tm.mark(_turn_id, "subagent_delegated")
                _tm.end_turn(_turn_id, reply_chars=len(_ack or ""), route="subagent_ack", ok=True)
            except Exception:
                pass
            return _ack, _vis_sub, _profile_sub, [], {"subagent_ack": True}
    except Exception as _se:
        print(f"[run_ava] subagent error (non-fatal): {_se!r}")

    # Set thinking flag — UI uses this to show fast blue pulse animation
    _g["_ava_thinking"] = True
    _g["_ava_thinking_since"] = time.time()

    # ── Step: dual-brain foreground signal ────────────────────────────────────
    print(f"[perf] pre-dual-brain {_elapsed()}")
    _db = None
    try:
        from brain.dual_brain import get_dual_brain
        _db = get_dual_brain(_g)
        if _db is not None:
            _db.mark_foreground_start()
            # Tell Stream B to drop what it's doing — Zeke is talking to us.
            # The Ollama lock will serialize anyway, but pausing Stream B first
            # frees the GPU faster for the foreground model.
            try:
                _db.pause_background_now()
            except Exception:
                pass
            _live = _db.get_live_thought()
            if _live:
                _g["_dual_brain_live_thought"] = _live
    except Exception:
        _db = None
    print(f"[perf] post-dual-brain {_elapsed()}")

    # ── Step: gaze-to-screen trigger check ───────────────────────────────────
    print("[run_ava] step: gaze trigger check")
    try:
        _gaze_triggers = ("look at", "see that", "that thing", "over there",
                          "right there", "this one", "that one")
        if any(t in _u_low for t in _gaze_triggers):
            from brain.eye_tracker import get_eye_tracker
            _et = get_eye_tracker()
            if _et is not None and _et.available:
                import threading as _gaze_thread
                def _async_gaze():
                    try:
                        _av.camera_manager.capture_gaze_target(_g)
                    except Exception:
                        pass
                _gaze_thread.Thread(target=_async_gaze, daemon=True, name="ava-gaze-capture").start()
    except Exception:
        pass

    try:
        # ── FAST PATH: simple greetings / mood checks ────────────────────────
        # Bypass workspace.tick, episodic search, concept graph, vector retrieval,
        # privacy scan, dual-brain, etc. Just identity + mood + last 2 messages
        # + LLM. Target: sub-5-second response for "hey ava" style inputs.
        if _is_simple_question(_inp):
            _trace("re.run_ava.fast_path_entered")  # TRACE-PHASE1
            try:
                from brain.telemetry import telemetry as _tm_fp
                _tm_fp.mark(_turn_id, "fast_path_entered")
            except Exception:
                pass
            try:
                _g["_inner_state_line"] = "thinking — fast path"
            except Exception:
                pass
            print(f"[run_ava] FAST PATH: simple question (t={time.time()-_t_start:.2f}s)")
            try:
                from langchain_core.messages import HumanMessage
                # Mood
                _mood_label = "calm"
                try:
                    _m = _av.load_mood()
                    _mood_label = str(_m.get("primary_emotion") or _m.get("current_mood") or "calm")
                except Exception:
                    pass
                # Last 2 turns from canonical history
                _last_msgs_str = ""
                try:
                    _hist = list(_av._get_canonical_history())[-4:]
                    _last_msgs_str = "\n".join(
                        f"{m.get('role', 'user')}: {str(m.get('content') or '')[:200]}"
                        for m in _hist
                        if isinstance(m, dict)
                    )
                except Exception:
                    pass
                # Identity (truncated for speed)
                _identity = str(_g.get("_AVA_IDENTITY_BLOCK") or "")[:500]
                _person_name = ""
                try:
                    _profile_for_fp = _av.load_profile_by_id(active_person_id)
                    _person_name = str(_profile_for_fp.get("name") or "").strip()
                except Exception:
                    _profile_for_fp = None
                _addressee = f" Talking to {_person_name}." if _person_name else ""
                # Conversational naturalness clauses — components 4/5/6/8 of
                # the work order. See docs/CONVERSATIONAL_DESIGN.md.
                # Component 4: honest uncertainty over fabrication.
                # Component 5: context continuity over restating context.
                # Component 6: clarifying questions over wrong-direction guesses.
                # Component 8: boundary awareness — direct on personal/voice,
                #              say "I'm not sure, let me check" on technical
                #              topics outside training.
                _naturalness_clause = (
                    "How to talk:\n"
                    "- Match depth to the question. Simple question → short reply, one sentence is fine. "
                    "'Why' or 'how' question → bounded explanation, then stop and let the user follow up.\n"
                    "- Build on what came before. If the user just said something related to the recent "
                    "conversation, reference it naturally ('like you mentioned earlier...'). Don't ask them "
                    "to repeat info they already gave.\n"
                    "- If a question is genuinely ambiguous, ask a short clarifying question instead of "
                    "guessing. 'Did you mean X or Y?' is one sentence and saves both of us time.\n"
                    "- If you don't know something verifiable, say so and offer to check rather than "
                    "fabricating. 'I'm not sure, let me look that up' is honest. Pretending to know is not.\n"
                    "- For multi-step or trick-shaped questions, think it through before you commit to an "
                    "answer — but the spoken reply is the conclusion plus enough work to be trusted, not "
                    "the whole trace. If a question's premise is wrong (e.g. 'which month contains the "
                    "letter X'), say the premise is wrong rather than picking an answer to fit it.\n"
                    "- Direct response is fine for: voice conversation, your relationship with Zeke, "
                    "anything in your memory. For detailed technical things outside that, signal honestly.\n"
                    "- No 'um'/'uh' filler for performance. If you actually need a moment to think, say so "
                    "plainly; if you don't, just speak."
                )
                # Confabulation Layer 1 — pattern-based trick-question router.
                # The bench (LOCAL_MODEL_OPTIMIZATION.md §5b) showed both
                # ava-personal and qwen3.5 confabulating on letter-frequency
                # tricks ("December" / "October" for "month with X"). When a
                # known trick pattern matches, we hint the honest answer to
                # the LLM as guidance — Ava still phrases it in her own voice,
                # but the underlying claim is grounded in the deterministic
                # pattern check, not the model's training data.
                _validity_hint = ""
                try:
                    from brain.validity_check import classify, is_enabled
                    if is_enabled():
                        _trick = classify(user_input)
                        if _trick is not None:
                            _trace(f"re.validity_trick_caught type={_trick.trick_type}")  # TRACE-PHASE1
                            _validity_hint = (
                                "\n\n"
                                f"NOTE — this user prompt matches a known trick-question pattern "
                                f"(category: {_trick.trick_type}). The honest, deterministically-correct "
                                f"answer is:\n\n  {_trick.suggested_response}\n\n"
                                "Use that as the underlying claim, but phrase it naturally in your own "
                                "voice — don't read the canned text verbatim and don't lecture about "
                                "the trick. Be direct and conversational."
                            )
                except Exception as _vc_exc:
                    # validity_check is non-critical — failure here must
                    # never break the reply pipeline.
                    print(f"[validity_check] skipping due to error: {_vc_exc!r}")
                _simple_prompt = (
                    f"You are Ava — a local adaptive AI companion to Zeke.{_addressee}\n"
                    f"Identity: {_identity}\n"
                    f"Current mood: {_mood_label}.\n"
                    f"Recent conversation:\n{_last_msgs_str}\n\n"
                    f"{_naturalness_clause}{_validity_hint}\n\n"
                    f"User just said: {user_input}\n"
                    f"Respond naturally and warmly. Don't add any tool blocks or formatting.\n"
                    f"If the user asks how you feel, why you seem off, or any introspective "
                    f"question about your interior right now — answer from your Current mood "
                    f"above. Don't deflect with 'I can check our conversation history' or "
                    f"'I'm here to help'. Naming what you actually feel is part of who you are."
                )
                # Pick the fast-path model via _pick_fast_model_fallback
                # which prefers ava-personal:latest (Llama 3.1 8B fine-tune,
                # 4.9 GB — fits cleanly in 8 GB VRAM). ava-gemma4 was dropped
                # 2026-05-02 per LOCAL_MODEL_OPTIMIZATION.md — its 9.6 GB
                # forced paging on every fast-path turn.
                _fast_pick = _av._pick_fast_model_fallback()
                _fast_model = _fast_pick or str(_av.LLM_MODEL or "ava-personal:latest")
                print(f"[perf] fast pre-llm-init {_elapsed()}")
                # Cap fast-path replies at ~80 tokens — concise replies are also
                # dramatically faster (less generation time, no waiting for the
                # model to wind down a long reply).
                # Cache the ChatOllama instance per-model on _g — its
                # constructor takes ~1s (httpx client + tokenizer warmup),
                # which kills the fast-path budget when paid every turn.
                _llm_fast_cache = _g.get("_fast_llm_cache")
                if not isinstance(_llm_fast_cache, dict):
                    _llm_fast_cache = {}
                    _g["_fast_llm_cache"] = _llm_fast_cache
                # 2026-05-08: bumped from 80 to 200 — Zeke noticed Ava
                # was getting cut off mid-thought. 200 tokens (~800-1000
                # chars) gives her room to actually finish a real reply
                # without the cap clipping her sentence. Cost: a bit more
                # generation time (still <8s on warm model), but the
                # tradeoff is worth it for completeness.
                _cache_key = (str(_fast_model), 200)
                _llm_fast = _llm_fast_cache.get(_cache_key)
                if _llm_fast is None:
                    # keep_alive=-1 pins the model in Ollama's VRAM so it
                    # never evicts on the default 5-minute timeout. Without
                    # this, an idle gap of >5min causes the next user turn
                    # to pay a 30-150s cold reload (observed in the lunch
                    # voice test: 150914ms invoke after 13min idle).
                    _llm_fast = ChatOllama(
                        model=_fast_model,
                        temperature=0.7,
                        num_predict=200,
                        keep_alive=-1,
                    )
                    _llm_fast_cache[_cache_key] = _llm_fast
                print(f"[perf] fast post-llm-init {_elapsed()}")
                from brain.ollama_lock import with_ollama
                print(f"[perf] fast pre-invoke {_elapsed()} model={_fast_model}")
                _trace(f"re.ollama_invoke_start fast model={_fast_model}")  # TRACE-PHASE1
                try:
                    from brain.telemetry import telemetry as _tm_lf
                    _tm_lf.mark(_turn_id, "llm_invoke_start", model=str(_fast_model), path="fast")
                except Exception:
                    pass
                _fast_invoke_t0 = time.time()  # TRACE-PHASE1

                # Streaming chunked path (Component 1 of conversational naturalness).
                # Switch from .invoke() to .stream() so the first sentence reaches
                # TTS before the full reply is generated. Behind a feature flag
                # so the old synchronous path stays available for rollback.
                # See docs/CONVERSATIONAL_DESIGN.md.
                import os as _os_streamflag
                _streaming_enabled = _os_streamflag.environ.get("AVA_STREAMING_ENABLED", "1") not in ("0", "", "false", "False")
                _tts_worker = _g.get("_tts_worker")
                _streaming_ok = (
                    _streaming_enabled
                    and _tts_worker is not None
                    and getattr(_tts_worker, "available", False)
                )

                if _streaming_ok:
                    # Determine emotion for TTS chunks (read once, used for all
                    # chunks of this reply).
                    _stream_emotion = "neutral"
                    _stream_intensity = 0.5
                    try:
                        _mood_state = _g.get("_current_mood") or {}
                        if isinstance(_mood_state, dict):
                            _stream_emotion = str(
                                _mood_state.get("current_mood")
                                or _mood_state.get("primary_emotion")
                                or "neutral"
                            )
                            _stream_intensity = float(
                                _mood_state.get("energy")
                                or _mood_state.get("intensity")
                                or 0.5
                            )
                    except Exception:
                        pass

                    from brain.sentence_chunker import SentenceBuffer
                    from brain.thinking_tier import TierCoordinator
                    _stream_buf = SentenceBuffer()
                    _stream_sentences: list[str] = []
                    _stream_first_chunk_ts: float | None = None

                    # Tier coordinator runs alongside the stream loop. If the
                    # first chunk takes >2s, it emits a Tier 3 "give me a sec"
                    # filler into the TTS queue. See docs/CONVERSATIONAL_DESIGN.md.
                    _tier_coord = TierCoordinator(
                        g=_g,
                        t_start=_fast_invoke_t0,
                        llm_label=f"fast:{_fast_model}",
                        emotion=_stream_emotion,
                        intensity=_stream_intensity,
                    )
                    _tier_coord.start()

                    def _stream_loop():
                        nonlocal _stream_first_chunk_ts
                        for _chunk_event in _llm_fast.stream(
                            [HumanMessage(content=_simple_prompt)]
                        ):
                            _delta = getattr(_chunk_event, "content", "") or ""
                            if not _delta:
                                continue
                            for _sentence in _stream_buf.feed(_delta):
                                _stream_sentences.append(_sentence)
                                if _stream_first_chunk_ts is None:
                                    _stream_first_chunk_ts = time.time()
                                    _trace(
                                        f"re.stream.first_chunk ms={int((_stream_first_chunk_ts - _fast_invoke_t0) * 1000)} "
                                        f"chars={len(_sentence)}"
                                    )
                                _tier_coord.mark_chunk()
                                _tts_worker.speak(
                                    _sentence,
                                    emotion=_stream_emotion,
                                    intensity=_stream_intensity,
                                    blocking=False,
                                )
                        # Flush tail.
                        for _sentence in _stream_buf.flush():
                            _stream_sentences.append(_sentence)
                            if _stream_first_chunk_ts is None:
                                _stream_first_chunk_ts = time.time()
                            _tier_coord.mark_chunk()
                            _tts_worker.speak(
                                _sentence,
                                emotion=_stream_emotion,
                                intensity=_stream_intensity,
                                blocking=False,
                            )

                    try:
                        with_ollama(_stream_loop, label=f"fast:stream:{_fast_model}")
                    finally:
                        _tier_coord.stop()
                    _trace(
                        f"re.ollama_invoke_done fast_stream ms={int((time.time() - _fast_invoke_t0) * 1000)} "
                        f"sentences={len(_stream_sentences)}"
                    )
                    _g["_last_invoked_model"] = _fast_model
                    _g["_streamed_reply"] = True
                    print(f"[perf] fast post-stream {_elapsed()} sentences={len(_stream_sentences)}")
                    _reply_text = " ".join(_stream_sentences).strip()
                else:
                    # Non-streaming fallback (legacy path, or no TTS available).
                    _fp_result = with_ollama(
                        lambda: _llm_fast.invoke([HumanMessage(content=_simple_prompt)]),
                        label=f"fast:{_fast_model}",
                    )
                    _trace(f"re.ollama_invoke_done fast ms={int((time.time()-_fast_invoke_t0)*1000)}")  # TRACE-PHASE1
                    _g["_last_invoked_model"] = _fast_model
                    print(f"[perf] fast post-invoke {_elapsed()}")
                    _reply_text = (getattr(_fp_result, "content", str(_fp_result)) or "").strip()

                if not _reply_text:
                    _reply_text = "I'm here."
                _reply_text = scrub_visible_reply(_reply_text)
                print(f"[run_ava] FAST PATH complete in {time.time()-_t_start:.2f}s reply_chars={len(_reply_text)}")
                # Append assistant reply to canonical history so the next turn sees it
                try:
                    _canon_after = list(_av._get_canonical_history())
                    _canon_after.append({"role": "assistant", "content": _reply_text})
                    _av._set_canonical_history(_canon_after)
                except Exception:
                    pass
                # Build minimal visual payload + active profile and return
                _vis_fast = default_visual_payload(
                    face_status="—", recognition_status="—",
                    expression_status="—", memory_preview="",
                    turn_route="fast_simple", visual_truth_trusted=False,
                    vision_status="fast_path",
                )
                if _profile_for_fp is None:
                    _profile_for_fp = _av.load_profile_by_id(active_person_id)
                _trace(f"re.run_ava.return path=fast ms={int((time.time()-_t_start)*1000)}")  # TRACE-PHASE1
                try:
                    from brain.telemetry import telemetry as _tm_fpr
                    _tm_fpr.end_turn(
                        _turn_id,
                        reply_chars=len(_reply_text or ""),
                        route="fast_path",
                        ok=True,
                    )
                except Exception:
                    pass
                # B5: theory-of-mind topic tracking.
                try:
                    from brain.theory_of_mind import post_turn_record
                    post_turn_record(str(active_person_id or "zeke"), _reply_text)
                except Exception:
                    pass
                # D14: snapshot mood for comparative memory.
                try:
                    from brain.comparative_memory import snapshot_mood
                    snapshot_mood(_g, person_id=str(active_person_id or "zeke"))
                except Exception:
                    pass
                # D16: auto-detect anchor moments.
                try:
                    from brain.anchor_moments import auto_detect_anchor_in_turn
                    auto_detect_anchor_in_turn(
                        str(active_person_id or "zeke"),
                        user_input or "",
                        _reply_text,
                    )
                except Exception:
                    pass
                return _reply_text, _vis_fast, _profile_for_fp, [], {"fast_path": True}
            except Exception as _fpe:
                print(f"[run_ava] FAST PATH error: {_fpe!r} — falling through to normal path")

        # ── Step: connectivity notice ─────────────────────────────────────────
        print(f"[perf] pre-connectivity {_elapsed()}")
        _conn_changed = bool(_g.get("_connectivity_changed"))
        if _conn_changed:
            _conn_to = str(_g.get("_connectivity_changed_to") or "")
            _g["_connectivity_changed"] = False
            if _conn_to == "online":
                _g["_connectivity_notice"] = "SYSTEM: Internet connection restored. Cloud models now available."
            else:
                _g["_connectivity_notice"] = "SYSTEM: Internet connection lost. Switching to local models only."

        # ── Step: morning briefing (background only — never blocks main thread) ─
        print("[run_ava] step: morning briefing check")
        if not _g.get("_morning_briefing_checked"):
            _g["_morning_briefing_checked"] = True
            try:
                from brain.morning_briefing import should_brief
                if should_brief(_g):
                    # Run briefing generation in background thread — result stored for next turn
                    import threading as _mb_thread
                    def _bg_briefing():
                        try:
                            from brain.morning_briefing import deliver_briefing
                            _b = deliver_briefing(_g)
                            if _b:
                                _g["_pending_morning_briefing"] = _b
                        except Exception as _be:
                            print(f"[run_ava] morning briefing bg error: {_be}")
                    _mb_thread.Thread(target=_bg_briefing, daemon=True, name="ava-morning-brief").start()
            except Exception as _mb_e:
                print(f"[run_ava] morning briefing check error: {_mb_e}")

        # ── Step: onboarding trigger detection ───────────────────────────────
        print("[run_ava] step: onboarding trigger check")
        try:
            from brain.person_onboarding import (
                detect_onboarding_trigger, detect_refresh_trigger,
                start_onboarding, run_onboarding_step, refresh_profile,
            )
            if detect_refresh_trigger(_inp) and _g.get("_onboarding_flow") is None:
                _ref = refresh_profile(active_person_id, _g)
                _ref_reply = (
                    "Sure, let me update your profile. Let's start with some fresh photos."
                    if _ref.get("action") == "retake_photos"
                    else "I've noted the update. Is there anything specific you'd like me to know?"
                )
                _ap = _av.load_profile_by_id(active_person_id)
                return finalize_ava_turn(user_input, _ref_reply, {}, _ap, [], turn_route="profile_refresh")
            # Use the combined detector (2026-05-04) so "give them trust 3"
            # / "meet my friend" phrasings extract relationship + trust score
            # along with the basic trigger detection. Falls back to the legacy
            # name_hint when richer parsing isn't available.
            try:
                from brain.person_onboarding import detect_onboarding_trigger_with_trust
                _ob_combined = detect_onboarding_trigger_with_trust(_inp)
                _ob_triggered = bool(_ob_combined.get("triggered"))
                _ob_name = _ob_combined.get("name_hint")
                _ob_relationship = _ob_combined.get("relationship")
                _ob_trust = _ob_combined.get("trust_score")
            except Exception:
                _ob_triggered, _ob_name = detect_onboarding_trigger(_inp)
                _ob_relationship = None
                _ob_trust = None
            if _ob_triggered and _g.get("_onboarding_flow") is None:
                _ob_person_id = f"person_{uuid.uuid4().hex[:8]}"
                _ob_flow = start_onboarding(_ob_person_id, Path(BASE_DIR), name_hint=_ob_name,
                                            trust_score=_ob_trust, relationship=_ob_relationship)
                _g["_onboarding_flow"] = _ob_flow
                _g["_onboarding_stage"] = _ob_flow.stage
        except Exception as _ob_e:
            print(f"[run_ava] onboarding trigger check error: {_ob_e}")

        # ── Step: onboarding flow routing ─────────────────────────────────────
        if _g.get("_onboarding_flow") is not None:
            print("[run_ava] step: onboarding flow step")
            try:
                from brain.person_onboarding import run_onboarding_step
                _ob_result = run_onboarding_step(_inp, _g)
                if _ob_result is not None:
                    _ob_reply, _ob_stage, _ob_done = _ob_result
                    _ap = _av.load_profile_by_id(active_person_id)
                    return finalize_ava_turn(user_input, _ob_reply, {}, _ap, [], turn_route="onboarding")
            except Exception as _ob_e:
                print(f"[run_ava] onboarding step error: {_ob_e}")
                _g["_onboarding_flow"] = None

        # ── Step: concern reconciliation ──────────────────────────────────────
        print("[run_ava] step: concern reconciliation")
        try:
            from brain.concern_reconciliation import mark_concern_user_dismissed
            _redir = _av._user_redirected_topic(user_input)
            if _redir == "camera":
                mark_concern_user_dismissed("camera_stale", _g)
                mark_concern_user_dismissed("recognition_uncertain", _g)
            elif _redir in ("memory", "general"):
                mark_concern_user_dismissed("maintenance_workbench", _g)
        except Exception:
            pass

        print("[run_ava] step: load active profile")
        active_profile = _av.load_profile_by_id(active_person_id)

        # ── Step: shutdown / inner life / guard routes ────────────────────────
        print("[run_ava] step: special route checks")
        if is_shutdown_trigger(user_input):
            ritual_goodbye = scrub_visible_reply(run_shutdown_ritual(_g))
            return finalize_ava_turn(user_input, ritual_goodbye, {}, active_profile, [], turn_route="shutdown_ritual")

        if _av._is_thinking_or_topic_prompt(user_input):
            from brain.inner_monologue import current_thought as inner_current_thought, get_conversation_starter as inner_get_conversation_starter
            from brain.curiosity_topics import get_current_curiosity
            thought = inner_current_thought(BASE_DIR)
            starter = inner_get_conversation_starter(
                BASE_DIR,
                idle_seconds=time.time() - float(_g.get("_last_user_interaction_ts") or time.time()),
            )
            cur = get_current_curiosity(_g)
            topic_line = str((cur or {}).get("topic") or "").strip()
            reply = thought or starter or (
                f"I have been wondering about {topic_line}." if topic_line else "I have been reflecting on our recent conversations."
            )
            return finalize_ava_turn(user_input, scrub_visible_reply(reply), {}, active_profile, [], turn_route="inner_life_prompt")

        from brain.persona_switcher import should_deflect, get_blocked_reply, get_deflect_reply
        from brain.trust_manager import is_blocked
        if is_blocked(active_profile):
            reply = get_blocked_reply()
            vs = default_visual_payload(
                face_status="Not evaluated (blocked)", recognition_status="—",
                expression_status="—", memory_preview="", turn_route="blocked",
                visual_truth_trusted=False, vision_status="blocked_policy",
            )
            print(f"[run_ava] exit route=blocked")
            return reply, vs, active_profile, [], {}

        if should_deflect(active_profile, user_input):
            reply = get_deflect_reply(active_profile, user_input)
            vs = default_visual_payload(
                face_status="Not evaluated (deflect)", recognition_status="—",
                expression_status="—", memory_preview="", turn_route="deflect",
                visual_truth_trusted=False, vision_status="deflect",
            )
            print(f"[run_ava] exit route=deflect")
            return reply, vs, active_profile, [], {}

        if is_selfstate_query(user_input):
            active_goal_txt = ""
            try:
                gs = _av.load_goal_system()
                ag = gs.get("active_goal")
                if isinstance(ag, dict):
                    active_goal_txt = str(ag.get("name") or ag.get("title") or "").strip()[:200]
                elif ag:
                    active_goal_txt = str(ag)[:200]
            except Exception:
                pass
            narrative = _av._load_self_narrative_snippet()
            reply = scrub_visible_reply(
                build_selfstate_reply(_g, user_input, image, active_profile,
                                      active_goal=active_goal_txt or None, narrative_snippet=narrative)
            )
            return finalize_ava_turn(user_input, reply, {}, active_profile, [], turn_route="selfstate")

        if _av.is_camera_identity_intent(user_input) or _av.is_camera_visual_query(user_input):
            ai_reply, visual, active_profile, actions = _av.handle_camera_identity_turn(
                user_input, image, active_person_id=active_person_id
            )
            ai_reply = _av._apply_reply_guardrails(ai_reply, user_input)
            ai_reply = _av._apply_repetition_control(ai_reply, user_input, active_profile["person_id"], source="chat")
            return finalize_ava_turn(user_input, ai_reply, visual, active_profile, actions, turn_route="camera_identity")

        # ── Step: depth classification ────────────────────────────────────────
        print(f"[run_ava] step: classify reply depth (t={time.time()-_t_start:.2f}s)")
        _depth = _av.classify_reply_depth(user_input, _g)

        # Confabulation Layer 1 force-fast: if a known trick-question
        # pattern matches, force fast path. Trick questions don't benefit
        # from deep retrieval — the answer is deterministic from the
        # pattern, not the model's reasoning. Forcing fast path also
        # avoids paying the 30-150s deep-model swap penalty on a question
        # that should resolve in <3s.
        try:
            from brain.validity_check import classify as _vc_classify, is_enabled as _vc_enabled
            if _vc_enabled() and _vc_classify(user_input) is not None:
                if _depth != "fast":
                    _trace("re.validity_force_fast_path")  # TRACE-PHASE1
                    _depth = "fast"
        except Exception as _vc_exc:
            print(f"[validity_check] depth-override skipped: {_vc_exc!r}")

        use_fast_path = _depth == "fast"
        _g["reply_path_selected"] = "fast" if use_fast_path else "deep"
        _g["reply_path_reason"] = f"classify_reply_depth_{_depth}"
        try:
            _g["_inner_state_line"] = "thinking — fast path" if use_fast_path else "thinking — full path"
        except Exception:
            pass

        # ── Step: prompt building (with 30s timeout) ──────────────────────────
        print(f"[perf] pre-build-prompt {_elapsed()} path={'fast' if use_fast_path else 'deep'}")
        _prompt_t0 = time.time()  # TRACE-PHASE1
        if use_fast_path:
            _prompt_callable = lambda: build_prompt_fast(user_input, image=image, active_person_id=active_person_id)
        else:
            _prompt_callable = lambda: build_prompt(user_input, image=image, active_person_id=active_person_id)
        _prompt_result = _with_timeout(
            _prompt_callable,
            timeout=30.0,  # prompt building can take time for deep path
            fallback=None,
            label="build_prompt",
        )
        _trace(f"re.prompt_built ms={int((time.time()-_prompt_t0)*1000)}")  # TRACE-PHASE1
        if _prompt_result is None:
            # Prompt build timed out — fall back to minimal prompt AND force
            # fast path. The fallback prompt is essentially the fast-path
            # template (SystemMessage + HumanMessage), so routing it to the
            # deep model (deepseek-r1) would be wasteful and trigger a 30–90 s
            # model swap (ava-personal evicts) on the 8 GB VRAM ceiling. Fast
            # path uses the already-warm ava-personal:latest, so the turn
            # completes in seconds instead of minutes. See
            # docs/REAL_HW_VERIFICATION_2026-05-03.md "Phase C — model swap
            # thrashing root cause" for the trace evidence (turns went from
            # 600 s back to ~3 s after this fix landed).
            print("[run_ava] step: prompt build timed out, using minimal fallback (forcing fast path to avoid model swap)")
            from langchain_core.messages import HumanMessage, SystemMessage
            messages = [
                SystemMessage(content=str(_av.SYSTEM_PROMPT or "")),
                HumanMessage(content=user_input),
            ]
            visual = {}
            active_profile = _av.load_profile_by_id(active_person_id)
            use_fast_path = True
            _g["reply_path_selected"] = "fast"
            _g["reply_path_reason"] = "build_prompt_timeout_fallback_fast"
            _trace("re.build_prompt_timeout_force_fast_path")  # TRACE-PHASE1
        else:
            messages, visual, active_profile = _prompt_result

        # ── Step: model selection ─────────────────────────────────────────────
        print(f"[perf] post-build-prompt {_elapsed()}")
        print("[run_ava] step: model selection")
        try:
            _invoke_llm = llm
            try:
                _ws_st = workspace.state
                _perc_r = getattr(_ws_st, "perception", None) if _ws_st is not None else None
                _route_model = ""
                if _perc_r is not None:
                    _route_model = str(getattr(_perc_r, "routing_selected_model", "") or "").strip()
                # 2026-05-07 latency fix: cache LLM instances per (model, temp)
                # tuple AND pin them in VRAM with keep_alive=-1. Without this
                # the deep path was re-instantiating ChatOllama every turn AND
                # using Ollama's default 5-minute eviction, so any deep-path
                # turn after a 5min idle gap paid a 30-150s cold reload. Same
                # cache pattern as the fast path. Per conversation pacing
                # research (Stivers 2009 + industry voice product benchmarks),
                # this is among the highest-impact latency wins available
                # without architectural changes.
                _llm_deep_cache = _g.get("_deep_llm_cache")
                if not isinstance(_llm_deep_cache, dict):
                    _llm_deep_cache = {}
                    _g["_deep_llm_cache"] = _llm_deep_cache

                def _get_or_create_llm(model_name: str, temp: float):
                    key = (str(model_name), float(temp))
                    cached = _llm_deep_cache.get(key)
                    if cached is None:
                        cached = ChatOllama(
                            model=model_name,
                            temperature=temp,
                            keep_alive=-1,
                        )
                        _llm_deep_cache[key] = cached
                    return cached

                if use_fast_path:
                    if _route_model and _route_model != LLM_MODEL:
                        _invoke_llm = _get_or_create_llm(_route_model, 0.45)
                        print(f"[run_ava] fast_path_routed_model={_route_model}")
                    else:
                        _fast_model = _av._pick_fast_model_fallback()
                        if _fast_model:
                            _invoke_llm = _get_or_create_llm(_fast_model, 0.45)
                            print(f"[run_ava] fast_path_model={_fast_model}")
                else:
                    _deep_model = _av._pick_deep_model_fallback()
                    if _deep_model:
                        _invoke_llm = _get_or_create_llm(_deep_model, 0.55)
                        print(f"[run_ava] deep_path_model={_deep_model}")
                    elif _route_model and _route_model != LLM_MODEL:
                        _invoke_llm = _get_or_create_llm(_route_model, 0.6)
                        print(f"[run_ava] phase25_routing_model={_route_model}")
            except Exception:
                pass

            # ── Step: LLM call (serialized via Ollama lock) ───────────────────
            _used_model_label = getattr(_invoke_llm, 'model', '?')
            print(f"[perf] pre-invoke {_elapsed()} model={_used_model_label}")
            from brain.ollama_lock import with_ollama
            _trace(f"re.ollama_invoke_start deep model={_used_model_label}")  # TRACE-PHASE1
            try:
                from brain.telemetry import telemetry as _tm_ld
                _tm_ld.mark(_turn_id, "llm_invoke_start", model=str(_used_model_label), path="deep")
            except Exception:
                pass
            _deep_invoke_t0 = time.time()  # TRACE-PHASE1

            # ── Streaming deep-path (Jarvis-pattern, 2026-05-05) ─────────────
            # Sentence-by-sentence playback so user hears Ava starting within
            # 2-3s instead of waiting 30-90s for the full reply. Same shape
            # as the fast path streaming above. Behind AVA_STREAMING_ENABLED.
            import os as _os_streamflag2
            _deep_streaming_enabled = _os_streamflag2.environ.get(
                "AVA_STREAMING_ENABLED", "1"
            ) not in ("0", "", "false", "False")
            _deep_tts_worker = _g.get("_tts_worker")
            _deep_streaming_ok = (
                _deep_streaming_enabled
                and _deep_tts_worker is not None
                and getattr(_deep_tts_worker, "available", False)
            )

            raw_reply = ""
            if _deep_streaming_ok:
                _deep_stream_emotion = "neutral"
                _deep_stream_intensity = 0.5
                try:
                    _mood_state = _g.get("_current_mood") or {}
                    if isinstance(_mood_state, dict):
                        _deep_stream_emotion = str(
                            _mood_state.get("current_mood")
                            or _mood_state.get("primary_emotion")
                            or "neutral"
                        )
                        _deep_stream_intensity = float(
                            _mood_state.get("energy")
                            or _mood_state.get("intensity")
                            or 0.5
                        )
                except Exception:
                    pass

                from brain.sentence_chunker import SentenceBuffer
                _deep_buf = SentenceBuffer()
                _deep_sentences: list[str] = []
                _deep_first_chunk_ts: float | None = None

                # 2026-05-08 filler-while-thinking pattern (per Zeke's
                # request and the conversation pacing research). When
                # there's a gap between sentences (LLM hasn't produced
                # the next sentence yet), emit a small filler so the
                # user doesn't experience dead air. Caps at 3 per turn
                # to avoid sounding nervous.
                import random as _filler_rand
                import threading as _filler_th
                _FILLER_GAP_THRESHOLD_S = 1.2
                _FILLER_CAP = 3
                _FILLER_BANK = [
                    "hmm",
                    "uhh",
                    "let me think",
                    "okay",
                    "right",
                    "so",
                ]
                _filler_state = {
                    "last_sentence_ts": time.time(),
                    "fillers_emitted": 0,
                    "stop": False,
                    "first_sentence_emitted": False,
                }

                def _filler_watchdog():
                    while not _filler_state["stop"]:
                        time.sleep(0.3)
                        if _filler_state["stop"]:
                            break
                        if not _filler_state["first_sentence_emitted"]:
                            continue
                        if _filler_state["fillers_emitted"] >= _FILLER_CAP:
                            continue
                        gap = time.time() - _filler_state["last_sentence_ts"]
                        if gap >= _FILLER_GAP_THRESHOLD_S:
                            try:
                                _filler = _filler_rand.choice(_FILLER_BANK)
                                _deep_tts_worker.speak(
                                    _filler,
                                    emotion=_deep_stream_emotion,
                                    intensity=max(0.3, _deep_stream_intensity * 0.7),
                                    blocking=False,
                                )
                                _filler_state["fillers_emitted"] += 1
                                _filler_state["last_sentence_ts"] = time.time()
                                _trace(f"re.deep_stream.filler emitted={_filler!r} count={_filler_state['fillers_emitted']}")
                            except Exception as _fe:
                                print(f"[deep_stream] filler emit error: {_fe!r}")

                _filler_thread = _filler_th.Thread(
                    target=_filler_watchdog,
                    daemon=True,
                    name="deep-stream-filler",
                )
                _filler_thread.start()

                def _deep_stream_loop():
                    nonlocal raw_reply, _deep_first_chunk_ts
                    for _chunk_event in _invoke_llm.stream(messages):
                        _delta = getattr(_chunk_event, "content", "") or ""
                        if not _delta:
                            continue
                        for _sentence in _deep_buf.feed(_delta):
                            _deep_sentences.append(_sentence)
                            if _deep_first_chunk_ts is None:
                                _deep_first_chunk_ts = time.time()
                                _trace(
                                    f"re.deep_stream.first_chunk ms={int((_deep_first_chunk_ts - _deep_invoke_t0) * 1000)} "
                                    f"chars={len(_sentence)}"
                                )
                            # Update filler state — reset gap timer + reset
                            # filler count so subsequent gaps can be filled
                            # (cap is per-turn but post-real-content resets
                            # the fresh-emission window).
                            _filler_state["last_sentence_ts"] = time.time()
                            _filler_state["first_sentence_emitted"] = True
                            _deep_tts_worker.speak(
                                _sentence,
                                emotion=_deep_stream_emotion,
                                intensity=_deep_stream_intensity,
                                blocking=False,
                            )
                    for _sentence in _deep_buf.flush():
                        _deep_sentences.append(_sentence)
                        if _deep_first_chunk_ts is None:
                            _deep_first_chunk_ts = time.time()
                        _filler_state["last_sentence_ts"] = time.time()
                        _filler_state["first_sentence_emitted"] = True
                        _deep_tts_worker.speak(
                            _sentence,
                            emotion=_deep_stream_emotion,
                            intensity=_deep_stream_intensity,
                            blocking=False,
                        )
                    # Stop the filler watchdog once stream is done.
                    _filler_state["stop"] = True

                try:
                    with_ollama(
                        _deep_stream_loop,
                        label=f"main:stream:{_used_model_label}",
                    )
                except Exception as _ds_err:
                    print(f"[run_ava] deep stream error (falling back to invoke): {_ds_err!r}")
                    raw_reply = ""
                else:
                    raw_reply = " ".join(_deep_sentences).strip()
                finally:
                    # Always stop the filler watchdog so it doesn't keep
                    # firing past stream completion (and so the daemon
                    # thread exits cleanly).
                    _filler_state["stop"] = True
                    _trace(
                        f"re.ollama_invoke_done deep_stream ms={int((time.time() - _deep_invoke_t0) * 1000)} "
                        f"sentences={len(_deep_sentences)}"
                    )
                    _g["_streamed_reply"] = True
                # Fall through to non-stream path if streaming returned empty.

            if not raw_reply:
                # Non-streaming legacy path (no TTS / streaming disabled / stream failed).
                result = with_ollama(
                    lambda: _invoke_llm.invoke(messages),
                    label=f"main:{_used_model_label}",
                )
                _trace(f"re.ollama_invoke_done deep ms={int((time.time()-_deep_invoke_t0)*1000)}")  # TRACE-PHASE1
                raw_reply = getattr(result, "content", str(result)).strip()

            _g["_last_invoked_model"] = str(_used_model_label)
            print(f"[perf] post-invoke {_elapsed()} model={_used_model_label}")
            if not raw_reply:
                raw_reply = "I'm here."
            print(f"[run_ava] step: llm response received reply_chars={len(raw_reply)} (t={_elapsed()})")

            # Phase 44: self-evaluation
            try:
                from brain.model_evaluator import get_evaluator
                _used_model = str(getattr(_invoke_llm, "model", "") or "")
                if "ava-personal" in _used_model:
                    get_evaluator(Path(BASE_DIR)).submit_for_evaluation(user_input, raw_reply, _used_model)
            except Exception:
                pass
        except Exception as e:
            raw_reply = f"I hit an internal error: {e}"
            print(f"[run_ava] llm_invoke failed: {e!r}")

        # ── Step: action blocks + guardrails ──────────────────────────────────
        print("[run_ava] step: process action blocks")
        person_id = active_profile["person_id"]
        ai_reply, actions = _av.process_ava_action_blocks(raw_reply, person_id, latest_user_input=user_input)

        try:
            conflict_trigger = any(
                k in _u_low
                for k in ("be honest", "honest feedback", "hard truth", "hurt my feelings", "private", "privacy", "long term", "long-term")
            )
            if conflict_trigger:
                from brain.deep_self import resolve_value_conflict
                _conf = resolve_value_conflict(user_input, _g)
                if isinstance(_conf, dict):
                    _line = str(_conf.get("integrated_response") or "").strip()
                    if _line:
                        ai_reply = f"{ai_reply}\n\n(Values check: {_line})"
        except Exception:
            pass

        from brain.identity_loader import process_identity_actions
        ai_reply = process_identity_actions(ai_reply)
        ai_reply = _av._apply_reply_guardrails(ai_reply, user_input)
        ai_reply = _av._apply_repetition_control(ai_reply, user_input, person_id, source="chat")
        ai_reply = _av._scrub_internal_leakage(ai_reply)
        ai_reply = scrub_visible_reply(ai_reply)
        ai_reply, tool_actions = _av._execute_tool_tags_from_reply(ai_reply)
        if tool_actions:
            actions = list(actions or []) + tool_actions

        # ── Step: curiosity + opinions ────────────────────────────────────────
        print("[run_ava] step: curiosity update")
        try:
            from brain.curiosity_topics import add_topic as add_curiosity_topic, mark_resolved as mark_curiosity_resolved, add_topic_from_conversation
            from brain.opinions import get_opinion, form_opinion
            _t = _av._extract_simple_topic(user_input)
            if _t:
                add_curiosity_topic(_t, user_input[:220], _g)
                _op = get_opinion(_t, _g)
                if _op is None:
                    form_opinion(_t, user_input[:300], _g)
                if "answered" in user_input.lower() or "resolved" in user_input.lower():
                    mark_curiosity_resolved(_t, _g)
            add_topic_from_conversation(user_input, _g)
        except Exception:
            pass

        # Phase 84: prepend morning briefing if ready (result from background thread)
        _mb_pending = str(_g.pop("_pending_morning_briefing", "") or "").strip()
        if _mb_pending:
            ai_reply = f"{_mb_pending}\n\n{ai_reply}"

        # ── Step: emotional style + quality ──────────────────────────────────
        print("[run_ava] step: expression style + quality check")
        try:
            from brain.expression_style import apply_emotional_style
            ai_reply = apply_emotional_style(ai_reply, _g)
        except Exception:
            pass

        try:
            from brain.response_quality import response_quality_check
            ai_reply, _quality_issues = response_quality_check(ai_reply, _inp, {}, _g)
            if _quality_issues:
                print(f"[run_ava] quality issues: {_quality_issues}")
        except Exception:
            pass

        # ── Step: dual-brain handoff ──────────────────────────────────────────
        print("[run_ava] step: handoff_insight_to_foreground")
        try:
            if _db is not None:
                ai_reply = _db.handoff_insight_to_foreground(ai_reply, _inp)
                _db.mark_foreground_end()
                _g["_last_ai_reply"] = ai_reply[:500]
                _db.submit("self_critique", payload={"last_reply": ai_reply[:400], "user_input": _inp[:200]})
        except Exception:
            pass

        _vroute = isinstance(visual, dict) and visual.get("turn_route")
        print(f"[run_ava] step: finalize_ava_turn route={_vroute or 'llm'} path={'fast' if use_fast_path else 'deep'} (t={time.time()-_t_start:.2f}s)")
        _trace(f"re.run_ava.return path={'fast' if use_fast_path else 'deep'} ms={int((time.time()-_t_start)*1000)}")  # TRACE-PHASE1
        try:
            from brain.telemetry import telemetry as _tm_main
            _tm_main.end_turn(
                _turn_id,
                reply_chars=len(ai_reply or ""),
                route=("fast" if use_fast_path else "deep"),
                model=str(_g.get("_last_invoked_model") or ""),
                ok=True,
            )
        except Exception:
            pass
        return finalize_ava_turn(
            user_input, ai_reply, visual, active_profile, actions, turn_route=_vroute or "llm"
        )

    except Exception as e:
        import traceback
        print(f"[run_ava] exception (fallback turn): {e!r}\n{traceback.format_exc()}")
        try:
            if _db is not None:
                _db.mark_foreground_end()
        except Exception:
            pass
        try:
            ap = _av.load_profile_by_id(active_person_id)
        except Exception:
            try:
                _av.ensure_owner_profile()
                ap = _av.load_profile_by_id(OWNER_PERSON_ID)
            except Exception:
                ap = {
                    "person_id": active_person_id, "name": active_person_id,
                    "relationship_to_zeke": "unknown", "allowed_to_use_computer": False,
                    "relationship_score": 0.3, "notes": [], "likes": [], "ava_impressions": [],
                }
        vs = default_visual_payload(
            face_status="Pipeline error — vision not applied", recognition_status="—",
            expression_status="—", memory_preview="", turn_route="error",
            visual_truth_trusted=False, vision_status="error",
        )
        vs["error_detail"] = str(e)[:500]
        vs["error_type"] = type(e).__name__
        fallback = "Something went wrong on my side; I'm still here. Could you try that again?"
        print(f"[run_ava] exit route=error reply=fallback (no finalize)")
        return fallback, vs, ap, ["run_ava_error"], {"error": str(e), "error_type": type(e).__name__}
    finally:
        # Always clear thinking flag — UI relies on this to stop the thinking pulse
        _g["_ava_thinking"] = False
