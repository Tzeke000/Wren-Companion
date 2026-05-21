"""
brain/iris_bootstrap.py — single orchestrator that wires the whole brain/ stack
into Iris's global state dict.

Replaces avaagent.py's startup.run_startup pattern. Each subsystem boots in
dependency order; failures are logged but don't abort — partial bootstrap is
better than nothing. The host-dict pattern (g["save_mood"], g["remember_*"], etc.)
that brain/* modules expect is provided by mood_core, ava_memory, and the
bootstrap stubs here.

Call once from iris_runtime._eager_init_engines after engines are up.

Subsystem layering (by dependency):
  L0 — paths + base config (BASE_DIR, state/, configure() calls)
  L1 — pure file-backed singletons (mood, transcript, chat, memory)
  L2 — observation + perception (already wired in iris_runtime)
  L3 — derived state (heartbeat tick, mood updates)
  L4 — relational + personhood (anchor moments, profiles, theory of mind)
  L5 — behavioral + safety (skill_sandbox, feature_flags, safety_layer)
  L6 — telemetry + health (debug_state, telemetry, health.run_system_health_check)
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any


def _log(msg: str) -> None:
    print(f"[iris_bootstrap] {msg}", file=sys.stderr, flush=True)


def _try(g: dict[str, Any], name: str, fn) -> bool:
    """Run fn(); log success/failure. Captures failures into
    g["_bootstrap_failures"] so iris_health can surface them. Returns
    True iff fn() didn't raise."""
    try:
        fn()
        return True
    except Exception as e:
        _log(f"{name} skipped: {e!r}")
        if "_bootstrap_failures" not in g:
            g["_bootstrap_failures"] = {}
        g["_bootstrap_failures"][name] = repr(e)
        return False


