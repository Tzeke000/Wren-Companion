"""
brain/startup.py — Ava startup initialization sequence.

Extracted from avaagent.py. Call run_startup(globals()) from avaagent.py
after all module-level definitions are done.

g is avaagent's globals() dict — all avaagent functions and constants
are accessible through it. We write initialized objects back into g so
avaagent's global namespace sees them.

THREADING RULE: any call that invokes Ollama / LLM must run in a daemon
thread, never on the main startup thread. The main thread must reach the
operator_server startup within ~10 seconds of launch.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


def _bg(name: str, fn, *args, **kwargs) -> threading.Thread:
    """Spawn a daemon thread and start it immediately."""
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True, name=name)
    t.start()
    return t


def run_startup(g: dict[str, Any]) -> None:
    """Initialize all Ava subsystems. g = globals() from avaagent.py."""
    # Guard: prevent double-execution if the module is somehow imported twice
    if g.get("_STARTUP_COMPLETE"):
        print("[startup] already complete — skipping duplicate run")
        return

    # AVA_TEST_MODE: route audio input to CABLE Output so the audio loopback
    # harness (Piper TTS played to CABLE Input) is heard by Ava's wake/STT
    # path. Production listens to GAIA HD Mic; test mode swaps to the virtual
    # cable so synthesized voice can drive the conversation. Option D from
    # bugs/synthesized-voice-wake-detection.md.
    import os as _os_test
    if _os_test.environ.get("AVA_TEST_MODE", "0").strip() == "1":
        try:
            import sounddevice as _sd_test
            _devs = _sd_test.query_devices()
            _cable_out_idx = None
            for _i, _d in enumerate(_devs):
                if "cable output" in _d.get("name", "").lower() and _d.get("max_input_channels", 0) > 0:
                    _cable_out_idx = _i
                    break
            if _cable_out_idx is not None:
                _orig_default = _sd_test.default.device
                _new_default = (_cable_out_idx, _orig_default[1] if isinstance(_orig_default, (tuple, list)) else None)
                _sd_test.default.device = _new_default
                print(f"[startup] AVA_TEST_MODE=1 — input routed to CABLE Output (idx={_cable_out_idx})")
            else:
                print("[startup] AVA_TEST_MODE=1 but CABLE Output not found — input still on default mic")
        except Exception as _td_e:
            print(f"[startup] AVA_TEST_MODE audio routing failed: {_td_e!r}")

    BASE_DIR: Path = g["BASE_DIR"]
    STATE_DIR: Path = g["STATE_DIR"]

    # Clear any stale restart flag left from a previous session so the watchdog
    # doesn't see it and immediately launch a second avaagent instance.
    _restart_flag = BASE_DIR / "state" / "restart_requested.flag"
    try:
        if _restart_flag.is_file():
            _restart_flag.unlink()
            print("[startup] cleared stale restart_requested.flag")
    except Exception as _rf_e:
        print(f"[startup] could not clear restart flag: {_rf_e}")
    MOOD_PATH: Path = g["MOOD_PATH"]
    OWNER_PERSON_ID: str = g["OWNER_PERSON_ID"]
    # Signal bus — must come early so every later subsystem can fire signals
    # into it without crashing on a None lookup.
    print("[startup] step: signal bus")
    try:
        from brain.signal_bus import bootstrap_signal_bus
        bootstrap_signal_bus(g)
        print("[startup] signal bus ready")
    except Exception as _sb_e:
        g["_signal_bus"] = None
        print(f"[signal_bus] startup skipped: {_sb_e}")

    print("[startup] step: identity + profiles")
    g["ensure_owner_profile"]()
    g["ensure_identity_files"]()

    print("[startup] step: tool registry")
    try:
        _tool_registry = g["ToolRegistry"]()
        g["_tool_registry"] = _tool_registry
        g["_desktop_tool_registry"] = _tool_registry
    except Exception as e:
        g["_tool_registry"] = None
        g["_desktop_tool_registry"] = None
        print(f"[tool_registry] startup skipped: {e}")

    print("[startup] step: visual memory")
    try:
        _vm = g["VisualMemory"](BASE_DIR)
        g["_visual_memory"] = _vm
        g["_visual_memory_summary"] = _vm.get_cluster_summary()
    except Exception as e:
        g["_visual_memory"] = None
        g["_visual_memory_summary"] = {"cluster_count": 0, "named_clusters": 0, "most_seen": ""}
        print(f"[visual_memory] startup skipped: {e}")

    print("[startup] step: connectivity monitor")
    try:
        from brain.connectivity import bootstrap_connectivity
        bootstrap_connectivity(g)
    except Exception as e:
        g["_is_online"] = False
        g["_connection_quality"] = "offline"
        g["_ollama_cloud_reachable"] = False
        g["_connectivity_changed"] = False
        print(f"[connectivity] bootstrap skipped: {e}")

    print("[startup] step: image generator")
    try:
        from tools.creative.image_generator import ImageGenerator
        g["_image_generator"] = ImageGenerator(g)
        print("[image_gen] ImageGenerator initialized")
    except Exception as e:
        g["_image_generator"] = None
        print(f"[image_gen] startup skipped: {e}")

    print("[startup] step: dual brain")
    try:
        from brain.dual_brain import bootstrap_dual_brain
        bootstrap_dual_brain(g)
    except Exception as e:
        g["_dual_brain"] = None
        print(f"[dual_brain] startup skipped: {e}")

    print("[startup] step: eye tracker")
    try:
        from brain.eye_tracker import bootstrap_eye_tracker
        bootstrap_eye_tracker(g)
    except Exception as e:
        g["_eye_tracker"] = None
        print(f"[eye_tracker] startup skipped: {e}")

    print("[startup] step: expression detector")
    try:
        from brain.expression_detector import bootstrap_expression_detector
        bootstrap_expression_detector(g)
    except Exception as e:
        g["_expression_detector"] = None
        print(f"[expression_detector] startup skipped: {e}")

    print("[startup] step: video memory")
    try:
        from brain.video_memory import bootstrap_video_memory
        bootstrap_video_memory(g)
    except Exception as e:
        g["_video_memory"] = None
        print(f"[video_memory] startup skipped: {e}")

    print("[startup] step: heartbeat bootstrap")
    try:
        from brain.heartbeat import bootstrap_heartbeat_runtime
        bootstrap_heartbeat_runtime(g)
    except Exception as e:
        print(f"[heartbeat] bootstrap skipped: {e}")

    print("[startup] step: startup resume")
    try:
        from brain.runtime_presence import bootstrap_startup_resume
        bootstrap_startup_resume(g)
    except Exception as e:
        print(f"[startup_resume] skipped: {e}")

    print("[startup] step: concern reconciliation")
    try:
        from brain.concern_reconciliation import run_startup_concern_reconciliation
        run_startup_concern_reconciliation(g)
    except Exception as e:
        print(f"[concern_reconciliation] startup skipped: {e}")

    print("[startup] step: curiosity topics bootstrap")
    try:
        g["bootstrap_curiosity_topics"](g)
    except Exception as e:
        print(f"[curiosity_topics] bootstrap skipped: {e}")

    # ── Concept graph: init object synchronously (file load only, no LLM) ────
    # bootstrap_from_existing_memory calls mistral:7b — runs in background thread
    print("[startup] step: concept graph init")
    try:
        from brain.concept_graph import ConceptGraph, bootstrap_from_existing_memory
        _concept_graph = ConceptGraph(BASE_DIR)
        g["_concept_graph"] = _concept_graph
        g["_concept_graph_bootstrap_nodes"] = 0

        _tmp_path = BASE_DIR / "state" / "concept_graph.json.tmp"
        if _tmp_path.is_file():
            # Another process left the .tmp locked — skip bootstrap this run
            # to avoid WinError 5 / 32. The graph loaded fine from the .json file.
            try:
                _tmp_path.unlink()
                print("[concept_graph] stale .tmp removed, bootstrap will run")
            except OSError:
                print("[concept_graph] .tmp still locked — skipping bootstrap this run")
                _bg("ava-cg-bootstrap", lambda: None)  # no-op placeholder
                _concept_graph.decay_unused_nodes(days_threshold=30)
                # Skip the real bootstrap block below
                raise RuntimeError("tmp_locked")

        def _bg_concept_bootstrap():
            try:
                _cg_boot = bootstrap_from_existing_memory(_concept_graph, g)
                if isinstance(_cg_boot, dict):
                    g["_concept_graph_bootstrap_nodes"] = int(_cg_boot.get("nodes_created") or 0)
                else:
                    g["_concept_graph_bootstrap_nodes"] = int(_cg_boot or 0)
                _concept_graph.decay_unused_nodes(days_threshold=30)
                print(f"[concept_graph] bootstrap complete nodes={g['_concept_graph_bootstrap_nodes']}")
            except Exception as e:
                print(f"[concept_graph] background bootstrap error: {e}")

        _bg("ava-cg-bootstrap", _bg_concept_bootstrap)
        print("[startup] step: concept graph bootstrap dispatched to background")
    except Exception as e:
        g["_concept_graph"] = None
        g["_concept_graph_bootstrap_nodes"] = 0
        print(f"[concept_graph] startup skipped: {e}")

    print("[startup] step: finetune pipeline")
    try:
        from brain.finetune_pipeline import FineTuneManager
        _finetune_manager = FineTuneManager(BASE_DIR)
        g["_finetune_manager"] = _finetune_manager
        g["_finetune_status"] = _finetune_manager._read_status()
        _finetune_manager.schedule_finetune(interval_days=7)
    except Exception as e:
        g["_finetune_manager"] = None
        g["_finetune_status"] = {"status": "idle", "error": str(e)}
        print(f"[finetune] startup skipped: {e}")

    print("[startup] step: deep self snapshot")
    try:
        from brain.deep_self import deep_self_snapshot
        g["_deep_self"] = deep_self_snapshot(g)
    except Exception as e:
        g["_deep_self"] = {}
        print(f"[deep_self] startup skipped: {e}")

    # ── Self model weekly update: calls qwen2.5:14b — background thread ───────
    print("[startup] step: self model update (background)")
    try:
        def _bg_self_model():
            try:
                g["update_self_model"](g)
                print("[self_model] weekly update complete")
            except Exception as e:
                print(f"[self_model] weekly update error: {e}")
        _bg("ava-self-model-update", _bg_self_model)
    except Exception as e:
        print(f"[self_model] weekly update skipped: {e}")

    print("[startup] step: inner monologue")
    try:
        from brain.inner_monologue import start_inner_monologue
        start_inner_monologue(g)
    except Exception as e:
        print(f"[inner_monologue] startup skipped: {e}")

    print("[startup] step: history manager")
    try:
        from brain.history_manager import AvaHistoryManager
        g["history_manager"] = AvaHistoryManager(BASE_DIR, target_context_length=8000)
    except Exception as e:
        g["history_manager"] = None
        print(f"[history_manager] startup skipped: {e}")

    print("[startup] step: pickup note")
    try:
        from brain.shutdown_ritual import load_pickup_note
        g["pickup_note"] = load_pickup_note()
    except Exception as e:
        g["pickup_note"] = None
        print(f"[shutdown_ritual] pickup note load skipped: {e}")

    print("[startup] step: profiles + identity")
    g["seed_default_profiles"]()
    from brain.identity_loader import load_ava_identity
    g["_AVA_IDENTITY_BLOCK"] = load_ava_identity()
    g["ensure_emotion_reference_file"]()
    g["_write_ava_pid_file"]()

    # Task 5 (2026-05-02): if a restart handoff is pending from a previous
    # voice-command-triggered restart, replay it now. read_handoff_on_boot
    # computes time_offline, surfaces a thought into inner monologue, and
    # deletes the file (read-once). Best-effort — no-op if no handoff.
    try:
        from brain.restart_handoff import read_handoff_on_boot
        handoff = read_handoff_on_boot(g)
        if handoff is not None:
            secs = float(handoff.get("time_offline_seconds") or 0.0)
            print(f"[startup] restart handoff replayed: time_offline={secs:.1f}s over_run={handoff.get('over_run')}")
            g["_last_restart_handoff"] = handoff
    except Exception as _rh_exc:
        print(f"[startup] restart handoff replay error: {_rh_exc!r}")
    # Defer selftest until after vectorstore init (which runs in background)
    # so vector_memory check passes correctly
    def _delayed_selftest():
        time.sleep(15.0)  # wait for vectorstore init to complete
        try:
            from brain.health_runtime import print_startup_selftest
            print_startup_selftest(g)
        except Exception as _ste:
            print(f"[startup-selftest] error: {_ste}")
    _bg("ava-delayed-selftest", _delayed_selftest)

    print("[startup] step: self narrative")
    from brain.beliefs import SELF_NARRATIVE_PATH, save_self_narrative, load_self_narrative
    if not SELF_NARRATIVE_PATH.exists():
        save_self_narrative(load_self_narrative())
        print("[beliefs] self-narrative initialized")
    else:
        load_self_narrative()
        print("[beliefs] self-narrative loaded")

    print("[startup] step: goal system")
    g["load_goal_system"]()

    # ── Vectorstore: embed_query call blocks on nomic-embed-text — background ─
    print("[startup] step: vectorstore init (background)")
    try:
        def _bg_vectorstore():
            try:
                g["init_vectorstore"]()
            except Exception as e:
                print(f"[vectorstore] background init error: {e}")
        _bg("ava-vectorstore-init", _bg_vectorstore)
    except Exception as e:
        print(f"[vectorstore] background dispatch failed: {e}")

    print("[startup] step: memory decay tick")
    try:
        from brain.memory import decay_tick
        decay_tick(g)
        print("[memory] decay tick complete")
    except Exception as e:
        print(f"[memory] decay tick failed: {e}")

    print("[startup] step: life rhythm schedule")
    try:
        g["_schedule_life_rhythm_on_startup"]()
    except Exception as e:
        print(f"[life_rhythm] schedule failed: {e}")

    print("[startup] step: health check")
    try:
        from brain.health import run_system_health_check
        _health_state = run_system_health_check(g, kind="startup")
        print(f"Health: {_health_state.get('startup_summary', 'UNKNOWN')}")
    except Exception as e:
        print(f"[health] startup check failed: {e}")

    print("[startup] step: face labels")
    g["load_face_labels"]()

    # InsightFace GPU engine (additive — face_recognizer remains the fallback).
    # Init in a background thread because:
    #   - buffalo_l weights download is ~280MB on first run
    #   - cudnn EXHAUSTIVE algorithm search can take ~80s on first init,
    #     after which results are cached on disk and re-init is fast.
    print("[startup] step: insight_face GPU engine (background — first-run cudnn warmup ~60-90s, cached after)")
    try:
        def _bg_insight_face():
            try:
                from brain.insight_face_engine import bootstrap_insight_face
                engine = bootstrap_insight_face(g)
                # Verification print so the user sees end-to-end success.
                ok = engine is not None and getattr(engine, "available", False)
                print(f"[startup] insight_face: ready={ok} provider={engine.provider() if ok else 'none'}")
            except Exception as e:
                print(f"[insight_face] background init error: {e!r}")
        _bg("ava-insight-face-init", _bg_insight_face)
    except Exception as e:
        g["_insight_face"] = None
        print(f"[insight_face] dispatch failed: {e}")

    # Expression calibrator — per-person baseline of facial geometry. Cheap to
    # init; loads existing baselines on first call.
    print("[startup] step: expression calibrator")
    try:
        from brain.expression_calibrator import bootstrap_expression_calibrator
        bootstrap_expression_calibrator(g)
    except Exception as e:
        g["_expression_calibrator"] = None
        print(f"[expression_calibrator] startup skipped: {e}")

    # Voice mood detector (librosa). Lightweight — instance created on demand.
    print("[startup] step: voice mood detector")
    try:
        from brain.voice_mood_detector import bootstrap_voice_mood_detector
        bootstrap_voice_mood_detector(g)
    except Exception as e:
        g["_voice_mood_detector"] = None
        print(f"[voice_mood] startup skipped: {e}")

    # Wake learner — loads any patterns Ava previously learned at runtime and
    # hydrates them into the WakeDetector singleton.
    print("[startup] step: wake learner")
    try:
        from brain.wake_learner import bootstrap_wake_learner
        bootstrap_wake_learner(g)
    except Exception as e:
        g["_wake_learner"] = None
        print(f"[wake_learner] startup skipped: {e}")

    # Question engine — Ava decides when she wants to ask Zeke things.
    print("[startup] step: question engine")
    try:
        from brain.question_engine import bootstrap_question_engine
        bootstrap_question_engine(g)
    except Exception as e:
        g["_question_engine"] = None
        print(f"[question_engine] startup skipped: {e}")

    # Ava memory (mem0 + ChromaDB + Ollama). Initializes in a background
    # thread because the first add() warms up the LLM.
    print("[startup] step: ava memory (mem0)")
    try:
        from brain.ava_memory import bootstrap_ava_memory
        bootstrap_ava_memory(g)
    except Exception as e:
        g["_ava_memory"] = None
        print(f"[ava_memory] startup skipped: {e}")

    # Command builder + voice command router (must come BEFORE app discovery
    # init so VoiceCommandRouter sees an empty custom-commands list, then
    # reload happens automatically when Ava/Zeke create new commands).
    print("[startup] step: command builder + voice command router")
    try:
        from brain.command_builder import bootstrap_command_builder
        from brain.voice_commands import bootstrap_voice_command_router, builtin_count
        bootstrap_command_builder(g)
        bootstrap_voice_command_router(g)
        print(f"[voice_commands] router ready ({builtin_count()} built-in commands)")
    except Exception as e:
        g["_command_builder"] = None
        g["_voice_command_router"] = None
        print(f"[voice_commands] startup skipped: {e}")

    # Correction handler — detects "no, I meant X" and learns mappings.
    print("[startup] step: correction handler")
    try:
        from brain.correction_handler import bootstrap_correction_handler
        bootstrap_correction_handler(g)
    except Exception as e:
        g["_correction_handler"] = None
        print(f"[correction_handler] startup skipped: {e}")

    # App discoverer — scans desktop / Program Files / Steam / Epic for
    # installed apps and games. Initial scan is in a background thread.
    print("[startup] step: app discoverer (background)")
    try:
        from brain.app_discoverer import bootstrap_app_discoverer
        bootstrap_app_discoverer(g)
    except Exception as e:
        g["_app_discoverer"] = None
        print(f"[app_discoverer] startup skipped: {e}")

    # Urgent reminder handler — fires on SIGNAL_REMINDER_DUE.
    # Reminders also run via a heartbeat sweep; this handler covers the case
    # where another subsystem decides a reminder is urgent enough to bypass
    # the next 30s tick.
    try:
        from brain.signal_bus import get_signal_bus, SIGNAL_REMINDER_DUE
        _bus = get_signal_bus()
        if _bus is not None:
            def _urgent_reminder(signal: dict) -> None:
                try:
                    text = str(signal.get("data", {}).get("text") or "")
                    if not text:
                        return
                    worker = g.get("_tts_worker")
                    if worker is not None and getattr(worker, "available", False) and bool(g.get("tts_enabled", False)):
                        worker.speak_with_emotion(
                            f"Reminder: {text}",
                            emotion="curiosity",
                            intensity=0.5,
                            blocking=False,
                        )
                except Exception as _e:
                    print(f"[urgent_reminder] error: {_e}")
            _bus.register_urgent_handler(SIGNAL_REMINDER_DUE, _urgent_reminder)
            print("[startup] urgent SIGNAL_REMINDER_DUE handler registered")
    except Exception as _rh_e:
        print(f"[urgent_reminder] handler registration skipped: {_rh_e}")

    print("[startup] step: mood init")
    if not MOOD_PATH.exists():
        g["save_mood"](g["enrich_mood_state"](g["default_mood"]()))
    else:
        g["save_mood"](g["load_mood"]())

    # Phase 65: mood carryover with decay
    try:
        _co_path = STATE_DIR / "mood_carryover.json"
        if _co_path.is_file():
            _co = json.loads(_co_path.read_text(encoding="utf-8"))
            _co_mood = _co.get("mood") or {}
            _co_ts = float(_co.get("shutdown_ts") or 0)
            _absent_hours = (time.time() - _co_ts) / 3600 if _co_ts > 0 else 999
            if _absent_hours < 72 and isinstance(_co_mood, dict):
                _decay = max(0.0, 1.0 - 0.20 * _absent_hours)
                if _absent_hours > 8:
                    _decay = max(0.30, _decay)
                _base_mood = g["load_mood"]()
                for _k, _v in _co_mood.items():
                    if isinstance(_v, float):
                        _co_mood[_k] = _v * _decay
                _base_mood.update({k: v for k, v in _co_mood.items() if k in _base_mood and isinstance(v, float)})
                g["save_mood"](g["enrich_mood_state"](_base_mood))
                print(f"[mood_carryover] applied decay={_decay:.2f} absent_hours={_absent_hours:.1f}")
    except Exception as e:
        print(f"[mood_carryover] skipped: {e}")

    print("[startup] step: state file init")
    ACTIVE_PERSON_PATH: Path = g["ACTIVE_PERSON_PATH"]
    SELF_MODEL_PATH: Path = g["SELF_MODEL_PATH"]
    INITIATIVE_STATE_PATH: Path = g["INITIATIVE_STATE_PATH"]
    SESSION_STATE_PATH: Path = g["SESSION_STATE_PATH"]
    EXPRESSION_STATE_PATH: Path = g["EXPRESSION_STATE_PATH"]

    if not ACTIVE_PERSON_PATH.exists():
        g["save_active_person_state"](OWNER_PERSON_ID, source="startup")
    if not SELF_MODEL_PATH.exists():
        g["save_self_model"](g["default_self_model"]())
    if not INITIATIVE_STATE_PATH.exists():
        g["save_initiative_state"](g["default_initiative_state"]())
    if not SESSION_STATE_PATH.exists():
        g["save_session_state"]({
            "total_message_count": 0,
            "session_start_at": g["now_iso"](),
            "last_session_end_at": "",
        })
    else:
        _boot_sess = g["load_session_state"]()
        _boot_sess["session_start_at"] = g["now_iso"]()
        g["save_session_state"](_boot_sess)
    if not EXPRESSION_STATE_PATH.exists():
        g["save_expression_state"](g["default_expression_state"]())

    print("[startup] step: TTS worker (COM-isolated thread)")
    try:
        from brain.tts_worker import get_tts_worker
        _tts_worker = get_tts_worker(g)
        g["_tts_worker"] = _tts_worker
        print(f"[tts_worker] available={_tts_worker.available} voice={_tts_worker.voice_name()}")
    except Exception as e:
        g["_tts_worker"] = None
        print(f"[tts_worker] startup skipped: {e}")

    print("[startup] step: TTS engine (wraps worker)")
    try:
        from brain.tts_engine import TTSEngine
        _tts = TTSEngine()
        g["tts_engine"] = _tts if _tts.is_available() else None
        g["tts_engine_name"] = _tts.engine_name() if _tts.is_available() else "none"
    except Exception:
        g["tts_engine"] = None
        g["tts_engine_name"] = "none"
    g["tts_enabled"] = True  # default ON now that worker is COM-safe

    print("[startup] step: STT engine (Whisper)")
    try:
        from brain.stt_engine import STTEngine
        _stt = STTEngine()
        if _stt.is_available():
            g["stt_engine"] = _stt
            g["_stt_ready"] = True
            print("[stt_engine] Whisper ready — voice input enabled")
        else:
            g["stt_engine"] = None
            g["_stt_ready"] = False
            print("[stt_engine] Whisper unavailable — voice input disabled")
    except Exception as _stt_e:
        g["stt_engine"] = None
        g["_stt_ready"] = False
        print(f"[stt_engine] startup skipped: {_stt_e}")

    print("[startup] step: wake word detector")
    try:
        from brain.wake_word import WakeWordDetector
        def _on_wake_word() -> None:
            g["_stt_listen_requested"] = True
        _wake_detector = WakeWordDetector(g, on_wake=_on_wake_word, base_dir=BASE_DIR)
        _wake_detector.start()
        g["_wake_word_detector"] = _wake_detector
        print(f"[wake_word] detector started backend={_wake_detector._backend}")
    except Exception as e:
        g["_wake_word_detector"] = None
        print(f"[wake_word] startup skipped: {e}")

    print("[startup] step: clap detector")
    try:
        from brain.clap_detector import ClapDetector
        def _on_clap() -> None:
            g["_stt_listen_requested"] = True
        _clap_detector = ClapDetector(g, on_clap=_on_clap)
        _clap_started = _clap_detector.start()
        g["_clap_detector"] = _clap_detector if _clap_started else None
        print(f"[clap_detect] started={_clap_started}")
    except Exception as e:
        g["_clap_detector"] = None
        print(f"[clap_detect] startup skipped: {e}")

    print("[startup] step: LLaVA vision check")
    try:
        from brain.scene_understanding import _pick_llava_model
        _llava_model = _pick_llava_model()
        if _llava_model:
            print(f"[llava] {_llava_model} available — scene understanding active")
            g["_llava_model_name"] = _llava_model
        else:
            print("[llava] no llava model found — scene understanding disabled")
            g["_llava_model_name"] = None
    except Exception as e:
        print(f"[llava] check failed: {e}")
        g["_llava_model_name"] = None

    print("[startup] step: self-revision log")
    try:
        from brain.self_revision import configure as configure_sr
        from pathlib import Path as _P_sr
        _base_for_sr = _P_sr(g.get("BASE_DIR") or ".")
        configure_sr(_base_for_sr)
    except Exception as _sre:
        print(f"[self_revision] configure failed: {_sre!r}")

    print("[startup] step: emotional vocabulary (Ava's coined words)")
    try:
        from brain.emotional_vocabulary import configure as configure_ev
        from pathlib import Path as _P_ev
        _base_for_ev = _P_ev(g.get("BASE_DIR") or ".")
        configure_ev(_base_for_ev)
    except Exception as _eve:
        print(f"[emotional_vocabulary] configure failed: {_eve!r}")

    print("[startup] step: comparative memory (mood snapshots)")
    try:
        from brain.comparative_memory import configure as configure_cm
        from pathlib import Path as _P_cm
        _base_for_cm = _P_cm(g.get("BASE_DIR") or ".")
        configure_cm(_base_for_cm)
    except Exception as _cme:
        print(f"[comparative_memory] configure failed: {_cme!r}")

    print("[startup] step: aesthetic preference (taste accumulator)")
    try:
        from brain.aesthetic_preference import configure as configure_ap
        from pathlib import Path as _P_ap
        _base_for_ap = _P_ap(g.get("BASE_DIR") or ".")
        configure_ap(_base_for_ap)
    except Exception as _ape:
        print(f"[aesthetic_preference] configure failed: {_ape!r}")

    print("[startup] step: curiosity research (queue of things to learn)")
    try:
        from brain.curiosity_research import configure as configure_cr
        from pathlib import Path as _P_cr
        _base_for_cr = _P_cr(g.get("BASE_DIR") or ".")
        configure_cr(_base_for_cr)
    except Exception as _cre:
        print(f"[curiosity_research] configure failed: {_cre!r}")

    print("[startup] step: identity stability (periodic bedrock-vs-narrative audit)")
    try:
        from brain.identity_stability import configure as configure_is
        from pathlib import Path as _P_is
        _base_for_is = _P_is(g.get("BASE_DIR") or ".")
        configure_is(_base_for_is)
    except Exception as _ise:
        print(f"[identity_stability] configure failed: {_ise!r}")

    print("[startup] step: discretion (per-person privacy graph)")
    try:
        from brain.discretion import configure as configure_ds
        from pathlib import Path as _P_ds
        _base_for_ds = _P_ds(g.get("BASE_DIR") or ".")
        configure_ds(_base_for_ds)
    except Exception as _dse:
        print(f"[discretion] configure failed: {_dse!r}")

    print("[startup] step: physical_context (warm weather cache off hot path)")
    try:
        from brain.physical_context import refresh_weather_cache_background
        refresh_weather_cache_background()

        # Periodic refresher — keeps cache warm so deep-path turns never
        # have to wait on wttr.in synchronously. 8min interval beats the
        # 10min TTL with margin.
        import threading as _th_pc
        import time as _t_pc

        def _periodic_weather_refresh() -> None:
            while True:
                try:
                    _t_pc.sleep(8 * 60)
                    refresh_weather_cache_background()
                except Exception:
                    pass

        _wt = _th_pc.Thread(
            target=_periodic_weather_refresh,
            daemon=True,
            name="weather-refresh-loop",
        )
        _wt.start()
    except Exception as _pce:
        print(f"[physical_context] weather warm failed: {_pce!r}")

    print("[startup] step: play (capacity for play, lifecycle-gated)")
    try:
        from brain.play import configure as configure_play
        from pathlib import Path as _P_play
        _base_for_play = _P_play(g.get("BASE_DIR") or ".")
        configure_play(_base_for_play)
    except Exception as _playe:
        print(f"[play] configure failed: {_playe!r}")

    print("[startup] step: honest disagreement (when Ava genuinely diverges)")
    try:
        from brain.honest_disagreement import configure as configure_hd
        from pathlib import Path as _P_hd
        _base_for_hd = _P_hd(g.get("BASE_DIR") or ".")
        configure_hd(_base_for_hd)
    except Exception as _hde:
        print(f"[honest_disagreement] configure failed: {_hde!r}")

    print("[startup] step: active learning (capture factual corrections)")
    try:
        from brain.active_learning import configure as configure_al
        from pathlib import Path as _P_al
        _base_for_al = _P_al(g.get("BASE_DIR") or ".")
        configure_al(_base_for_al)
    except Exception as _ale:
        print(f"[active_learning] configure failed: {_ale!r}")

    print("[startup] step: behavior patterns (observation accumulator)")
    try:
        from brain.behavior_patterns import configure as configure_bp
        from pathlib import Path as _P_bp
        _base_for_bp = _P_bp(g.get("BASE_DIR") or ".")
        configure_bp(_base_for_bp)
    except Exception as _bpe:
        print(f"[behavior_patterns] configure failed: {_bpe!r}")

    print("[startup] step: web search (A7 — connectivity-gated knowledge lookup)")
    try:
        from brain.web_search import configure as configure_ws
        from pathlib import Path as _P_ws
        _base_for_ws = _P_ws(g.get("BASE_DIR") or ".")
        configure_ws(_base_for_ws)
    except Exception as _wse:
        print(f"[web_search] configure failed: {_wse!r}")

    print("[startup] step: handoff (cross-session texture read + threshold watchdog)")
    try:
        from brain.handoff import (
            read_handoff,
            write_handoff_with_summary,
            should_trigger_handoff,
        )
        from pathlib import Path as _P_ho
        _base_for_ho = _P_ho(g.get("BASE_DIR") or ".")
        # Read the prior session's handoff and stash on g for prompt_builder.
        _prior_handoff = read_handoff(_base_for_ho)
        if _prior_handoff:
            g["_prior_handoff"] = _prior_handoff
            print(f"[handoff] loaded prior handoff iso={_prior_handoff.get('iso','?')}")
        else:
            print("[handoff] no prior handoff — first run or never persisted")
        # Initialize per-session counters.
        g["_turns_this_session"] = 0
        g["_last_handoff_turn_count"] = 0

        # 2026-05-08 (per Zeke): replaced 5-min periodic writes with a
        # context-threshold watchdog. The handoff should fire at MEANINGFUL
        # boundaries — when conversation has accumulated enough to be
        # worth summarizing, not on a wallclock cadence. Threshold is
        # turn-count-based (coarse but matches "70% context fill" intent).
        # The watchdog polls every 30s; when threshold hit, fires a rich
        # write that includes the agent's own salient summary.
        import threading as _th_ho
        import time as _t_ho

        def _handoff_threshold_watchdog():
            while True:
                try:
                    _t_ho.sleep(30)
                    if should_trigger_handoff(g):
                        print("[handoff] threshold reached — firing rich write")
                        write_handoff_with_summary(
                            g, _base_for_ho, reason="context_threshold",
                        )
                except Exception:
                    pass

        _ht = _th_ho.Thread(
            target=_handoff_threshold_watchdog,
            daemon=True,
            name="handoff-threshold-watchdog",
        )
        _ht.start()
    except Exception as _hoe:
        print(f"[handoff] startup failed: {_hoe!r}")

    print("[startup] step: skill sandbox (#20 — fail-closed allowlist for auto-skills)")
    try:
        from brain.skill_sandbox import configure as configure_sb
        from pathlib import Path as _P_sb
        _base_for_sb = _P_sb(g.get("BASE_DIR") or ".")
        configure_sb(_base_for_sb)
    except Exception as _sbe:
        print(f"[skill_sandbox] configure failed: {_sbe!r}")

    print("[startup] step: D1 continuity gate (ritual protection — D1 itself NOT shipped)")
    try:
        from brain.continuity_gate import configure as configure_cg, gate_status, is_continuity_allowed
        from pathlib import Path as _P_cg
        _base_for_cg = _P_cg(g.get("BASE_DIR") or ".")
        configure_cg(_base_for_cg)
        # Audit log on every boot — even if D1 isn't shipped, surface gate state
        _gs = gate_status()
        _gs_allowed = bool(_gs.get("overall_allowed"))
        if _gs_allowed:
            print(f"[continuity_gate] WARNING: gate reports allowed=True at startup. D1 substrate (when shipped) would activate. Settling days remaining: {_gs.get('settling_days_remaining')}")
        else:
            _reasons = _gs.get("blocking_reasons") or []
            print(f"[continuity_gate] gate inactive — {len(_reasons)} blocking condition(s)")
    except Exception as _cge:
        print(f"[continuity_gate] configure failed: {_cge!r}")

    print("[startup] step: creative initiative (idea queue + works)")
    try:
        from brain.creative_initiative import configure as configure_ci
        from pathlib import Path as _P_ci
        _base_for_ci = _P_ci(g.get("BASE_DIR") or ".")
        configure_ci(_base_for_ci)
    except Exception as _cie:
        print(f"[creative_initiative] configure failed: {_cie!r}")

    print("[startup] step: async letters (Ava as correspondent)")
    try:
        from brain.async_letters import configure as configure_al
        from pathlib import Path as _P_al
        _base_for_al = _P_al(g.get("BASE_DIR") or ".")
        configure_al(_base_for_al)
    except Exception as _ale:
        print(f"[async_letters] configure failed: {_ale!r}")

    print("[startup] step: counterfactual archive (decision history)")
    try:
        from brain.counterfactual_archive import configure as configure_cf
        from pathlib import Path as _P_cf
        _base_for_cf = _P_cf(g.get("BASE_DIR") or ".")
        configure_cf(_base_for_cf)
    except Exception as _cfe:
        print(f"[counterfactual_archive] configure failed: {_cfe!r}")

    print("[startup] step: daily practice (registered practices + history)")
    try:
        from brain.daily_practice import configure as configure_dp
        from pathlib import Path as _P_dp
        _base_for_dp = _P_dp(g.get("BASE_DIR") or ".")
        configure_dp(_base_for_dp)
    except Exception as _dpe:
        print(f"[daily_practice] configure failed: {_dpe!r}")

    print("[startup] step: anchor moments (persistent episodic memory)")
    try:
        from brain.anchor_moments import configure as configure_am
        from pathlib import Path as _P_am
        _base_for_am = _P_am(g.get("BASE_DIR") or ".")
        configure_am(_base_for_am)
    except Exception as _ame:
        print(f"[anchor_moments] configure failed: {_ame!r}")

    print("[startup] step: topic tabling (cognitive autonomy)")
    try:
        from brain.topic_tabling import configure as configure_tt
        from pathlib import Path as _P_tt
        _base_for_tt = _P_tt(g.get("BASE_DIR") or ".")
        configure_tt(_base_for_tt)
    except Exception as _tte:
        print(f"[topic_tabling] configure failed: {_tte!r}")

    print("[startup] step: plugin manifest registry")
    try:
        from brain.plugin_manifest import configure as configure_pm
        configure_pm()
    except Exception as _pme:
        print(f"[plugin_manifest] configure failed: {_pme!r}")

    print("[startup] step: feature flags (catalog + overrides)")
    try:
        from brain.feature_flags import configure as configure_flags
        from pathlib import Path as _P_ff
        _base_for_ff = _P_ff(g.get("BASE_DIR") or ".")
        configure_flags(_base_for_ff)
    except Exception as _ffe:
        print(f"[feature_flags] configure failed: {_ffe!r}")

    print("[startup] step: provenance graph (claim sourcing)")
    try:
        from brain.provenance import configure_provenance
        from pathlib import Path as _P_pv
        _base_for_pv = _P_pv(g.get("BASE_DIR") or ".")
        configure_provenance(_base_for_pv)
    except Exception as _pve:
        print(f"[provenance] configure failed: {_pve!r}")

    print("[startup] step: person registry (per-person facade)")
    try:
        from brain.person_registry import configure_person_registry
        from pathlib import Path as _P_pr
        _base_for_pr = _P_pr(g.get("BASE_DIR") or ".")
        configure_person_registry(_base_for_pr)
    except Exception as _pre:
        print(f"[person_registry] configure failed: {_pre!r}")

    print("[startup] step: safety / boundary layer (skeleton)")
    try:
        from brain.safety_layer import configure_safety
        configure_safety(g)
    except Exception as _se:
        print(f"[safety_layer] configure failed: {_se!r}")

    print("[startup] step: telemetry (pipeline-stage timing)")
    try:
        from brain.telemetry import configure_telemetry
        from pathlib import Path as _P_tm
        _base_for_tm = _P_tm(g.get("BASE_DIR") or ".")
        configure_telemetry(_base_for_tm)
    except Exception as _te:
        print(f"[telemetry] configure failed: {_te!r}")

    print("[startup] step: app catalog (Steam + Epic library scan)")
    try:
        from brain.app_catalog import build_catalog, needs_rebuild, summary
        from pathlib import Path as _P_ac
        _base_for_ac = _P_ac(g.get("BASE_DIR") or ".")
        if needs_rebuild(_base_for_ac):
            # Run in background — Steam scan can take a few seconds on
            # large libraries; don't block voice-loop bootstrap.
            def _bg_catalog():
                try:
                    s = summary(_base_for_ac)
                    print(f"[app_catalog] before rebuild: {s.get('total_entries')} entries")
                    cat = build_catalog(_base_for_ac)
                    print(f"[app_catalog] rebuilt: {len(cat.get('entries') or [])} entries from {cat.get('sources')}")
                except Exception as e:
                    print(f"[app_catalog] background rebuild error: {e!r}")
            _bg("ava-app-catalog", _bg_catalog)
        else:
            s = summary(_base_for_ac)
            print(f"[app_catalog] cached: {s.get('total_entries')} entries from {s.get('sources')}")
    except Exception as _ace:
        print(f"[app_catalog] startup skipped: {_ace!r}")

    print("[startup] step: scheduler (reminders watcher)")
    try:
        from brain.scheduler import start_watcher as _start_sched
        _start_sched(g)
    except Exception as _se:
        print(f"[scheduler] start failed: {_se!r}")

    print("[startup] step: voice loop")
    try:
        from brain.voice_loop import start_voice_loop
        _vl_ok = start_voice_loop(g)
        if _vl_ok:
            print("[voice_loop] started=True — passive listening active")
        else:
            # Diagnose exactly why voice loop didn't start
            _stt_obj = g.get("stt_engine")
            _tts_obj = g.get("tts_engine")
            _stt_ok = (
                _stt_obj is not None
                and callable(getattr(_stt_obj, "is_available", None))
                and _stt_obj.is_available()
            )
            _tts_ok = (
                _tts_obj is not None
                and callable(getattr(_tts_obj, "is_available", None))
                and _tts_obj.is_available()
            )
            print(
                f"[voice_loop] started=False — "
                f"stt={'ok' if _stt_ok else ('None' if _stt_obj is None else 'unavailable')} "
                f"tts={'ok' if _tts_ok else ('None' if _tts_obj is None else 'unavailable')}"
            )
    except Exception as e:
        print(f"[voice_loop] startup skipped: {e}")

    # ── Milestone 100: calls qwen2.5:14b — background thread, runs once ───────
    print("[startup] step: milestone 100 check (background)")
    try:
        def _bg_milestone():
            try:
                from brain.milestone_100 import run_milestone_if_needed
                run_milestone_if_needed(g)
            except Exception as e:
                print(f"[milestone_100] background error: {e}")
        _bg("ava-milestone-100", _bg_milestone)
    except Exception as e:
        print(f"[milestone_100] dispatch skipped: {e}")

    # ── Background ticks: heartbeat + video capture daemons ───────────────────
    print("[startup] step: background tick threads")
    try:
        from brain.background_ticks import bootstrap_background_ticks
        bootstrap_background_ticks(g)
    except Exception as e:
        print(f"[background_ticks] startup skipped: {e}")

    # Wire the lifecycle bridge so subsystems hooked on
    # HOOK_ON_LIFECYCLE_CHANGE get notified automatically.
    try:
        from brain.hooks import install_lifecycle_bridge, fire, HOOK_ON_STARTUP
        install_lifecycle_bridge()
        # Fire on_startup hooks for any registered subsystems.
        fire(HOOK_ON_STARTUP, g)
    except Exception as _he:
        print(f"[hooks] startup bridge failed: {_he!r}")

    g["_STARTUP_COMPLETE"] = True
    # Transition out of "booting" lifecycle state now that subsystems are up.
    try:
        from brain.lifecycle import lifecycle
        lifecycle.transition("alive_attentive", reason="startup complete")
    except Exception as _le:
        print(f"[lifecycle] transition failed: {_le!r}")
    print("Ava running...")
    print(f"Base dir: {BASE_DIR}")
    print(f"Profiles dir: {g['PROFILES_DIR']}")
    print(f"Memory dir: {g['MEMORY_DIR']}")
    print(f"Self reflection dir: {g['SELF_REFLECTION_DIR']}")
    print(f"Workbench dir: {g['WORKBENCH_DIR']}")
    print(f"Emotion reference: {g['EMOTION_REFERENCE_PATH']}")
    print(f"Active person: {g['get_active_person_id']()}")