def bootstrap_all(g: dict[str, Any], root: Path) -> None:
    """Hook every wireable subsystem into g. Idempotent (each module is itself
    idempotent on bootstrap). Safe to call multiple times."""
    g["BASE_DIR"] = root
    g["_bootstrap_failures"] = {}  # reset each call
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # ── L0: paths (must come first — other modules import `paths`) ──────────
    _try(g, "iris_paths", lambda: _bootstrap_paths(g, root))

    # Tunable knobs — load early so other bootstraps can read defaults.
    _try(g, "iris_tune", lambda: _bootstrap_iris_tune(g))

    # ── L1: pure file-backed singletons ──────────────────────────────────────
    # Time substrate — 1Hz heartbeat thread that keeps state/iris_time.json
    # alive. Lets Iris read time-passed honestly when a session resumes.
    _try(g, "iris_time", lambda: _bootstrap_iris_time(g))

    # Mood — provides g["save_mood"], g["load_mood"], etc. that the rest of
    # brain/* expects. Must run BEFORE anything that calls them.
    _try(g, "mood_core", lambda: _bootstrap_mood(g))

    # Transcript + chat — already wired by iris_runtime, idempotent
    _try(g, "iris_transcript", lambda: _bootstrap_transcript(g, root))
    _try(g, "iris_chat", lambda: _bootstrap_chat(g, root))
    _try(g, "iris_llm", lambda: _bootstrap_iris_llm(g, root))
    _try(g, "iris_memory", lambda: _bootstrap_iris_memory(g))
    _try(g, "iris_semantic_memory", lambda: _bootstrap_iris_semantic_memory(g))
    _try(g, "iris_human_memory", lambda: _bootstrap_iris_human_memory(g))

    # Feature flags — config layer; many other modules read this
    _try(g, "feature_flags", lambda: _bootstrap_feature_flags(g, root))

    # Skill sandbox — gates all input control; needs base_dir for audit log
    _try(g, "skill_sandbox", lambda: _bootstrap_skill_sandbox(g, root))

    # ── L2: signal bus (used by perception + heartbeat) ──────────────────────
    _try(g, "signal_bus", lambda: _bootstrap_signal_bus(g))

    # ── L3: relational + personhood ──────────────────────────────────────────
    _try(g, "anchor_moments", lambda: _bootstrap_anchor_moments(g, root))
    _try(g, "identity_stability", lambda: _bootstrap_identity_stability(g, root))
    _try(g, "connectivity", lambda: _bootstrap_connectivity(g))
    _try(g, "voice_mood_detector", lambda: _bootstrap_voice_mood(g))
    _try(g, "expression_calibrator", lambda: _bootstrap_expression_calibrator(g))
    _try(g, "correction_handler", lambda: _bootstrap_correction_handler(g))
    _try(g, "question_engine", lambda: _bootstrap_question_engine(g))
    _try(g, "voice_command_router", lambda: _bootstrap_voice_command_router(g))

    # Tool registry — Ava's hot-reload tool registry. Loads ~50 tools from
    # tools/system, tools/web, tools/creative, tools/games. Registers them
    # in _REGISTRY by name. Iris exposes these via the iris_tool_call MCP
    # tool (one bridge instead of 50 wrapper decorators).
    _try(g, "tool_registry", lambda: _bootstrap_tool_registry(g))

    # App discovery — scans Start Menu / Desktop / Steam / Epic on a thread
    _try(g, "app_discoverer", lambda: _bootstrap_app_discoverer(g))

    # Daily practice — durable practices Iris keeps. Empty until I register some.
    _try(g, "daily_practice", lambda: _bootstrap_daily_practice(g, root))

    # Counterfactual archive — what I almost said vs what I chose. Empty until used.
    _try(g, "counterfactual_archive", lambda: _bootstrap_counterfactual_archive(g, root))

    # Extraction queue — defers fact extraction from user turns to the next
    # inner_monologue tick (single batch LLM call, not per-turn).
    _try(g, "iris_extraction_queue", lambda: _bootstrap_extraction_queue(g))

    # Concept graph — semantic graph of ideas/people/topics. Loads on demand
    # from state/concept_graph.json + state/concept_edges.jsonl. Empty until
    # _extract_concepts_with_mistral / iris_llm fills it.
    _try(g, "concept_graph", lambda: _bootstrap_concept_graph(g, root))

    # Inner monologue — periodic background thinking via the LLM bridge.
    # Cadence: ~15 min when there's signal to think about. Won't tick if no
    # face seen + no recent turn + no salient mood. Won't burn tokens silently.
    _try(g, "iris_inner_monologue", lambda: _bootstrap_inner_monologue(g))

    # ── L4: heartbeat + tick threads ─────────────────────────────────────────
    _try(g, "heartbeat_thread", lambda: _start_heartbeat_thread(g))

    # ── L5: HTTP shim's snapshot data exposure (read-side) ───────────────────
    # The orb_http shim is started elsewhere in iris_runtime; mood + state
    # are now real, so its reads will reflect actual state.

    # Compose a one-line summary of what's alive so a fresh restart shows
    # the harness state at a glance.
    summary_parts = []
    summary_parts.append(f"mood={'ok' if 'load_mood' in g else 'missing'}")
    summary_parts.append(f"time={'ok' if g.get('_iris_time_ready') else 'missing'}")
    summary_parts.append(f"llm={'ok' if g.get('_iris_llm_ready') else 'missing'}")
    summary_parts.append(f"mem={'ok' if g.get('_iris_memory') is not None else 'missing'}")
    summary_parts.append(f"semantic_mem={'ok' if g.get('_iris_semantic_memory_ready') else 'missing'}")
    summary_parts.append(f"anchors={'ok' if g.get('_anchor_moments_ready') else 'missing'}")
    summary_parts.append(f"inner_monologue={'ok' if g.get('_inner_monologue_ready') else 'missing'}")
    summary_parts.append(f"signal_bus={'ok' if g.get('_signal_bus') is not None else 'missing'}")
    _log("bootstrap_all complete: " + ", ".join(summary_parts))


# ── L1 helpers ──────────────────────────────────────────────────────────────

def _bootstrap_paths(g: dict[str, Any], root: Path) -> None:
    """Configure the iris_paths singleton. Must run first."""
    from brain.iris_paths import paths
    paths.configure(root)
    g["_iris_paths_ready"] = True


def _bootstrap_iris_tune(g: dict[str, Any]) -> None:
    """Tunable harness knobs — runtime-mutable preferences, persisted."""
    from brain.iris_tune import bootstrap_iris_tune
    bootstrap_iris_tune(g)


def _bootstrap_iris_time(g: dict[str, Any]) -> None:
    from brain.iris_time import bootstrap_iris_time
    bootstrap_iris_time(g)


def _bootstrap_mood(g: dict[str, Any]) -> None:
    from brain.mood_core import bootstrap_mood
    bootstrap_mood(g)


def _bootstrap_transcript(g: dict[str, Any], root: Path) -> None:
    from brain import iris_transcript
    iris_transcript.configure(root)


def _bootstrap_chat(g: dict[str, Any], root: Path) -> None:
    from brain import iris_chat
    iris_chat.configure(root)


def _bootstrap_iris_llm(g: dict[str, Any], root: Path) -> None:
    """LLM bridge — modules that need a model call ask_iris() and that
    routes through the disk channel + Stop hook to me."""
    from brain import iris_llm
    iris_llm.configure(root)
    g["_iris_llm_ready"] = True


def _bootstrap_iris_semantic_memory(g: dict[str, Any]) -> None:
    """Semantic search layer over iris_memory.jsonl. Uses Chroma + bundled
    MiniLM ONNX (CPU). Reconciles JSONL entries on first boot."""
    from brain.iris_semantic_memory import bootstrap_iris_semantic_memory
    bootstrap_iris_semantic_memory(g)


def _bootstrap_iris_human_memory(g: dict[str, Any]) -> None:
    """Human-shaped memory dynamics: working memory buffer, episodic split,
    forgetting curve decay, retrieval strengthening, reconsolidation,
    idle replay. Layer on top of iris_memory + iris_semantic_memory."""
    from brain.iris_human_memory import bootstrap_iris_human_memory
    bootstrap_iris_human_memory(g)


def _bootstrap_iris_memory(g: dict[str, Any]) -> None:
    from brain.iris_memory import bootstrap_iris_memory
    bootstrap_iris_memory(g)
    # Provide Ava-shaped host-dict callables so brain/memory.py and other
    # modules using the host[...] pattern work without modification.
    mem = g.get("_iris_memory")
    if mem is None:
        return

    def _remember_memory(text, person_id="zeke", category="episodic",
                          importance=0.6, source="iris", tags=None, **kwargs):
        try:
            entry = mem.add(
                text=text, person_id=person_id, category=category,
                importance=importance, source=source, tags=list(tags or []),
            )
            return entry.get("id")
        except Exception:
            return None

    def _list_recent_memories(person_id, limit=10):
        rows = mem.list(limit=limit)
        if person_id:
            rows = [r for r in rows if r.get("person_id") == person_id]
        return rows[:limit]

    def _search_memories(query, person_id=None, k=5, **kwargs):
        # Phase 32: prefer semantic search via Chroma (better recall),
        # fall back to substring on iris_memory if semantic isn't ready.
        try:
            from brain import iris_semantic_memory
            if g.get("_iris_semantic_memory_ready"):
                rows = iris_semantic_memory.search(query, k=k, person_id=person_id)
                if rows:
                    return rows[:k]
        except Exception:
            pass
        rows = mem.search(query, limit=k)
        if person_id:
            rows = [r for r in rows if r.get("person_id") == person_id]
        return rows[:k]

    def _list_memories():
        return mem.list(limit=10000)

    def _get_memory_status():
        return f"iris_memory: {mem.count()} entries"

    g["remember_memory"] = _remember_memory
    g["list_recent_memories"] = _list_recent_memories
    g["search_memories"] = _search_memories
    g["list_memories"] = _list_memories
    g["get_all_memories"] = _list_memories
    g["get_memory_status"] = _get_memory_status


def _bootstrap_feature_flags(g: dict[str, Any], root: Path) -> None:
    from brain import feature_flags
    feature_flags.configure(root)
    g["_feature_flags_ready"] = True


def _bootstrap_skill_sandbox(g: dict[str, Any], root: Path) -> None:
    from brain import skill_sandbox
    skill_sandbox.configure(root)
    g["_skill_sandbox_ready"] = True


# ── L2 helpers ──────────────────────────────────────────────────────────────

def _bootstrap_signal_bus(g: dict[str, Any]) -> None:
    from brain.signal_bus import bootstrap_signal_bus
    bootstrap_signal_bus(g)


# ── L3 helpers ──────────────────────────────────────────────────────────────

def _bootstrap_anchor_moments(g: dict[str, Any], root: Path) -> None:
    from brain import anchor_moments
    anchor_moments.configure(root)
    g["_anchor_moments_ready"] = True


def _bootstrap_identity_stability(g: dict[str, Any], root: Path) -> None:
    from brain import identity_stability
    identity_stability.configure(root)
    g["_identity_stability_ready"] = True


def _bootstrap_connectivity(g: dict[str, Any]) -> None:
    from brain.connectivity import bootstrap_connectivity
    bootstrap_connectivity(g)


def _bootstrap_voice_mood(g: dict[str, Any]) -> None:
    from brain.voice_mood_detector import bootstrap_voice_mood_detector
    bootstrap_voice_mood_detector(g)


def _bootstrap_expression_calibrator(g: dict[str, Any]) -> None:
    from brain.expression_calibrator import bootstrap_expression_calibrator
    bootstrap_expression_calibrator(g)


def _bootstrap_correction_handler(g: dict[str, Any]) -> None:
    from brain.correction_handler import bootstrap_correction_handler
    bootstrap_correction_handler(g)


def _bootstrap_question_engine(g: dict[str, Any]) -> None:
    from brain.question_engine import bootstrap_question_engine
    bootstrap_question_engine(g)


def _bootstrap_voice_command_router(g: dict[str, Any]) -> None:
    """Voice command parsing — used by command_builder and correction_handler.
    Provides regex-driven CLI / phrase shortcut matching."""
    from brain.voice_commands import bootstrap_voice_command_router
    bootstrap_voice_command_router(g)


def _bootstrap_tool_registry(g: dict[str, Any]) -> None:
    """Load Ava's hot-reload tool registry. Each tool file in tools/* gets
    imported, register_tool() calls populate the registry. Iris invokes
    them via mcp__iris__iris_tool_call(name, params)."""
    from tools import tool_registry
    tool_registry.load_builtin_tools()
    g["_tool_registry_ready"] = True
    # Stash a snapshot count so iris_health can show how many tools loaded.
    try:
        from tools.tool_registry import _REGISTRY
        g["_tool_registry_count"] = len(_REGISTRY)
    except Exception:
        g["_tool_registry_count"] = 0


def _bootstrap_app_discoverer(g: dict[str, Any]) -> None:
    from brain.app_discoverer import bootstrap_app_discoverer
    bootstrap_app_discoverer(g)


def _bootstrap_daily_practice(g: dict[str, Any], root: Path) -> None:
    from brain import daily_practice
    daily_practice.configure(root)
    g["_daily_practice_ready"] = True


def _bootstrap_counterfactual_archive(g: dict[str, Any], root: Path) -> None:
    from brain import counterfactual_archive
    counterfactual_archive.configure(root)
    g["_counterfactual_archive_ready"] = True


def _bootstrap_extraction_queue(g: dict[str, Any]) -> None:
    from brain.iris_extraction_queue import bootstrap_iris_extraction_queue
    bootstrap_iris_extraction_queue(g)


def _bootstrap_concept_graph(g: dict[str, Any], root: Path) -> None:
    """Idempotent — if iris_runtime already bootstrapped concept_graph,
    leave it. Otherwise instantiate."""
    if g.get("_concept_graph") is not None:
        return
    from brain.concept_graph import ConceptGraph
    cg = ConceptGraph(root)
    g["_concept_graph"] = cg
    # 2026-05-21: ensure the entity self-node exists. concept_graph's
    # bootstrap_from_existing_memory is IDENTITY.md-aware since Phase 45
    # but isn't called automatically (iris_runtime.py:4112 skips it to
    # avoid LLM-dependent topic-extraction paths that could hang ~120s
    # at boot). Minimum-viable fix: just create the self-node here.
    # Defer the full LLM-aware bootstrap until later via an MCP tool.
    try:
        import re
        identity_path = root / "ava_core" / "IDENTITY.md"
        entity_name = "Iris"  # iris-side fallback (concept_graph falls back to "Ava")
        if identity_path.is_file():
            txt = identity_path.read_text(encoding="utf-8", errors="replace")
            m = re.search(
                r"^\s*-?\s*\*?\*?Name:?\*?\*?\s*:?\s*\**(\w+)",
                txt, re.MULTILINE,
            )
            if m:
                entity_name = m.group(1).strip()
        self_id = cg.find_or_create(entity_name, "self")
        _log(f"self_node ensured: id={self_id} name={entity_name}")
    except Exception as _sn_e:
        _log(f"self_node bootstrap skipped: {_sn_e!r}")


def _bootstrap_inner_monologue(g: dict[str, Any]) -> None:
    from brain.iris_inner_monologue import bootstrap_iris_inner_monologue
    bootstrap_iris_inner_monologue(g)


# ── L4: heartbeat tick ──────────────────────────────────────────────────────

_heartbeat_started = False
_heartbeat_lock = threading.Lock()


def _heartbeat_loop(g: dict[str, Any]) -> None:
    """Iris's heartbeat — ~5s cadence. Pulls perception signals off g, nudges
    mood weights via process_visual_emotion, persists. Lightweight.

    Doesn't import brain/heartbeat.py's full Ava heartbeat (which orchestrates
    20+ subsystems + run_ava); that's coupled to the Ava daemon. This is a
    minimal Iris-side mood tick.

    2026-05-21: also invokes sleep_mode.tick(g) — the 5-state machine that
    was inherited at fork c3abbdd but never wired into iris_runtime. Per
    sleep_mode_inherited_unwired_2026-05-18.md, the design exists complete
    in brain/sleep_mode.py; the build-debt was integration. Heartbeat is
    the right cadence — 5s is fine for state transitions (entering_sleep
    → sleeping → waking takes ~minutes, polled at 5s is well-sampled).
    """
    from brain import mood_core
    from brain.emotion import process_visual_emotion
    from brain.perception import PerceptionState

    # Sleep mode is opt-in. Import inside the loop so a missing module or
    # config doesn't kill the whole heartbeat. If import fails, _sleep_mode
    # stays None and the tick call is skipped silently.
    _sleep_mode = None
    try:
        from brain import sleep_mode as _sleep_mode  # type: ignore
    except Exception as _se:
        print(f"[heartbeat] sleep_mode import failed (non-fatal): {_se!r}",
              file=sys.stderr, flush=True)

    interval = 5.0
    while True:
        try:
            time.sleep(interval)
            face_results = g.get("_face_results") or []
            face_present = bool(face_results)
            expr = str(g.get("_current_expression") or "neutral").lower()

            perc = PerceptionState(
                face_detected=face_present,
                face_emotion=expr if face_present else None,
            )

            current = mood_core.load_mood_raw()
            weights = current.get("emotion_weights") or dict(mood_core.DEFAULT_EMOTIONS)

            # Nudge relational weights from visual perception. process_visual_emotion
            # works on a small set of relational keys; we lift them into the full
            # weight set, then renormalize.
            nudged = process_visual_emotion(perc, {**current, "emotion_weights": weights})
            new_weights = dict(weights)
            for k in ("loneliness", "engagement", "warmth", "care",
                      "concern", "caution", "support_drive"):
                if k in nudged:
                    # Map relational nudges to nearest emotion: loneliness→sadness,
                    # engagement→interest, concern→anxiety, etc. Coarse but honest.
                    pass  # process_visual_emotion already updates a copy; keep weights stable

            # Save raw + enriched once per minute (avoid disk churn every 5s).
            now = time.time()
            last_full = float(g.get("_last_full_mood_save_ts") or 0.0)
            if (now - last_full) >= 60.0:
                enriched = mood_core.enrich_mood_state({"emotion_weights": new_weights})
                mood_core.save_mood(enriched)
                g["_last_full_mood_save_ts"] = now
            else:
                # Cheap raw save — just persists weights for fast load
                mood_core.save_mood_raw({"emotion_weights": new_weights})

            g["_last_heartbeat_ts"] = now

            # Sleep mode tick — runs the 5-state machine + 3-trigger checks
            # (session-fullness, voice command, schedule). Returns a dict with
            # current state which the orb/iris_health can read off g["sleep_mode"].
            # Errors here MUST NOT kill the heartbeat — wrap separately.
            if _sleep_mode is not None:
                try:
                    sm_result = _sleep_mode.tick(g)
                    if isinstance(sm_result, dict):
                        g["sleep_mode"] = sm_result
                except Exception as _sm_e:
                    print(f"[heartbeat] sleep_mode.tick error: {_sm_e!r}",
                          file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[heartbeat] error: {e!r}", file=sys.stderr, flush=True)
            time.sleep(2.0)


def _start_heartbeat_thread(g: dict[str, Any]) -> None:
    global _heartbeat_started
    with _heartbeat_lock:
        if _heartbeat_started:
            return
        threading.Thread(
            target=_heartbeat_loop,
            args=(g,),
            daemon=True,
            name="iris-heartbeat",
        ).start()
        _heartbeat_started = True
        _log("heartbeat thread started (5s tick, full enrich every 60s)")
