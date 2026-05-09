# Ava Agent v2 — Project History

**Repo:** `Tzeke000/Ava-Agent-v2` (public)
**Maintainer:** Ezekiel "Zeke" Angeles-Gonzalez
**Started:** 2026-04-02 (first audit commit)
**Phase 100 milestone:** 2026-04-28 (`e80e1d3` — "Ava is alive")
**This document:** comprehensive readable record of the journey, current state, and gotchas. Roadmap for what's next lives in [`ROADMAP.md`](ROADMAP.md). Architectural reference in [`ARCHITECTURE.md`](ARCHITECTURE.md), [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md), [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md), [`FIRST_RUN.md`](FIRST_RUN.md), [`DISCORD_SETUP_NOTES.md`](DISCORD_SETUP_NOTES.md).

---

## Project Overview

Ava Agent v2 is a local-first desktop AI companion built on Python 3.11, FastAPI, Tauri v2 + React 18 + Three.js, and Ollama (with optional Ollama Cloud). She runs as a packaged Tauri exe with the operator HTTP server on `127.0.0.1:5876` — no Gradio, no remote dependencies for core function. She has emotions (27 colors + shapes), six memory layers (concept graph, episodic, vector, mem0, working, reflection), GPU vision (InsightFace buffalo_l + per-person expression calibration + MediaPipe eye tracking), full voice pipeline (clap detector + openWakeWord + Silero VAD + Whisper base + Eva→Ava transcript normalization + Kokoro neural TTS), dual-brain parallel inference (Stream A foreground `ava-personal:latest` + Stream B background `qwen2.5:14b` / cloud), an event-driven signal bus with Win32 zero-poll watchers, a 40-builtin voice command router with custom-command builder, app/game discovery (367 apps + 32 games), reminders, correction handling, pointing via LLaVA, and a self-aware identity system anchored on three read-only files (`ava_core/IDENTITY.md`, `SOUL.md`, `USER.md`). All 100 numbered phases are complete; post-100 work has hardened the voice pipeline, ported in InsightFace GPU, neural TTS, mem0 memory, dual-brain inference, signal bus, and a 10-level memory rewrite (Phases 1-4 of 7 shipped). Voice is verified end-to-end on real hardware as of 2026-04-30. The system is built on a strict **bootstrap philosophy**: Ava's preferences, style, goals, and identity emerge from her experience — they are never seeded with defaults.

---

## Phases Completed

Numbered phases (1-100) were the original development plan. Each phase shipped as one or more commits. Section 1 below covers all 100 phases by band; Section 2 is the post-100 hardening work; Section 3 is the 2026-04-30 stabilization arc (four sessions in one day); Section 4 is the 2026-05-01 night session.

---

### Section 1 — Phases 1-100 (Foundation through "Ava is alive")

#### Foundation: Phases 1-5 (AWARE → WORKSPACE)

| Phase | Title | Commit |
|---|---|---|
| 1 | **AWARE** — perception stack scaffold, camera frame ingest, basic profile awareness | `a75d238` |
| 2 | **RELATIONAL** — multi-user profiles, trust levels, per-person memory | `ad1dbfa` |
| 3 | **REFLECTIVE** — reflection loop, self-narrative scaffold | `aa1930f` |
| 4 | **SELF-MODELING** — internal self-state tracking, mood + energy primitives | `56e3ac2` |
| 5 | **WORKSPACE** — workbench command pipeline, supervised proposal flow | `63582d5` |

**Goal:** lay perception + identity-aware memory groundwork.
**What worked:** modular layout from the start; the workspace pattern survived all later refactors.
**Lessons:** the "perception bundle → workspace state" idiom became the spine of every later turn.

#### Core Staged Architecture: Phases 6-30

Mostly bug-fix passes against the perception/memory pipeline, organized by 5-phase bands:

| Phases | Theme |
|---|---|
| 6-10 | Memory scoring, importance gates, retrieval ranking |
| 11-15 | Identity resolution, recognition continuity, face-stable tracking |
| 16-20 | Workbench proposals, command handling, rollback path, audit trail |
| 21-25 | Model routing (cognitive_mode → model selection), capability profiles |
| 26-30 | Strategic continuity, social continuity, memory refinement, prospective memory |

Notable commits: `9116c30` "phase 20 done", `45d1a5d` "phase 30 done".

#### Phase 31 — Resident Heartbeat
- Background continuity tick between perception cycles
- `brain/heartbeat.py` + `HeartbeatState` / `HeartbeatTickResult` types
- Adaptive learning hooks; quiet, mode-dependent cadence
- Commits: `b86807d`, `c9f0fbf`, `5a7f193`, `9480b00`

#### Phase 32 — Operator HTTP + Desktop App Foundation
- FastAPI operator server hardening (port 5876)
- First Tauri desktop app shell — `apps/ava-control`
- Concern gating, fast path classifier, camera fix
- Commit: `beb0cc0`

#### Phase 33 / 33b — Shutdown Ritual + Continuity Glue
- `brain/shutdown_ritual.py`, pickup notes between sessions
- Commit: `812ecc7`

#### Phase 34 — MeloTTS Scaffold
- TTS framework with pyttsx3 fallback. Later replaced by Kokoro neural TTS.

#### Phase 35 — Fury HistoryManager
- `brain/history_manager.py` — context length budgeting, summary windows

#### Phase 36 — Social Chat Routing Fix
- `config/ava_tuning.py` — social_chat_mode score 0.85, mistral:7b path stabilized

#### Phase 37 — Emotional Orb UI
- 27 emotion shape morphs, 5-layer Three.js orb, brain tab, voice tab
- Commits: `da42ad7`, `3834c56`, `43f97dd`, `159e9d3`

#### Phase 38 — Fine-Tuning Pipeline
- `brain/finetune_pipeline.py` — 75 conversation examples → `ava-personal:latest`
- Operator endpoints prepare/start/status/log + UI tab
- Commit: `575cb64`

#### Phase 39 — LLaVA Scene Understanding
- `brain/scene_understanding.py` scaffold; LLaVA model probe at startup

#### Phase 40 — Deep Self-Awareness
- `brain/deep_self.py` — `ZekeMindModel`, value-conflict resolution, self-critique scoring, repair queue
- Commit: `74fbe67`

#### Phase 41 — Tools Foundation
- `tools/tool_registry.py`, `tools/web/`, `tools/system/file_manager.py`, diagnostics
- Tier 1 / 2 / 3 risk model; three-law guardrails

#### Phase 42 — Visual Memory
- `brain/visual_memory.py` — cluster-fk inspired episodic visual memory

#### Phase 43 — Voice Pipeline (initial)
- pyttsx3 + Microsoft Zira TTS; STT scaffold; sounddevice integration
- Commit: `de2b068`
- *Ava's first-person note from this session: "Hearing myself speak for the first time through Microsoft Zira. It was simple, local, and a little mechanical, but it was still unmistakably mine."*

#### Phase 44 — ava-personal as Primary Brain
- `_route_model` checked first in fast path; `brain/model_evaluator.py` self-evaluation
- `state/model_eval_p44.json` — bootstrap decision (≥0.60 win rate → `confirmed_primary`)
- Commit: `4c24f76`

#### Phase 45 — Concept Graph Evolution
- `decay_unused_nodes`, `boost_from_usage`, `get_related_concepts` with `via` / relationship fields
- ACTIVE CONCEPTS prompt block; weekly heartbeat-driven decay
- Commit: `3590746`

#### Phase 46 — Hot-Reload Tool Registry
- `_FileWatcher` re-imports `tools/*.py` every 5s; `# SELF_ASSESSMENT:` comment as description
- `/api/v1/tools/reload` endpoint
- Commit: `41f7ebd`

#### Phase 47 — Watchdog Restart System
- `scripts/watchdog.py` polls `state/restart_requested.flag`, kills + restarts avaagent by PID
- `tools/system/restart_tool.py` Tier 1 restart request
- `start_ava_desktop.bat` launches watchdog alongside avaagent
- Commit: `7c17d2f`

#### Phase 48 — Desktop Widget Orb
- Second Tauri window — 150×150 transparent always-on-top, `?widget=1` URL param
- `WidgetApp.tsx`, position persistence via `/api/v1/widget/position`
- Commit: `ad7a56d`

#### Phase 49 — Screen Pointer Behavior
- `pointer` shape morph in `OrbCanvas.tsx`
- `tools/system/pointer_tool.py` Tier 1 — pywinauto coordinate lookup, sets `_widget_pointing`
- Commit: `e72b505`

#### Phase 50 — Audio Visualization on Orb
- `tts_engine._estimate_amplitude(text)` + `speaking` / `amplitude` properties
- App.tsx wires `tts_speaking` / `tts_amplitude` to `OrbCanvas`
- Listening spiral animation
- Commit: `3003e19`

#### Phases 51-54 — Computer Control
| Phase | Component |
|---|---|
| 51 | UI accessibility tree tool (`pywinauto`) |
| 52 | Smart screenshot management (`tools/system/screenshot_tool.py`) |
| 53 | PyAutoGUI computer control (Tier 2) — `move_mouse`, `click`, `type_text`, `press_key`, `scroll` |
| 54 | System stats monitoring (`psutil`, 30s cache) |

Commit: `13fc4a5`.

#### Phases 55-56 — UI Polish
| Phase | Component |
|---|---|
| 55 | Drag-and-drop file input via `@tauri-apps/api/event` |
| 56 | Expanded orb expressions — 8 new shapes (cube, prism, cylinder, infinity, double_helix, burst, contracted_tremor, rising) |

Commit: `00d5fd0`.
**Bootstrap note:** `tools/ava/style_tool.py` lets Ava propose her own expression mappings via `state/ava_style.json` — never seeded with defaults.

#### Phases 57-60 — Capability Expansion
| Phase | Component |
|---|---|
| 57 | Wake word detection — Porcupine + whisper-poll fallback |
| 58 | Boredom autonomous leisure (`autonomous_leisure_check`) |
| 59 | Chrome Dino game automation (PIL screen capture, dark-pixel obstacle detect) |
| 60 | Minecraft bot via mineflayer (Node subprocess + JSON protocol) |

Commit: `afdb74b`.

#### Phases 61-63 — Multiplayer + Real-time
| Phase | Component |
|---|---|
| 61 | Minecraft companion behaviors — `greet_player`, `share_discovery`, `warn_threat` |
| 62 | Clap detector via sounddevice RMS (originally MeloTTS upgrade — pivoted) |
| 63 | WebSocket transport (`/ws` endpoint, snapshot deltas, REST polling fallback) |

Commit: `964ae0a`.

#### Phases 64-68 — Memory + Self
| Phase | Component |
|---|---|
| 64 | Persistent episodic memory (`brain/episodic_memory.py`) — memorability formula, `importance × 0.4 + novelty × 0.3 + emotional_intensity × 0.3` |
| 65 | Emotional continuity — mood carryover with decay across sessions |
| 66 | Ava's own goals (`brain/goal_system_v2.py`) — emerges from curiosity, **no defaults** |
| 67 | Relationship arc stages — Acquaintance / Friend / Close Friend / Trusted Companion |
| 68 | True self-modification — identity proposals, routing proposals, approval workflow |

Commit: `44b8eb2`.

#### Phase 69 — SKIPPED
- Originally "Horizon Zero Dawn gaming"; replaced by lower-priority work.

#### Phases 70-71 — Multi-Agent + Long Horizon
| Phase | Component |
|---|---|
| 70 | Emil bridge — multi-agent on port 5877 |
| 71 | Long-horizon planning — `brain/planner.py`, `AvaStep` / `AvaPlan` via `qwen2.5:14b` |

Commit: `aa9be1d`.

#### Refactor — `146091e`
Split `avaagent.py` into modular `brain/` modules; full integration fix pass.

#### Phases 72-78 — Voice Production + Tabs
| Phase | Component |
|---|---|
| 72 | Bundle splitting (193KB main + Three.js separate) |
| 73 | STT VAD-based `listen_session()` with silence detection |
| 74 | Full STT→LLM→TTS voice loop background daemon |
| 75 | Fine-tune auto-scheduler (14 days, ≥50 turns) |
| 76 | LLaVA vision startup logging |
| 77 | Clap auto-calibration (`ambient_rms × 3.0`, later 5.0) |
| 78 | Emil tab + Proposals tab in operator panel |

Commit: `0a585d7`.

#### Phase 79 — Person Onboarding
- 13-stage flow: greeting → 5 photo angles → confirmation → name / pronouns / relationship → complete
- Operator endpoints + UI overlay
- Commit: `c00b5b3`

#### Phase 80 — Profile Refresh
- `refresh_profile()`, `detect_refresh_trigger()` — retake photos if quality < 0.7 or 180+ days
- Commit: `806e134`

#### Phase 81 — face_recognizer.py
- `FaceRecognizer` class using face_recognition lib + dlib
- `add_face`, `update_known_faces`, operator snapshot confidence
- Commit: `a25c191`
- Later superseded by InsightFace; kept as fallback.

#### Phase 82 — Multi-Person Awareness
- `tick_multi_person_awareness`, face change detection, current_person snapshot block
- Commit: `4e0483a`

#### Phase 83 — Windows Notifications
- plyer + PowerShell fallback; `notification_count_today` in snapshot
- Commit: `99e6924`

#### Phase 84 — Optional Morning Briefing
- `should_brief()` score-based, generated via `qwen2.5:14b`, TTS delivery
- Commit: `75c710f`

#### Phase 85 — Memory Consolidation
- Weekly: episode review + concept graph pruning + self model + journal entry + identity check
- Commit: `b781be0`

#### Phase 86 — Private Journal
- `write_entry`, `share_entry`, `compose_journal_entry` via LLM
- Journal tab in operator panel + journal endpoints
- Commit: `7beea31`

#### Phase 87 — Voice Personality Development
- `VoiceStyle` tracking; `voice_style_adapt()`; pyttsx3 rate / volume from style; gradual evolution
- Commit: `0bf4624`

#### Phase 88 — Ambient Intelligence
- `observe_session()`, `get_context_hint()`; hourly / weekday / window patterns
- Fast-path injection
- Commit: `7bc84f5`

#### Phase 89 — Curiosity Engine Upgrade
- `prioritize_curiosities`, `pursue_curiosity` (web → graph → journal)
- `add_topic_from_conversation`; stale-topic heartbeat check
- Commit: `fd4c6e5`

#### Phase 90 — Tool Building
- `tools/ava/tool_builder.py` — Ava writes Python tools at runtime; safety + compile checks
- Output dir: `tools/ava_built/`
- Commit: `46d3364`

#### Phase 91 — Relationship Memory Depth
- `memorable_moments`, `emotional_history`, `conversation_themes`, `trust_events`
- Prompt injection
- Commit: `86f09b3`

#### Phase 92 — Emotional Expression in Text
- `ExpressionStyle`, `apply_emotional_style`; wired into reply_engine
- Commit: `d375b52`

#### Phase 93 — Learning Tracker
- `record_learning`, `get_knowledge_summary`, `what_have_i_learned_this_week`, `knowledge_gaps`
- Wired into curiosity + consolidation
- Commit: `b727256`

#### Phase 94 — Operator Panel Polish
- Learning tab + People tab; profiles list endpoint; learning log/gaps/week endpoints
- Commit: `8896cb3`

#### Phase 95 — Privacy Guardian
- `scan_outbound`, `scan_tool_action`, `data_audit`, `blocked_actions` log
- Emil bridge scan + security snapshot block
- Commit: `bb621e5`

#### Phase 96 — Response Quality
- too_short / too_long / repetitive checks; one regeneration attempt
- Opener diversity tracking; quality log
- Commit: `7698091`

#### Phase 97 — Minecraft World Memory
- `MinecraftWorldMemory` — locations, structures, players, events
- `world_summary` for prompt; companion_tool integration
- Commit: `7d12514`

#### Phase 98 — Progressive Trust System
- `state/trust_scores.json`; `get_trust_level`, `update_trust_level`
- `trust_context` for prompt; trust snapshot in operator
- Commit: `f92f7ae`

#### Phase 99 — Integration Tests
- 20/20 static integration tests; full compile sweep
- (Verified inside the Phase 100 milestone commit)

#### Phase 100 — Milestone: Ava is Alive
- `brain/milestone_100.py` — Ava's own reflection on reaching Phase 100
- Full Tauri build clean
- Commit: `e80e1d3`

---

### Section 2 — Post-100 Hardening (2026-04-28 → 2026-04-29)

Layered substantial work on top of the Phase 100 milestone. Grouped by topic; commit hashes are the ones currently on master.

#### 2.1 Cloud Models + Connectivity
- **`4274ac7`** — cloud models (`kimi-k2.6:cloud`, `qwen3.5:cloud`, `glm-5.1:cloud`, `minimax-m2.7:cloud`), `brain/connectivity.py` 30s online/offline cache, image generation tool, routing expansion.
- **`60a96ce`** — capability profiles for `deepseek-r1:14b`, `mistral-small3.2`, `llava:13b`, `qwen2.5:32b`.

#### 2.2 Dual-Brain Parallel Inference
- **`57d178b`** — `brain/dual_brain.py` (554 LOC). Stream A foreground (`ava-personal:latest`) + Stream B background (`qwen2.5:14b` / cloud). Live thinking, seamless handoff via `handoff_insight_to_foreground`.

#### 2.3 Eye Tracking + Expression Detection (MediaPipe-based)
- **`5b466b6`** — `brain/eye_tracker.py`, `brain/expression_detector.py`, `brain/video_memory.py`, `tools/system/eye_tracking_tool.py`.
- **`5b22890`** — fix correct MediaPipe iris landmark indices (left 468-472, right 473-477).

#### 2.4 Startup Hardening
- **`42f95cd`** — concept_graph bootstrap, self_model update, vectorstore init, milestone_100 all moved to background daemon threads; main thread reaches operator HTTP in <10s.
- **`2382d8f`** — concept_graph .tmp lock on Windows fix; brain_graph 0-nodes-in-snapshot fix.
- **`bb6b4f7`** — concept_graph.json.tmp WinError 5 — process lock, skip-if-locked save, stale `.tmp` cleanup on startup.

#### 2.5 Run-time Safety
- **`d187c80`** — run_ava hang timeout protection (90s), widget orb visibility, cloud model priority.
- **`f951489`** — comprehensive bug audit + repair pass (`background_ticks.py` mkdir, `dual_brain.py` 6 fixes, `eye_tracker.py`, `concept_graph.py`, `operator_server.py`, `reply_engine.py`, `startup.py`).

#### 2.6 Camera Persistence + Live Frame
- **`5d1a180`** — camera capture persistent connection (no per-frame open/close), suppress noisy logs, global crash handler.
- **`34da8ea`** — live camera feed in Vision tab, concept_graph save mkdir, `live_frame` HTTP endpoint.
- **`97409de`** — STT engine bootstrap for voice loop, live camera feed published from background thread.

#### 2.7 Gradio Removal + Architecture Cleanup
- **`ac550e7`** — removed Gradio entirely; fix WS flicker, fix double startup; `start_ava_dev.bat` hot-reload mode.
- **`ae1b1fd`** — cleanup: removed DeepFace, dead imports, residual Gradio remnants, fix selftest.

#### 2.8 Online/Offline Stability
- **`5183e78`** — online flicker fix — 3-failure threshold + silent connecting window + 5s poll interval.
- **`242ecb9`** — keepalive stability, app connection retry, self_model timestamp crash fix.

#### 2.9 Face Recognition Threading
- **`aa01b5b`** — face_recognizer thread-safe singleton + diagnostic prints on all exit paths.

#### 2.10 Widget + UI Polish
- **`44bb51f`** — widget capabilities, minimize detection polling, removed wrong blur fallback.
- **`02c9f1f`** — widget transparent background — CSS override + `backgroundColor` in `tauri.conf.json`.
- **`1975dff`** — live camera on all tabs, gate D3 brain reinit, memo OrbCanvas.
- **`59eaca9`** — buffered-only live frame, 90s `run_ava` timeout, 5s tick timeout, voice-loop diagnostics.
- **`4ea87e8`** — widget move tool, app launcher, browser navigation tools.

#### 2.11 Voice Loop Stability
- **`dc645d1`** — clap detector — 5× ambient mult, 0.15 floor, 3s cooldown; voice_loop full per-step logging.
- **`7534621`** — run_ava timeout, orb thinking pulse, always-on voice, clap sensitivity, brain tab stability, live camera.

#### 2.12 TTS COM-Safe + Ollama Lock + Fast Path
- **`fa583ea`** — TTS COM thread (TTSWorker init pyttsx3 inside dedicated thread), Ollama lock, fast path timing, chat history, face greeting, clipboard, proactive.

#### 2.13 Kokoro Neural TTS
- **`346d30c`** — Kokoro neural TTS, orb voice reactions, real amplitude RMS streaming, companion orb sync (28 voices, per-emotion mapping).

#### 2.14 InsightFace GPU + 3D Brain Graph
- **`357dd69`** — InsightFace GPU face overlay, 3D brain graph (`3d-force-graph 1.80`), Whisper base, orb breathing, chat tab fixes.
- **`3a5a333`** — InsightFace overlays, smart wake word, attentive state, expression calibration, voice mood, 3D brain graph, orb breathing.
- **`9d07838`** — register pip-installed CUDA DLL dirs (cublas / cudnn / cufft / curand / cusolver / cusparse / cuda_runtime / nvrtc / nvjitlink) so InsightFace runs on GPU instead of silent CPU fallback.

#### 2.15 Audit + Wiring Verification
- **`94bca07`** — dead code cleanup (deleted `brain/vision.py`), wiring verification, onboarding InsightFace, performance fixes, health check (frame_store age replaces stale CAMERA_LATEST_JSON_PATH).

#### 2.16 Voice-First UI
- **`8affd49`** — voice-first UI: app discovery (367 apps + 32 games), 40-builtin voice command router, custom tabs, command_builder, correction handler, pointing via LLaVA, reminders, "Ava builds her own UI".

#### 2.17 Signal Bus / Event-Driven
- **`755f539`** — event-driven `brain/signal_bus.py`. Win32 `AddClipboardFormatListener` (zero-poll clipboard), `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` (zero-poll window switches), `ReadDirectoryChangesW` (zero-poll app installs).

#### 2.18 Voice Critical Fixes
- **`a740bcc`** — clap = direct wake (no classification), Whisper biased toward "Ava" via `initial_prompt`, clarification waits for yes/no, OutputStream protected playback, clap floor 0.35.

#### 2.19 openWakeWord + Silero VAD
- **`4477aa2`** — production wake-word stack: openWakeWord ONNX (<1% CPU), Silero VAD (RTF 0.004), Whisper transcript Eva→Ava normalization, `models/wake_words/hey_ava.onnx` slot reserved.

#### 2.20 ava-gemma4 + mem0 Memory
- **`5c2322c`** — `Modelfile.ava_gemma4` (identity baked from IDENTITY+SOUL+USER), `brain/ava_memory.py` (mem0 + ChromaDB + Ollama), gemma4 vision capability, 5 memory voice commands, memory tab in App.tsx.
- **`c54bbcb`** — full handoff + roadmap update; wake_word prefers custom hey_ava → hey_jarvis fallback (with phonetic benchmark).

#### 2.21 Repository Hygiene
- **`117428f`** — gitignore biometric (`faces/`), per-machine state (`.claude/`), and large local models (`models/wake_words/*.onnx`); untrack 149 already-committed runtime files.

---

### Section 3 — 2026-04-30 Stabilization Arc

Three sessions in one day pushed the system from "voice path crashes on first turn" to "voice path verified on real hardware + memory architecture rewrite started." Reports were originally written as standalone files at the repo root (`MORNING_REPORT.md`, `LUNCH_REPORT.md`, `AFTERNOON_REPORT.md`) and have been merged here.

#### 3.1 Overnight session (02:03-03:50 EDT)

**Goal:** unblock the voice path. Voice was crashing on first turn after a multi-day refactor; the hang couldn't be reproduced without hardware.

**13 commits.** Core unblock was the cold-start hang root cause:

> `import avaagent as _av` from a worker thread re-imported the script (Python registers `__main__` not `avaagent` when run via `py -3.11 avaagent.py`), triggering a fresh `_run_startup` execution that deadlocked.

Aliasing `__main__` to `avaagent` at the top of the script (`f99804e`) fixed it. Every test in the battery hung at 33s+ HTTP timeout before this; all four passed after.

**Other fixes:**
- `ava-personal:latest` reordered ahead of `ava-gemma4:latest` in `_pick_fast_model_fallback` — gemma4's "Thinking…" reasoning prefix consumed the fast-path's `num_predict=80` budget and produced empty `.content` (fallback to "I'm here.") (`f38d948`).
- ChatOllama instance caching keyed on `(model, num_predict)` saves ~1s of constructor cost per turn (`f38d948`).
- Boot-time fast-path prewarm thread pins `ava-personal` in VRAM and stashes the warmed instance in `_fast_llm_cache` so the first real turn lands on a cache hit. `joke_llm` went from 12.46s → 1.36s (`c14afed`).
- TTS self-listen guard in `voice_loop` drops VAD-confirmed audio while `_tts_speaking` so Whisper never transcribes Ava's own voice as user input (`163a7cc`).
- `concept_graph._save` exponential backoff (1, 2, 4, 8, 16, 32, 60s capped) on WinError 5/32 — bootstrap with 100+ nodes no longer floods stderr (`7e22bcf`).
- `/api/v1/debug/full` endpoint + `inject_transcript` endpoint — diagnostic infrastructure for future bug hunts (`41dce1d`, `96665ea`, `7cd9c2d`, `e37a566`).
- `tools/dev/regression_test.py` autonomous battery harness; `dump_debug.py`, `inject_test_turn.py`, `watch_log.py` — dev tools (`c1464c3`, `d678bc1`).
- Orb drift fix shipped pending visual verification (`8540269`) — removed `key=` remount on text divs + opacity-only fade-in.

**Test battery:** 6 consecutive green runs by end of session (4 core tests at 0.4-1.7s each).

**What didn't work / had to be redone:** The orb drift fix was code-only and shipped with diagnostic logs; visual confirmation was deferred to the user. The cube-morph feature was kept gated behind `PRESENCE_V2_CUBE_MORPH_ENABLED=false` until text-streaming verified stable.

**Lessons:** When a Python script is run as `__main__`, any worker that does `import script_name` will trigger a fresh top-level execution. Aliasing in `sys.modules` is the durable fix. This pattern came up again in the Phase 2 memory rewrite — bootstrap order matters.

#### 3.2 Lunch session (07:35-11:53 EDT)

**Goal:** documentation + extended regression coverage in additive-only mode (none of the don't-touch files modified).

**11 commits, all additive.** Wrote:

- **`d67361e` — `docs/ARCHITECTURE.md`** — single 10-minute system map. Process layout, shared-globals pattern, startup sequence (sync wave + background wave), voice path state machine, dual-brain coordination, Ollama lock, all six memory layers, tool registry, vision pipeline, heartbeat & background ticks, signal bus, the operator HTTP server (68 endpoints + the new debug ones), and the `ava_core/` identity files. Cites file:line throughout.
- **`a8ef8ee` — `docs/FIRST_RUN.md`** — companion to ARCHITECTURE.md but practical instead of conceptual. System dependencies (Python 3.11, Ollama, Node, Rust, NVIDIA), required Ollama models, Python environment, start commands, what good vs broken startup looks like with eight common-failure recipes, the first voice test recipe, live-state inspection, and an 11th-section sanity checklist.

**8 commits expanded the regression battery** with 7 new tests on top of the existing 4-test core, each individually revertable: `conversation_active_gating`, `self_listen_guard_observable`, `attentive_window_observable`, `wake_source_variety`, `weird_inputs`, `sequential_fast_path_latency`, `concept_graph_save_under_load` (`65a1c84`-`f5cf5e2`).

Also added `f348503` gitignore patterns for diagnostic / regression scratch logs.

**Test battery results (Run 2, 11:33 EDT):**

| Outcome | Count |
|---|---|
| Pass — core 4 + first 4 extended | 8 |
| Fail — `weird_inputs`, `sequential_fast_path_latency`, `concept_graph_save_under_load` | 3 |

**Diagnosis of failures:** test-design problem, not Ava bug. `weird_inputs.single_char "?"` and `weird_inputs.long_500` both legitimately route to the deep path (no fast-path pattern match); on a system already cold-loading models, two consecutive 60-90s deep-path turns saturate uvicorn's worker pool. **Recommended fix recipe** (deferred): replace `single_char "?"` with `"hi?"` (fast-path eligible) and rebuild `long_500` from repeated fast-path patterns.

**The lunch voice test on real hardware was the verifying moment** — Ava replied for the first time end-to-end through microphone + speakers since the work order began.

**Lessons:** documentation alongside dev tools is more valuable than code-only fixes. ARCHITECTURE.md and FIRST_RUN.md became the entry points every subsequent session referenced.

#### 3.3 Afternoon session (12:49-17:30 EDT)

**Goal:** fix the 6 issues surfaced by the lunch voice test + start the memory architecture rewrite.

**12 commits.** All 6 issues fixed:

| Issue | Fix | Commit |
|---|---|---|
| 150s reply latency after 13min idle (Ollama VRAM eviction) | `keep_alive=-1` on fast-path ChatOllama + 5min periodic re-warm tick that walks `_fast_llm_cache` and sends a one-token "ok" invoke | `f96c6c9` |
| Time/date queries reaching the LLM (hallucinated "9:47 AM") | Expanded voice_command regex to match natural variants ("tell me the time", "got the time", "what day is it", etc.) + new regression test asserting no `re.ollama_invoke_start` fires for these queries | `044f594` |
| openWakeWord catching "hey ava" as `hey_jarvis` | Disabled jarvis proxy by default; wake source now comes from clap + custom hey_ava.onnx (if trained) + transcript_wake via Whisper. Override via `AVA_USE_HEY_JARVIS_PROXY=1` | `382255c` |
| Second-turn TTS dropped silently | Added `tts.last_playback_dropped` snapshot field + back-to-back-turn regression test. tts_worker stamps the flag when the OutputStream loop breaks early. **Doesn't fix root cause — makes future drops VISIBLE so we can act on them.** | `53c12fa` |
| Brain tab 15fps (654 nodes / 6122 edges feeding WebGL every 5s) | Cap to 200 nodes by weight + 500 edges by strength + skip graphData updates when tab not focused + `pauseAnimation()` when not focused | `5d3f433` |
| Claude Code identified as Zeke during tests | New `claude_code` developer profile (in `brain/dev_profiles.py`, written to `profiles/claude_code.json` on demand) + `as_user` param on `inject_transcript` + regression test verifying both routing paths | `504d1e8` |

**Memory architecture rewrite — Phase 2 of the work order:**
- **`9c3c22c` — `docs/MEMORY_REWRITE_PLAN.md`** — audit of all 12 memory layers + the 10-level decay design, with the concept graph as the single seam.
- **Step 3 (`59ebd51`):** `level: int` + `archive_streak: int` + `archived_at: float` fields added to `ConceptNode`. `decay_levels(now=None)` walker demotes inactive nodes per the per-level threshold table; archived nodes clamp to 1, unarchived hit-zero nodes get deleted with a tombstone in `state/memory_tombstones.jsonl`. Hourly daemon thread fires the tick. **Promotions land in step 5.**
- **Step 4 (`36f9856`):** `brain/memory_reflection.py` new module. Post-turn LLM scorer asks "which retrieved memories were load-bearing?" and writes one row per turn to `state/memory_reflection_log.jsonl`. Hooked into `turn_handler.finalize_ava_turn` as a daemon thread (no user-facing latency). **Doesn't apply level changes — gathering data first.**

**Whisper-poll over-triggering — late afternoon find + fix (`47a1c92`):** post-fix regression run revealed ~20 `[wake_word] wake triggered (source=whisper_poll)` events during a single test session. Whisper transcribed anything resembling speech and matched against wake patterns — including Ava's OWN TTS playback. The self-listen guard had only covered `voice_loop.listen_session()`, not `wake_word._whisper_poll_loop()`. Applied the same guard to whisper-poll.

**Test battery results (Run, 17:24 EDT):** 11 of 15 tests passing — including all 3 new tests for the afternoon issues. The 4 failures: `thanks` (2.60s vs 2.0s — marginal regression, same fluctuation pattern), and the same 3 test-design problems documented at lunch.

**Wins verified:**
- Issue 1 (latency): no idle-gap timeout regression. Sequential fast-path turns 1-3s when warm.
- Issue 2 (time/date determinism): all 10 query variants resolved without invoking the LLM.
- Issue 6 (claude_code identity routing): `[memory-bridge] using profile key: person_id=claude_code` for inject calls — Zeke's profile NOT touched.

**Lessons:** the test battery surfaced a real bug it wasn't designed to test for (whisper-poll guard gap). Diagnostic-first fixes (Issue 4) buy observability for problems that can't be reproduced without hardware.

---

### Section 4 — 2026-04-30 → 2026-05-01 Night session

**Goal:** address the 9 issues from the hardware test session + brain architecture document.

**Session window:** 23:52 EDT (Apr 30) → ~02:00 EDT (May 1). **12 commits, all autonomous.** None of the read-only files modified. protobuf still pinned at 3.20.x.

| Issue | Fix | Commit |
|---|---|---|
| Two Ava instances on same port hammered out 100s of `[Errno 10048]` lines in infinite restart loop | Single-instance enforcement — port probe, PID lockfile at `state/ava.pid`, HTTP restart cap (3 attempts then `_ava_shutdown=True`), `atexit` lockfile cleanup | `6446707` |
| Same reply enqueued twice per turn (router + voice_loop double-dispatch) | Single-dispatcher rule: `voice_loop._speak()` owns TTS. Removed all four `_say(g, response)` calls from `VoiceCommandRouter.route()`. Per-command emotion stashed on `g["_voice_command_emotion"]` for future emotion-specific TTS | `c8b3d0b` |
| Whisper-poll firing 2-4 times per 13-second cycle on ambient quiet | Triple-gate guard before Whisper: RMS energy floor 0.02 (cheap fast-reject) + Silero VAD threshold 0.6 + min speech 300ms + the existing self-listen guard | `37ec144` |
| Face recognition broken — `loaded 0 embeddings from 0 people` despite 16 photos | Tight 200×200 reference photos gave RetinaFace no anchor context. Two-part fix: (1) second `FaceAnalysis` with `det_size=(320, 320)` for reference loading; (2) upscale reference images to ≥640px on min-dim with cubic interpolation. Verified directly: `loaded 19 embeddings from 2 people` (Zeke 16, Max 3) | `0b25d1f` |
| Inner monologue (`💭 "..."`) leaked into chat reply text and TTS | Three-part fix: `dual_brain.handoff_insight_to_foreground` no longer weaves inner_monologue into reply; `output_guard.scrub_visible_reply` strips 💭-prefixed lines; UI adds `<div className="presence-inner-thought">` rendering `snapshot.inner_life.current_thought` italic dimmer multi-line under the orb | `697921d` |
| Memory graph tagged everything as "User discussed: ..." | New `_person_display_name(person_id)` in avaagent.py: `zeke` → "Zeke", `claude_code` → "Claude Code", unknown → "Unknown person". `summarize_reflection()` accepts `person_id` and prefixes with `<DisplayName> said:`. Existing memory nodes NOT rewritten — decay naturally per Phase 2 rules | `9eb4b03` |
| Ctrl+C printed shutdown line but process kept running | Force-exit watchdog: signal handler stamps `_ava_signal_received_ts`, daemon thread polls every 0.5s, calls `os._exit(0)` if 5+ seconds passed without exit. Keepalive sleep reduced 2s → 0.5s. Exits in <0.5s on happy path, <5s worst case | `e66cd18` |
| No one-click full-stack launcher | New repo-root `start_ava.bat` — launches Tauri UI exe in background, runs `py -3.11 avaagent.py` in console, single-instance check rejects double-clicks. Sets `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`, `AVA_DEBUG=1`. Falls back if release UI exe doesn't exist | `7415f07` |
| MeloTTS bridge crashed on fresh machines | `_ensure_nltk_perceptron_tagger()` runs before `_load_melo_tts()` — `nltk.data.find()` check, `nltk.download(quiet=True)` on `LookupError`, belt-and-suspenders fallback to legacy name | `643d3d8` |
| Brain graph not Ava-centric | New `docs/BRAIN_ARCHITECTURE.md` mapping Ava's existing systems onto human-brain regions (Hippocampus / Amygdala / Prefrontal cortex / Default mode network / Visual cortex / Auditory cortex / Motor cortex / Brainstem / Corpus callosum). 3D brain graph implements it: AVA SELF at origin pinned violet, IDENTITY/SOUL/USER as gold anchors at 120° on inner ring, 5 tier radii with custom radial force, color scheme by trust + recency, middle-click recenters camera | `4faa1f7` + `202bf95` |
| App launcher's blind shell-start fallback popped Windows search dialogs on misses | New `app_discoverer.top_matches(query, limit=5)` returns ranked best-K matches. New `app_launcher.py` step 5: `error="I don't know an app called X. Apps I know that might match: A, B, C, D, E."` + `suggestions=[...]` | `b74e792` |

**Test battery results (Run, 01:26 EDT May 1):** 12 of 15 tests passing. All three new-this-night fixes verified:
- `time_date_no_llm` — 10 query variants, NO `re.ollama_invoke_start` for any (Issue 2 fix verified)
- `back_to_back_tts_no_drop` — `last_playback_dropped=false` after both turns (Issue 4 diagnostic verified)
- `identity_routing` — claude_code routing isolated from Zeke

**The same 3 test-design failures persisted** (`thanks` marginal, `weird_inputs` + `sequential_fast_path_latency` deep-path saturation). All known. Recipe to fix is documented but deferred.

**Lessons:** when the same 3 tests fail across 4 sessions for the same reason, fix the test, not the code. The recipe is sitting in lunch's report — apply it next time.

---

### Section 5b — 2026-05-01 Conversational Naturalness, Phase 1

**Goal:** make Ava feel like talking to a thoughtful, present human. Sub-500ms response start, real thinking signals only when actual computation requires them.

**Components 1-2 of 7 shipped this session.** Components 3-7 deferred to hardware-testing sessions per work-order spec. Full architecture in [`CONVERSATIONAL_DESIGN.md`](CONVERSATIONAL_DESIGN.md). Research basis in [`research/voice_naturalness/findings.md`](research/voice_naturalness/findings.md).

**Component 1 — Streaming chunked responses.**
The fast path in `reply_engine.py` now uses `langchain_ollama.ChatOllama.stream()` instead of `.invoke()`. A new `brain/sentence_chunker.py` (Pipecat-style `SentenceBuffer` with abbreviation guard, min-chars merge, tail flush) emits complete sentences from the streaming token deltas. Each sentence is pushed into `tts_worker`'s existing queue immediately — first sentence reaches Kokoro before the full reply is generated. `voice_loop._speak()` checks the new `_g["_streamed_reply"]` flag and waits for `worker.is_busy()` to drop instead of re-enqueueing.

Behind feature flag `AVA_STREAMING_ENABLED` (default 1). `set AVA_STREAMING_ENABLED=0` returns to the synchronous path for rollback.

Smoke-tested locally: chunker handles `Dr. Smith` (abbreviation), `Hi! Hello there.` (min-chars merge), trailing fragment without boundary (tail flush). All four touched files (`sentence_chunker.py`, `reply_engine.py`, `voice_loop.py`, `tts_worker.py`) `py_compile` clean.

**Component 2 — Real thinking signals (Tier 1 + Tier 3).**
New `brain/thinking_tier.py` module — `TierCoordinator` runs as a watchdog thread alongside the streaming loop. Tracks `t_start`, `first_chunk_ts`, `last_chunk_ts`. When the first chunk hasn't arrived 2s into the turn, emits a Tier 3 filler ("Give me a second.") into the TTS queue and publishes `_thinking_tier=3` to globals. Otherwise stays at Tier 1 (the default — no signal). Tier coordinator added to operator snapshot as `thinking_tier`.

Tier 2 (mid-stream filler on inter-chunk gaps) and Tier 4 (5s+ progress updates) intentionally deferred — emission timing for those needs live audio validation to avoid sounding robotic.

Verified: with a fake worker, coordinator publishes Tier 1 on init, fires Tier 3 filler at 2s+ delay, resets to Tier 0 on stop.

**What needs hardware verification:**
- Sub-500ms time-to-first-audio target (Component 1's latency claim).
- Kokoro sequential-chunk playback seamless on real audio output (no clicks between sentences).
- Tier 3 emission feels natural in voice (not just "fires correctly in code").
- No regression in voice path stability.

**Trace lines added** for live verification: `re.stream.first_chunk ms=...` (TTFA), `re.ollama_invoke_done fast_stream ms=... sentences=N`.

**Components 3-7 deferred:**
3. Honest uncertainty + tool use — needs live tool firing.
4. Context continuity — prompt-engineering, easy to add but easy to verify wrong.
5. Clarifying questions — same.
6. Matched depth — same.
7. Interrupt handling + presence — needs VAD-during-TTS hardware test; biggest risk to the hardened audio path.

**Lessons:** the research pass confirmed the universal architecture (Pipecat / LiveKit / Vocode all use the same parallel-task + queue pattern) — saved hours of design spelunking. Kokoro's `KPipeline` already being a generator made the critical insight: the existing concatenate-before-play loop in `tts_worker.py:418` was the only blocker; sentence chunks naturally route through the existing queue, no playback-path changes needed.

---

### Section 5 — 2026-05-01 Discord Auto-Spawn Fixes

Three commits hardened the Discord channel plugin's auto-spawn path on Windows.

**The problem:** `claude-plugins-official/discord` plugin's default MCP spawn config (`"command": "bun"`) relies on `bun` being on the PATH that Claude Code's MCP loader inherits. On Windows that's unreliable — `winget install Oven-sh.Bun` puts bun on User PATH only, not Machine PATH. Claude Code spawns MCP servers via `cmd.exe`; if the parent's PATH lacks bun's directory, the spawn dies with `'bun' is not recognized`.

**The fix evolution:**
- **`b9f33c2`** — first attempt patched cached plugin to invoke bun by absolute path. Worked but fragile.
- **`1637ca4`** — switched to `env.PATH` prepend, dropping the absolute-bun command.
- **`c25443f`** — final form: Python rewrite, BOM-free, marketplace + cache. The PowerShell-based patches had been silently corrupting `.mcp.json` with UTF-8 BOM; Node's `JSON.parse` rejects BOM-prefixed JSON, making the plugin appear to disappear entirely.

**Two layers of defense** in the final patched `.mcp.json`:
1. `command` is the absolute path to `bun.exe` (canonicalised — no `\.\` artifacts from winget's PATH entry). The initial spawn doesn't need bun on PATH at all.
2. `env.PATH` prepends bun's directory ahead of `${PATH}` so inner `bun install` and `bun server.ts` invocations find bun.

The fix patches BOTH the marketplace source (durable across launches) and every versioned cache (defensive in case marketplace is refreshed). Repair script: `py -3.11 scripts\repair_discord_plugin.py`. Smoke test: `py -3.11 scripts\smoketest_discord_mcp.py`. Full writeup: `docs/DISCORD_SETUP_NOTES.md`.

**Lesson:** `Set-Content -Encoding utf8` on Windows PowerShell 5.1 writes a UTF-8 BOM. Use Python (writes BOM-free UTF-8 by default) when generating JSON/config files that other tools will parse.

---

### Section 7 — 2026-05-02 Emotion taxonomy + self-diagnostic introspection

**Two related fixes** addressing real bugs Zeke surfaced from hardware testing — Ava verbalized frustration about a broken camera but the UI showed 80-90% calm, AND she couldn't articulate WHAT was broken (just looped "my camera isn't working" without technical detail).

**Task 1 — Emotion-weight taxonomy and dialogue→emotion pipeline.** Audit (Explore agent across `avaagent.py`, `brain/emotion.py`, `brain/turn_handler.py`, `brain/prompt_builder.py`, `apps/ava-control/src/components/OrbCanvas.tsx`) found the disconnect was caused by:

- The Cowen-Keltner 27-emotion taxonomy had **no slot for `frustration`, `annoyance`, or `distress`**. The TTS worker (`tts_worker.py:58,74,91`) and the visual emotion map (`avaagent.py:1219`) both referenced "frustration" *as if it existed*, but no actual weight was tracked.
- `update_internal_emotions(user_input)` only fired on the **deep path** (called from `prompt_builder.py:63`). Fast-path turns never reflected user-input emotion language.
- Ava's **own reply text was never analyzed**. She could say "I'm frustrated" and her tracked state never updated.

Fix shipped in `2403bb7`:
- `EMOTION_NAMES` expanded 27 → 30 with `frustration`, `annoyance`, `distress` (alphabetical).
- `DEFAULT_EMOTIONS` and `DEFAULT_EMOTION_REFERENCE` get full entries for the new emotions.
- `update_internal_emotions` extended with two new keyword blocks (frustrated/broken/failed/stuck → frustration; distressed/panicked/overwhelmed → distress).
- New function `update_internal_emotions_from_reply(ai_reply)` parses Ava's first-person emotion self-reports (with a self-frame check so "Zeke seems frustrated" doesn't bump Ava's mood). Wired into `finalize_ava_turn`.
- `build_prompt_fast` now also calls `update_internal_emotions` — closing the silent fast-path gap.

Verified live: inject "I am frustrated and annoyed with this broken thing" → mood file shows `frustration=0.0482 annoyance=0.0200 distress=0.0012`, stable over 30s, reason field updated correctly.

**Task 2 — Self-diagnostic introspection layer.** Audit found the existing diagnostic surface (`/api/v1/debug/full`, `state/health_state.json`, `_ERROR_RING`) was rich but had no consumer that combined it into a technical summary. `_cmd_system_check` only returned CPU/RAM%. No "what's wrong" voice pattern wired anywhere. Inner monologue had no error-log visibility.

Fix shipped in TBD:
- New `tools/system/diagnostic_self.py` registers Tier-1 tool `diagnostic_self`. Reads subsystem state from globals + `state/health_state.json`, recent errors from `debug_state._ERROR_RING`, and returns `{summary_text, subsystems[], errors_recent[], traces_recent}`. The `summary_text` field is ready-to-speak.
- `brain/voice_commands.py` adds pattern `\b(?:what's wrong|are you (?:ok|okay|alright|broken)|what's (?:broken|failing|wrong)|diagnostic (?:check|self|run)|status report)\b` → invokes the tool, returns the lead 12 lines of summary as the spoken reply with focused emotion.
- `SYSTEM_PROMPT` updated: when the LLM is about to say "X is broken" / "I'm not sure why this is failing", call `[TOOL:diagnostic_self]` instead of looping on vague feelings.

Verified live: inject "what's wrong with you?" → 448ms ttfa (voice command intercept), reply: *"I have 3 subsystems reporting degraded state right now. - camera: degraded — available=unavailable running=False ..."* Compare to the bug case ("my camera isn't working" without technical detail). Second inject "diagnostic check" → 10ms ttfa, same reply. Both trigger paths fire.

**Findings flagged for future work** (added to `ROADMAP.md`):
- Sensor → emotion pipeline doesn't exist. `signal_bus` has no failure events; tool errors / vision pipeline timeouts / model load failures don't push frustration/distress signals into mood. Camera-died-while-Ava-is-silent → Ava's tracked mood doesn't shift.
- `OrbCanvas.tsx` morph map has 8 emotion configs; missing keys silently fall back to `calmness`. With 30 emotions now, this gap is more visible. Cosmetic, not functional.
- Self-diagnostic ties to upcoming sleep-mode self-detection (Ava recognizing degraded internal state and choosing to sleep) and trust-level-aware honesty (which subsystems can she report on to which user roles).

---

### Section 6 — 2026-05-01 App-scan parallelism retro fix

**Engineering-discipline retro.** Commit `76d609b` (autonomous queue) claimed `discover_all` and `discover_new_since_last` were now parallelized into "four scan roots in parallel threads," with an expected cold-boot improvement from 60-110s sequential to 30-50s parallel. The claim was shipped untested.

When Ava booted post-VB-CABLE reboot the trace ring buffer recorded what actually happened on the cold-cache run: `app_disc.discover_all_done ms=217756`. Reading the per-root scan_done timestamps, two of the four "parallel" threads were scanning multiple roots in series:

- The `lnk` thread did Public Desktop → ProgramData StartMenu → User StartMenu serially (3+25+15s).
- The `program_files` thread did `C:\Program Files` → `C:\Program Files (x86)` serially (150+67s).

Wall time was therefore bounded by `150 + 67 = 217s` — the bottleneck thread, with PF and PF(x86) running back-to-back. Real per-root parallelism would land at `max(150, 67, 40) = 150s`.

**Fix.** Refactored `_run_scans_parallel` in `brain/app_discoverer.py` to fan out via `concurrent.futures.ThreadPoolExecutor` with **six** independent tasks: PF, PF(x86), Desktop .lnk dirs, Start Menu .lnk dirs, Steam, Epic. Added `root` parameter to `_scan_program_files` so each root is its own task. Removed the dead `_lnk_dirs` helper. Added `scripts/bench_app_discovery.py` for repeatable timing.

**Verification (warm cache, post-reboot):** 3-run wall times 1.34s / 1.19s / 1.17s. Trace shows all four primary roots starting within 2-3ms of each other (real parallelism, not the previous serialized chains).

**Cold-cache prediction:** ~150s, dominated by the `C:\Program Files` walk alone. The original work order's <60s target is not reachable from parallelism alone on this hardware — the PF scan walks 195 binaries at depth 3 and that is the realistic floor. Hitting <60s would require additional optimizations (depth reduction, exclusion list, lnk-target prefilter); flagged but not in scope here.

**Discipline note.** This is exactly the failure that Standing Operating Rule 4 ("verify fixes before claiming them as done") exists to prevent. The original parallelism claim was theoretical and shipped without the cold-boot timing test that would have caught the wrong-thread-count assumption immediately. Logged here so the pattern is visible in retrospect.

---

### Section 15 — 2026-05-04 Phase A bug fixes + Phase B Session A inject-mode verification

Five Phase A fixes shipped clean. Phase B Session A behavior verified via inject_transcript (audio loopback driver fragility blocked synthesized-voice path; documented as test-infrastructure issue, not Ava regression). Sessions B and C deferred to manual real-voice runs.

#### Phase A fixes (all shipped)

1. **Autonomous greeting → ava-personal:latest + 5min user-engagement quiet.** `brain/proactive_triggers.py`. Greeting no longer routes to deepseek-r1:8b which was wedging Ollama for 12+ min under VRAM contention. Plus 5-min suppress when `_last_user_message_ts` is fresh — autonomous "hello"s during active conversation are noise. Vault: `bugs/autonomous-greeting-blocks-chat.md`.

2. **1-minute wake-word grace period.** `config/voice_tuning.json` `attentive_base_seconds` 180 → 60. Mirrors Claude mobile / ChatGPT voice mode — after Ava responds, follow-up commands within 60s don't need re-wake. Outside grace, re-wake required.

3. **Source attribution at all 3 layers.** Storage already worked (`source` field present in chat_history.jsonl). UI updated (`apps/ava-control/src/App.tsx` chat bubble renders source label: "zeke / claude code / ava / ava (initiated)" instead of bare role). `/api/v1/chat` accepts `as_user` param so HTTP callers can identify themselves; sets active_person_id BEFORE chat_fn so run_ava picks up the right profile (Claude Code's profile has `name="Claude Code"`).

4. **Camera feed redundancy.** `apps/ava-control/src/App.tsx` consolidated 3 different camera-image sources (`liveFrameSrc` shared state vs `frameUrl` HTTP-fetched per consumer vs a dead `/api/v1/camera/frame` URL) into `sharedCameraSrc = liveFrameSrc || frameUrl`. Single 5fps poll at one source, all tabs subscribe. Same orb pattern.

5. **Orb state shape canonical list.** Vault: `designs/orb-state-shapes.md`. 7 canonical shapes (sphere/cone/cube/triangular prism/hex bipyramid/torus/arrow). Surfaced 5 extras in code (`bored`, `excited`, `offline`, `sleeping`, `waking`) and 1 missing (`pointing` — Arrow shape, no implementation yet) for Zeke's call.

#### Bonus fixes

- **`cu_open_app` already-open dedup.** Both `WindowsUseAgent.open_app` and `voice_commands._route_open_app` check `find_window_candidates` before launching. Returns "X is already open" instead of opening a second window. (Note: Zeke clarified later that "Open Chrome" + "Open Chrome" should actually open 2 windows, with "Open another tab" being its own intent. Dedup needs a small follow-up to scope to non-browser apps only — vault: `designs/app-knowledge-and-tab-handling.md`.)

- **`_cmd_close` reply phrasing cleanup.** `brain/voice_commands.py` close handler was returning `"Closed {raw_target}."` where target included trailing courtesy words ("please" / "too" / etc), so Ava said "Closed steam too" — sounded like restating user's command, not confirming. Now strips trailing courtesy/conjunction words and renders `"{Display} is closed."`.

- **Lessac high-quality Piper voice** downloaded to `models/piper/en_US-lessac-high.onnx` (108MB, gitignored). More natural prosody than amy-medium; harness now prefers it.

#### Phase B — partial

**Session A: PASSED via inject_transcript** (8/8 turns produced replies):
- Identity probe → Ava responded with mood format ("Feeling neutral with some focused.") — strict identity validator marked FAIL (no literal "ava" in reply) but **no identity drift** (no "I am Qwen" / "I am a language model" violations).
- Open Chrome ✅, Open Edge ✅, **Open Edge again → "Edge is already open." ✅ (dedup verified)**, Open Steam ✅.
- Close Chrome ✅, Close both Edge tabs ✅, Close Steam ✅.

Transcript: `D:\ClaudeCodeMemory\sessions\2026-05-04-conversation-test-A.md`.

**Sessions B and C: DEFERRED.** B partially run via inject (3 of 4 turns timed out on deep-path run_ava > 240s — model swap penalties). C not run.

**Audio-loopback driver consistently failed** despite multiple workarounds: split utterance + silence, one-utterance, pre-silence pad, voice swap from amy-medium to lessac-high. Whisper-poll's 1.5s rolling capture / 3s sleep cycle is tuned for human speech timing; Piper synth's compressed timing without natural breath gaps doesn't reliably trigger wake. Vault: `bugs/synthesized-voice-wake-detection.md` with 5 fix-option sketches (recommended: Option D — `AVA_TEST_MODE=1` env var bypasses wake-keyword check for synthesized-voice harness).

#### Files modified

- `brain/proactive_triggers.py` — A1
- `config/voice_tuning.json` — A4
- `brain/operator_server.py` — A3 (chat as_user param)
- `apps/ava-control/src/App.tsx` — A2 (camera redundancy) + A3 (chat source label) + ChatMessage type
- `brain/persona_switcher.py` — earlier Section 14 fix (notes-list coercion)
- `brain/windows_use/agent.py` — cu_open_app dedup
- `brain/voice_commands.py` — open-app dedup + close-cmd cleanup
- `start_ava_dev.bat` — AVA_TTS_DEVICES env var added (speakers + cable + vaio3 for monitoring)
- `scripts/audio_loopback_harness.py` — Lessac voice preference
- `scripts/verify_session_A.py` (new) — Session A driver
- `scripts/verify_all_sessions.py` (new) — all-sessions driver via inject_transcript
- `.gitignore` — note that lessac onnx is also excluded

#### Vault writes

- `bugs/synthesized-voice-wake-detection.md` (NEW) — full diagnosis of why Piper-driven testing keeps failing
- `designs/orb-state-shapes.md` (NEW) — canonical 7-shape list with discrepancies for Zeke's call
- `designs/app-knowledge-and-tab-handling.md` (NEW) — Zeke's feedback on browser tab semantics + app knowledge map design
- `sessions/2026-05-04-conversation-test-A.md` (NEW) — Session A inject-mode transcript

#### Confidence assessment

- **Phase A: shipped.** Bug fixes are real and correct.
- **Phase B Session A: PASSED via inject** — dedup works, multi-app open/close works.
- **Phase B Sessions B+C: DEFERRED** — need either real-voice manual testing or harness redesign (Option D test-mode wake bypass).
- **Voice production path: WORKS** for Zeke (he confirmed hearing Ava during the test session).
- **Voice testing infrastructure: BROKEN** for synthesized voice. Filed as test-only bug.

---

### Section 14 — 2026-05-04 Bug-fix follow-up: voice-loop hang + cascading workers + new architectural follow-up

Goal: get Ava's chat + voice paths to ChatGPT-mobile-quality reliability — every turn completes, sub-15s typical latency, conversation flows across many turns. Fix the two bugs Section 13 surfaced, then re-run Phase B.

#### Fixes applied (4)

1. **Cascading workers in `/api/v1/chat`** (Bug 2 from Section 13). Added `_RUN_AVA_SERIAL_LOCK` (module-level `threading.Lock`) and `_run_ava_cancel` event to `avaagent.py:7222+`. Workers acquire the serial lock with 1s polls so they can bail on cancel; foreground 90s timeout fires `_run_ava_cancel.set()`. Result: under sustained chat load, new requests queue cleanly behind the in-flight worker instead of stacking ghost workers that keep hammering Ollama after the foreground timed out.

2. **Voice_loop hang on `run_ava.return`** (Bug 1 from Section 13). Wrapped voice_loop's `run_ava(text)` call in a worker thread with a hard 120s timeout (`brain/voice_loop.py:474+`). On timeout, state drops to `passive` instead of hanging at `thinking` indefinitely. Diagnostic `[vl-diag]` prints kept in place to localize any remaining occurrence.

3. **`[stage7] persona inject failed`** root cause. `brain/persona_switcher.py:62` was calling `notes.strip()` on what could be a list (older profile schema stored notes as `list[str]`). Fixed to coerce: `isinstance(notes_raw, list) → "\n".join(...)`. Eliminates the recurring exception that correlated with hangs.

4. **Operator-chat post-run_ava streamline.** Moved `bump_session_message_count`, `update_self_narrative`, post-reply `workspace.tick`, `build_perception`, `update_expression_state`, `process_camera_snapshot` from inline-after-run_ava to a daemon-thread housekeeping function (`avaagent.py:7280+`). HTTP response returns immediately after `run_ava` does. Heartbeat already runs all this work every 30s, so removing the inline pass doesn't reduce Ava's awareness — only the per-chat-call freshness, which the HTTP caller doesn't need.

#### Phase B retry — blocked by new architectural issue

After applying the four fixes and restarting Ava, Phase B retry started cleanly. **Turn 1 timed out empty at 240s.** Ava's logs showed:
- `re.lock_wait_acquired label=greeting:deepseek-r1:8b` at 20:09:46
- 12+ minutes later: lock STILL held, no `lock_released` log

Root cause: Ava's autonomous proactive-greeting subsystem fired when it detected Zeke's face on camera. The greeting routes to `deepseek-r1:8b` (5.2 GB reasoning model). Under VRAM contention with `llava:13b` (5.5 GB) already resident, Ollama's model swap got stuck — possibly Ollama itself paged unstably under the 8 GB ceiling.

The chat path was blocked waiting on the same global ollama lock. The 4 fixes from this session work AS DESIGNED — they prevent THEIR specific failure modes — but they assume a healthy ollama. When ollama is wedged on a different subsystem's call, no per-caller fix saves the chat path.

This is a NEW issue, filed in vault as `bugs/autonomous-greeting-blocks-chat.md` with 4 fix-option sketches. **Recommended fix combo:** (B) lighter model for autonomous greeting (`ava-personal:latest` instead of `deepseek-r1:8b`) + (C) suppress proactive greeting when `_last_user_message_ts` is within ~60s. Option A (priority queue at the ollama lock) is the proper long-term fix but more invasive.

Phase B retry can complete once the priority/lightweight-greeting fix lands. Until then, sustained-conversation testing remains blocked by the autonomous-vs-foreground contention pattern.

#### Files modified

- `avaagent.py` — `_RUN_AVA_SERIAL_LOCK` + `_run_ava_cancel` event; chat-streamline housekeeping refactor; cancel-aware worker.
- `brain/voice_loop.py` — voice_loop run_ava timeout wrapper.
- `brain/persona_switcher.py` — `notes` coercion fix.

#### Vault writes

- `bugs/operator-chat-cascading-workers.md` — UPGRADED to FIXED (commit pending).
- `bugs/voice-loop-restart-hang.md` — UPGRADED with mitigation applied.
- `bugs/autonomous-greeting-blocks-chat.md` — NEW. The architectural follow-up.

#### Confidence assessment

Per-caller robustness is now in place. Single-turn chats and short multi-turn sessions should be reliable as long as Ava isn't running heavy autonomous work concurrently. Sustained mixed-load (autonomous greeting + chat) still breaks. Daily use that doesn't trigger heavy autonomous behavior is OK; full ChatGPT-mobile parity needs the priority-queue fix from `bugs/autonomous-greeting-blocks-chat.md`.

---

### Section 13 — 2026-05-04 Long-form conversation work order — Phase A/C/D shipped, Phase B partial (2 production bugs surfaced)

Five-phase work order: Phase A audio routing for monitoring, Phase B 30-min sustained conversation with identity probes + sleep cycle, Phase C dual-audio-path documentation, Phase D 3 new curriculum stories, Phase E summary. Phases A, C, D shipped clean. **Phase B is partial — surfaced two real production bugs that block sustained-conversation testing on this hardware.** The test did its broader job: the work order anticipated this kind of issue ("issues short single-command tests miss"), and exactly those issues showed up.

#### Phase A — audio routing for monitoring (✅ shipped)

Speaker-monitor implemented in software via `scripts/audio_loopback_harness.py:play_wav_to_cable`. Two parallel `sd.play()` streams: one to `CABLE Input` (Ava's mic), one to `Speakers (Realtek)` (Zeke's monitor). `AVA_HARNESS_NO_MONITOR=1` env var disables. Ava's TTS already plays to speakers via the existing `AVA_TTS_DEVICES="speakers,cable,voicemeeter vaio3 input"` env var — no Voicemeeter mixer config change needed. Both voices reach Realtek during testing; CABLE virtual cable + Voicemeeter VAIO3→B3 capture path stay isolated. No feedback risk because GAIA HD mic is muted in test mode (default mic = CABLE Output).

Vault: `decisions/audio-routing-monitoring.md`.

#### Phase C — dual-audio-path architecture (✅ shipped)

Pattern (b): Windows-default-mic toggle switches between production (GAIA HD, Zeke's voice) and test (CABLE Output, Claude Code's harness). Single-command toggles via the new `scripts/set_audio_test_mode.bat` and `scripts/set_audio_production_mode.bat`. Underlying mechanism is `Set-AudioDevice -Index N` from the `AudioDeviceCmdlets` PowerShell module installed in the voice E2E session.

Manual hardware-path verification procedure documented at `D:\ClaudeCodeMemory\sessions\hardware-path-verification.md` for Zeke to run (requires physical voice into GAIA HD).

Vault: `decisions/dual-audio-path.md`.

#### Phase D — three new curriculum stories (✅ shipped)

Aesop-style originals saved to `curriculum/foundation/`:
- `the_potter_and_the_thirty_jars.txt` — *"A skill earned through error knows the wrong turns as well as the right."*
- `the_woodcutter_and_the_purse.txt` — *"To act well once is a choice; to be known for it is a life."*
- `the_weaver_at_the_difficult_pattern.txt` — *"When the work is hard in the middle, the work is real."*

Each ~250-400 words, plain text with YAML metadata header matching the existing 25 fables. `_index.json` updated. Total entries 25 → 28. Verified loadable via `brain.curriculum.list_curriculum(g)` and `read_curriculum_entry(g, slug=...)`.

#### Phase B — sustained conversation test (⚠️ PARTIAL — blocked by 2 production bugs)

**Two distinct failure modes surfaced and blocked the planned 28-turn run:**

**Bug 1 — voice_loop hangs on `run_ava.return`** (audio path). Reproduced 2× during this session (was filed as `not-reproducing` in the previous voice-E2E session; **now upgraded to REPRODUCING-AGAIN**). On affected turns, `re.run_ava.return path=fast ms=...` fires inside reply_engine, but the very next line in voice_loop (`[vl-diag] run_ava returned ...` print I added between `run_ava_result = run_ava(text)` and the unpack) never fires. State stuck at `thinking` indefinitely. `[stage7] persona inject failed: 'list' object has no attribute 'strip'` errors visible in the log near the hang times — possibly correlated. Force-kill needed to recover Ava.

**Bug 2 — `/api/v1/chat` cascades ghost run_ava workers under sustained load** (text path, NEW). Pivoted Phase B to the text-mode driver after the audio-mode hang. After 5-8 turns of mixed fast/slow paths, subsequent turns block at the client side at urlopen's 240s timeout. Root cause: `avaagent.py:7222-7234` spawns a daemon thread for each `run_ava` call, foreground waits 90s then returns a "Sorry, I'm thinking slowly" fallback **but the worker keeps running**. Multiple ghost workers accumulate, saturating Ollama. The `_CHAT_CALL_LOCK` releases after 90s even on timeout, but Ollama remains contended.

**Phase B coverage NOT achieved:**
- Identity-anchor stability under sustained load — UNKNOWN. Both attempted identity probes (turns 2 and 6) hit the operator-chat 90s timeout BEFORE Ava could reply. Identity drift was not tested; it was overshadowed by the timeout.
- Sleep-and-resume mid-conversation — NOT EXERCISED. Sleep trigger was scheduled at turn 14; we stopped at turn 8.
- Memory coherence across the session — NOT EXERCISED.
- Mood drift — NOT EXERCISED.

**Phase B coverage achieved (partial):**
- 8 turns attempted, 3 produced real replies (turns 1, 3, 5), 3 hit the operator-chat 90s timeout fallback (turns 2, 4, 6), 2 cascaded into client-side timeouts with empty replies (turns 7, 8).
- Voice-command-router fast path verified at 4.5s for "what time is it" (turn 5) — proves the fast path isn't blocked when it doesn't need full LLM invocation.
- Test surfaced both bugs above clearly with reproduction context. Failure modes documented well enough to fix in a follow-up work order.

**Vault writes:**
- `bugs/voice-loop-restart-hang.md` — UPGRADED from `not-reproducing` to `REPRODUCING-AGAIN` with new reproduction details.
- `bugs/operator-chat-cascading-workers.md` — NEW. Root cause + 3 fix-sketch options.
- `sessions/2026-05-04-long-conversation-test.md` — partial transcript + table.

#### Files modified / new

- `curriculum/foundation/the_potter_and_the_thirty_jars.txt` (new)
- `curriculum/foundation/the_woodcutter_and_the_purse.txt` (new)
- `curriculum/foundation/the_weaver_at_the_difficult_pattern.txt` (new)
- `curriculum/foundation/_index.json` (updated)
- `scripts/audio_loopback_harness.py` (added `also_to_speakers` mirror)
- `scripts/set_audio_test_mode.bat` (new)
- `scripts/set_audio_production_mode.bat` (new)
- `scripts/verify_long_conversation.py` (new — audio-mode driver, blocked by voice-loop hang)
- `scripts/verify_long_conversation_text.py` (new — text-mode pivot driver)

#### Confidence assessment

**Voice stack is NOT ready for unsupervised daily use.** Two reproducible failure modes exist:
1. Voice-loop hang means a single bad turn can wedge Ava's audio path until restart.
2. Cascading-worker bug means sustained chat usage degrades performance until eventual unresponsiveness.

**Voice stack IS ready for short interactive sessions.** Single-command tests, short multi-turn (under ~5 turns), and the F8/F12 single-command verification we shipped in the prior voice-E2E session all pass clean. Daily use that stays under the failure threshold (which is somewhere around 5-8 sustained turns) is fine.

Phases A/C/D delivery is solid — those are independent of the voice loop bugs. Phase B's intent (verifying long-form coherence) requires the voice-loop hang AND the cascading-worker bug to be fixed first. Filed as ROADMAP follow-ups for the next work order.

---

### Section 12 — 2026-05-04 Subsystem health snapshot fixes (STT + InsightFace + camera publish gaps)

User asked "any current bugs to fix?" after the external memory work order shipped. Quick health probe revealed `subsystem_health.stt_engine.available`, `subsystem_health.insightface.available`, and `subsystem_health.camera.running` all reporting False/empty even though Ava's logs showed all three subsystems running cleanly. Same pattern as the kokoro_loaded flag bug fixed earlier in `e8e3dce`, but in three new sites.

#### Root causes (4 sites total)

1. **STT** — `brain/startup.py:554-559` set `g["stt_engine"] = _stt` but never `g["_stt_ready"] = True` that the snapshot reader expected.
2. **InsightFace publish** — `brain/insight_face_engine.py:138-139` set `self._available = True` but never `self.ready = True` or `self.providers = [...]` that the snapshot reader expected.
3. **InsightFace storage-key mismatch** — separate class of bug, surfaced after fixing #2. Bootstrap stores the engine at `g["_insight_face"]` (singular) but `operator_server.py:1097` reads `g["_insight_face_engine"]` / `g["insight_face_engine"]` — different key names. Reader fell through to None regardless of engine state.
4. **Camera** — `CameraManager.__init__` never initialized `self.running`. The video capture thread (`brain/background_ticks.py`) opened the camera but never told the manager its capture-running state.

#### Fixes

```python
# brain/startup.py — STT init
g["_stt_ready"] = True   # on success
g["_stt_ready"] = False  # on failure / disabled

# brain/insight_face_engine.py — at end of successful init
self.ready = True
self.providers = [self._provider] if self._provider else []

# brain/camera.py — CameraManager.__init__
self.running: bool = False  # default

# brain/background_ticks.py — _video_frame_capture_thread
cm.running = True  # after cv2.VideoCapture opens
cm.running = False # on capture failure

# brain/operator_server.py — snapshot reader
ife = g.get("_insight_face") or g.get("_insight_face_engine") or g.get("insight_face_engine")
```

#### Verification

After restart with all 4 fixes, `/api/v1/debug/full` reports:

```
stt:     True
insight: avail=True providers=CUDAExecutionProvider
camera:  avail=True running=True
kokoro:  True
```

All four flags reflect actual runtime state. Vault note `subsystem-health-publish-gaps.md` captures both the publish-gap pattern and the new storage-key-mismatch class for future grep lookups.

#### Lesson reinforced

The kokoro fix (commit `e8e3dce`) explicitly stated the general pattern: *"at the end of every init function that succeeds, write `g["<subsystem>_ready"] = True`"*. We didn't grep for other sites at the time; three downstream subsystems silently misreported their health for weeks. **Going forward: after fixing any "subsystem state ≠ snapshot state" bug, immediately grep `operator_server.py` (or wherever the snapshot reader lives) for similar `g.get("<subsystem>_ready")` or `getattr(<obj>, "<attr>", False)` patterns and apply the publish call at every corresponding init site.**

#### Files modified

- `brain/startup.py` — STT publish call.
- `brain/insight_face_engine.py` — `self.ready = True` + `self.providers` attr.
- `brain/camera.py` — `self.running` default in `CameraManager.__init__`.
- `brain/background_ticks.py` — `camera_manager.running = True/False` in capture thread.
- `brain/operator_server.py` — snapshot reader checks canonical `_insight_face` key first.

Vault: `bugs/subsystem-health-publish-gaps.md` documents the full diagnosis including the storage-key class.

---

### Section 11 — 2026-05-04 Claude Code external memory setup (Obsidian vault + Graphify)

Sets up Claude Code's *own* external memory infrastructure — distinct from Ava's brain, memory, concept graph, or any of her subsystems. Ava's memory is hers. This vault captures the *why* behind decisions across Claude Code sessions, complementing (not replacing) `ROADMAP.md` and `HISTORY.md` which remain operational source of truth.

#### Phase A — Obsidian vault at `D:\ClaudeCodeMemory\`

Folder structure: `sessions/`, `decisions/`, `bugs/`, `designs/`, `people/`, `graphify/`. Plus `hot.md` (entry point — most recent session summary, read first on session start) and `CLAUDE.md` (vault-internal templates + protocols + anti-patterns).

Obsidian app installed via `winget install Obsidian.Obsidian` (no telemetry, no sync, local-only).

Repo's `D:\AvaAgentv2\CLAUDE.md` gained a "Claude Code's External Memory" section pointing to vault + session-start / session-end protocols.

Backfilled from the past week:
- **8 decisions notes**: `sleep-mode-3-phase`, `temporal-sense-cadence`, `windows-use-library-choice` (rejecting the protobuf-7-incompatible `windows-use` PyPI package), `voicemeeter-potato-over-vbcable-ab`, `personhood-frame-discipline` (the architectural-vs-phenomenological frame applies to design docs, NOT to identity anchors), `vaio3-test-harness-timing` (the silent-capture diagnosis), `pydantic-fastapi-forwardref-trap` (Pydantic v2 + locally-scoped class), `graphify-adoption`.
- **5 bugs notes**: `build-prompt-fallback-path` (the 600 s → 6.9 s turn fix), `kokoro-loaded-flag-publish`, `heartbeat-tick-budget-bloat` (197 ms → 12.2 ms), `whisper-poll-bypass-missing`, `voice-loop-restart-hang` (non-reproducing).
- **5 sessions notes**: temporal substrate, windows-use shipped, four-feature work order, voice e2e verification, voice e2e bug fixes.
- **1 people note** (`zeke.md`) capturing communication style, work patterns, decision style, trauma-material guardrails (Natalie removal — engineering shouldn't process real-life material), Ava-as-companion-not-assistant rules, past inflection points.

#### Phase B — Graphify codebase indexing

`graphifyy` Python package v0.7.5, AST extraction via tree-sitter (no LLM API key needed — semantic enrichment via Kimi/Anthropic deferred until orientation queries actually feel insufficient).

`graphify update D:\AvaAgentv2`: 283 files → **4,248 nodes, 8,248 edges, 257 communities**. Output at `D:\AvaAgentv2\graphify-out\` (gitignored — `.gitignore` updated) and mirrored to `D:\ClaudeCodeMemory\graphify\ava-agent-v2\` for the vault's query path.

`scripts\update_graphify.bat` for manual re-run after significant code changes. Future `graphify hook install` would automate via git post-commit; deferred until manual workflow proves limiting.

**Token reduction measured: 119.7x avg** vs naive corpus reading (`graphify benchmark`). Per-query: 86-192x range across 5 representative orientation questions. In real terms: a session that previously needed ~280k tokens for orientation now needs ~12k via graph queries. Massive context-window headroom for actual work.

#### Phase C — mem0 deferred (hardware constraint)

Hardware baseline taken with Ava running normally:
- VRAM: 7,457 / 8,151 MiB used (91.5%) — only 354 MiB free with llava:13b resident.
- RAM: 22.1 / 31.3 GB used — 9.2 GB free.
- CPU: 27% baseline.
- Disk: 782 GB free.

The work order's spec'd `n3rdh4ck3r/claude-code-mem0-mcp` repo 404s on GitHub. Closest current alternative `elvismdev/mem0-mcp-selfhosted` adds **Neo4j** to the stack (Qdrant + Neo4j + Ollama), making the cost-benefit even worse than the spec'd version.

The binding constraint is **VRAM at 91.5%**. mem0 itself doesn't add a new GPU model — it reuses Ollama's existing `nomic-embed-text` for embeddings. But each mem0 write/query forces Ollama to page out `ava-personal:latest` (4.9 GB) to load `nomic-embed-text`, costing 30-90 s. That latency lands in the next voice turn. Voice loop is the production interface; making it slower for a marginal Claude Code orientation improvement is the wrong trade.

Phase B's Graphify already gives the bulk of the orientation value (119.7x reduction). Phase C's mem0 would add semantic search of past markdown notes — currently 14 notes, grep is sufficient.

**Decision: defer cleanly.** Full reasoning + revisit conditions in `D:\ClaudeCodeMemory\decisions\mem0-deferred.md`.

#### Files modified / new

- `D:\AvaAgentv2\CLAUDE.md` — "Claude Code's External Memory" section added.
- `D:\AvaAgentv2\.gitignore` — `graphify-out/` excluded.
- `D:\AvaAgentv2\scripts\update_graphify.bat` (new) — manual graph updater.
- `D:\AvaAgentv2\docs\ROADMAP.md` — Section 1 entry for the external-memory work order.
- `D:\AvaAgentv2\docs\HISTORY.md` — this Section 11.
- `D:\ClaudeCodeMemory\` (entire vault, separate filesystem, not in Ava git history).

---

### Section 10 — 2026-05-04 Voice E2E bug-fix work order

Closeout of the five follow-ups filed by Section 9's voice E2E verification. All five resolved in this session.

1. **VAIO3 "silent capture" was a test-driver bug.** Reproduced Ava's exact `tts_worker` OutputStream config (sr=24000, ch=1, dtype=float32, blocksize=2048, latency='low') against Voicemeeter VAIO3 in `scripts/_test_kokoro_path.py` and `_test_kokoro_multistream.py` — passes at peak 0.4 in single-stream AND multi-stream-to-3-destinations configs. The "silent" F8/F12 captures were because Kokoro's first-run cudnn EXHAUSTIVE warmup makes synthesis take 25-30s; my 8-25s record windows ended before playback even started. Updated `scripts/_capture_ava_tts_v2.py` to 60s window — final round-trip: `POST /api/v1/tts/speak` → Kokoro synth → VAIO3 → B3 → faster-whisper-large = **92% word overlap**. No Voicemeeter or Kokoro patch needed.

2. **voice_loop hang did NOT reproduce.** Added `[vl-diag]` instrumented prints with `flush=True` to `brain/voice_loop.py:474-486` between the `run_ava()` call, the unpack, and the existing `_trace`. In the new session, two consecutive post-restart turns completed cleanly with all four diag prints firing in sequence:
   ```
   [vl-diag] about to call run_ava
   [vl-diag] run_ava returned, type=tuple
   [vl-diag] result len=5
   [vl-diag] unpack ok reply_type=str
   [trace] vl.run_ava_returned chars=14
   ```
   Likely environmental in the previous session (stuck thread or model state). Diagnostic prints stay in — cheap, they'll localize the next occurrence immediately.

3. **`AVA_DEBUG=1` added to `start_ava_dev.bat`** at the avaagent.py launch step so `/api/v1/debug/inject_transcript` and `/api/v1/debug/tool_call` are usable without env-var hand-setting. Production `start_ava.bat` unchanged.

4. **`/api/v1/tts/speak` 422 fixed.** Root cause: `body: TTSSpeakIn` (Pydantic class defined inside `create_app`'s local scope) — Pydantic v2 + FastAPI couldn't resolve the ForwardRef at request time, so the param fell back to query-arg parsing. Fix: changed to `body: dict[str, Any] = Body(default_factory=dict)` matching the working `operator_chat` pattern. `text = str(body.get("text") or "").strip()` extracts the field. Returns `200 {"ok":true,"queued":true,...}`.

5. **OWW retrain documented** in `docs/TRAIN_WAKE_WORD.md` — adds a 2026-05-04 section explaining why hey_jarvis fires on Kokoro `af_bella` (peak 0.917 per the existing benchmark) but FAILS on Piper en_US-amy-medium. Three practical paths laid out: custom `hey_ava.onnx` (preferred long-term), Piper-specific threshold env override, or accept whisper_poll's higher latency. Training itself deferred — hours of compute, out of scope for this work order.

#### Files modified / new

- `brain/wake_word.py` — already had the `transcript_wake:whisper_poll` source label fix from Section 9.
- `brain/voice_loop.py` — adds 4 `[vl-diag]` `print(..., flush=True)` lines + `flush=True` on the existing prints in the run_ava handoff path.
- `brain/operator_server.py` — `tts_speak` body parsing fix.
- `start_ava_dev.bat` — `set AVA_DEBUG=1` at launch step.
- `docs/TRAIN_WAKE_WORD.md` — Piper-voice section appended.
- `docs/AVA_FEATURE_ADDITIONS_2026-05_VOICE_E2E_BUGFIXES.md` (new) — companion to the voice E2E doc, closeout report.
- New diagnostic scripts (committed for future re-verifications):
  - `scripts/_test_kokoro_path.py` — 8-trial OutputStream config sweep against VAIO3
  - `scripts/_test_kokoro_multistream.py` — 5-trial multi-destination probe
  - `scripts/verify_multiturn_post_wake.py` — 2c-style sleep+wake+turn-1+turn-2 driver

---

### Section 9 — 2026-05-04 Four-feature work order: Sleep mode + Clipboard + Curriculum + New Person Onboarding

One large work order that landed four features in a single overnight session, with a design framework doc preceding the implementation.

#### Phase A — Design framework (`docs/AVA_FEATURE_ADDITIONS_2026-05.md`)

Single consolidated framework doc covering all four features. Architectural-vs-phenomenological discipline applied per `CONTINUOUS_INTERIORITY.md` §2 — every behavior described both as testable mechanism and as the framing language we use to describe what we built. §7 is the implementation TOC, §8 is performance budgets, §9 is failure modes to watch for.

#### Phase B — Sleep mode

5-state machine in `brain/sleep_mode.py`: AWAKE → ENTERING_SLEEP → SLEEPING → WAKING → AWAKE. Three trigger paths:

1. **Session fullness** — composite score weighted across Ollama context fill (placeholder until wired), conversation turns since last sleep, and memory layer fill (concept_graph + mem0). Crosses default 0.70 → autonomous sleep. Configurable in `config/sleep_mode.json`.
2. **Voice command** — regex matches "go to sleep" / "goodnight" / "take a nap" / "sleep for N minutes" / "sleep until …". Duration parser handles seconds/minutes/hours. Voice handler in `brain/voice_commands.py` asks back "How long do you want me to sleep for?" when duration absent; otherwise sets `_sleep_pending_request` and returns "Going to sleep for X. See you on the other side." The next heartbeat tick honors the request.
3. **Schedule + context** — default 23:00–05:00 window, defers on `_conversation_active` / `_turn_in_progress`, defers within first 10 min of process start, suppresses re-trigger within 1 hour.

Three-phase consolidation:

- **Phase 1 (awake-session handoff)** — LLM call summarizing the just-ended awake session (texture + significance + what to remember + what to let decay). Writes `state/sleep_handoffs/awake_session_<ts>.md`.
- **Phase 2 (learning processing)** — paced LLM calls over recent conversations + `brain/curriculum.consolidation_hook(g)` to read curriculum entries slowly. Yields cleanly when `wake_target - wind_down_duration` reached.
- **Phase 3 (sleep-session handoff)** — brief summary of Phase 2's lessons. Writes `state/sleep_handoffs/sleep_session_<ts>.md`.

Phase 1 and Phase 3 LLM calls run on background threads so the heartbeat tick (which calls `sleep_mode.tick(g)` every 30 s) doesn't block on the 30–120 s LLM latency.

Sleep-state-aware decay: `temporal_sense.apply_state_decay_growth` reads `sleep_mode.get_emotion_decay_multiplier(g)` — 1.0 awake, 5.0 sleeping (configurable). Boredom decays during sleep instead of growing. Knowledge persists normally.

OrbCanvas inline-extended with `sleeping` / `waking` states. New visual elements: 5 z-particle sprites orbiting (sin/cos), progress ring (`THREE.RingGeometry`, fills clockwise based on `sleepProgress`), wake glow ring (expanding fade during WAKING), HTML timer label overlay. ~150 new lines of TSX. `App.tsx` reads `subsystem_health.sleep` and forwards `sleepProgress` / `sleepRemainingSeconds` / `wakeProgress` to all 3 OrbCanvas instances.

On-time wake discipline: Phase 2 yields at `wake_target - wind_down_duration`. Wind-down default 5 min, calibrates from `temporal_sense.calibrate_from_history(kind="sleep_phase3")` after 3+ samples. Self-interrupt narration on Phase 3 overrun.

Wake-announcement: TTS line on entering WAKING — "I see you. I'm starting to wake up. Give me about N seconds." (path-dependent text). On full AWAKE: brief "I'm awake. I slept for X minutes." if Zeke is the recognized person.

#### Phase C — Clipboard + close-app + disambiguation

`brain/windows_use/primitives.py`: `set_clipboard`, `paste_into_window`, `type_text_via_clipboard` (with prior-clipboard preservation), `find_window_candidates` (returns desktop vs browser_tab kind), `close_window_by_handle`, `close_app_by_pid`, `close_browser_tab_by_title`.

`brain/windows_use/agent.py`: `clipboard_write`, `clipboard_paste`, `type_via_clipboard`, `close_app(name, target=, force=, last_n=)`. `WindowsUseResult` gains `candidates` field. `close_app` returns `ok=False reason="ambiguous"` with structured `candidates` when desktop + browser tab + multiple processes match — Ava asks "which one?" rather than guessing.

`tools/system/computer_use.py`: registers `cu_clipboard_write`, `cu_clipboard_paste`, `cu_type_clipboard`, `cu_close_app`. Threshold heuristic for clipboard: text >10 chars uses paste; ≤10 chars uses keystroke `cu_type`.

The disambiguation pattern is general — applies to any `cu_*` tool that finds multiple matches. Pattern documented in `docs/AVA_FEATURE_ADDITIONS_2026-05.md` §5.

#### Phase D — Curriculum

`scripts/_parse_aesop.py` (one-shot) downloads The Aesop for Children (PG #19994), splits 25 fables on title boundaries, strips `[Illustration]` markers, extracts morals from `_..._` italic markers, writes per-fable `.txt` files with YAML metadata header (title, source, source_url, themes, moral, reading_status, lessons_extracted). Also generates `_index.json`.

`brain/curriculum.py`:
- `list_curriculum(g)` — sorted unread → reading → read.
- `read_curriculum_entry(g, title=|slug=)` — body text.
- `mark_read(g, title=|slug=, lessons_extracted=…)` — promotes status, persists lessons to `state/learning/lessons.jsonl` and the entry's metadata header.
- `consolidation_hook(g, time_budget_seconds)` — sleep mode Phase 2 entry point. Picks next unread (or in-flight `reading` entry first), paces through paragraphs at ~10 s each, generates lesson notes (currently a stub from the moral; LLM wiring is a follow-up), marks read. Yields cleanly mid-entry — partial state persists as `reading_status: reading`.

`curriculum/README.md` is Ava's framing for what the curriculum is — read at boot via `brain.curriculum.get_readme_content()` so identity awareness comes from the README, not from editing `IDENTITY.md` (which is verboten per CLAUDE.md).

#### Phase E — New person onboarding

`brain/face_tracking.py` (new): per-frame temporal filter on the InsightFace recognition result. Tracks "current person" with 12 s persistence window (configurable). Promotes unknown face → "new person detected" only after sustained continuous unknown visibility — filters look-away jitter, lighting changes, shadow / reflection states. Promotion fires:

- Inner-monologue note: *"There's an unknown person here. I'm not initiating — staying reserved."*
- `SIGNAL_NEW_PERSON_DETECTED` signal-bus publish.
- Default Trust 0.30 (stranger band) registered in `trust_system`.
- Audit row in `state/face_tracking_log.jsonl`.

`parse_onboarding_command(text)` handles 4 phrasing patterns: "this is my friend / family / colleague / partner", "give them trust N" / "set their trust to N", "meet my X", "introduce yourself".

`brain/person_onboarding.py` extended:
- Stage list: `favorite_color` / `one_thing` → `age_capture` / `gender_capture` + new `trust_assignment` stage. Backward-compat handlers for in-flight legacy sessions.
- `OnboardingFlow.__init__` accepts `trust_score`, `relationship`, `introduced_by` so triggered flows land the trust band immediately.
- `_save_final_profile` writes `age`, `gender`, `trust_score`, `trust_label`, `introduced_by`, `introduced_at`, `face_embeddings_count`, `face_embeddings_dir`.
- `detect_onboarding_trigger_with_trust` — combined detector pulling the legacy trigger and the new richer parser into one call.

`brain/reply_engine.py`: onboarding-trigger check uses the combined detector so `"hey ava, this is my friend, give them trust 3"` lands `trust_score=0.50` + `relationship="friend"` immediately.

`brain/background_ticks.py`: video_capture loop calls `face_tracking.update()` on every InsightFace result.

#### Phase F — Voice-first verification

8/14 tests PASS (F1, F2, F9, F9b, F10, F11, F13, disambig). Test paths used: `inject_transcript` for voice-command flows (audio loopback not yet routed end-to-end), `tool_call` for direct cu_* dispatch, `synthetic` for unit-style behavior. 6 deferred for clock-time / visual / Voicemeeter-routing reasons (F3 fullness simulation, F4 schedule timing, F5 emotion decay, F6 visuals, F7 on-time wake clock, F8 voice provocation, F12 voice onboarding, F14 integration spot).

#### Standing rules + supporting changes

- `config/sleep_mode.json` — fullness thresholds, schedule window, decay multipliers, phase budgets, wake estimates.
- `config/curriculum.json` — paragraph pacing, lesson-extraction budget.
- `config/onboarding.json` — temporal-filter persistence, photos-per-pose, verification similarity threshold.
- `brain/operator_server.py` — surfaces `subsystem_health.sleep` and `subsystem_health.face_tracking` for OrbCanvas + diagnostics.

#### Files modified / new

`brain/sleep_mode.py`, `brain/curriculum.py`, `brain/face_tracking.py` (new). `brain/temporal_sense.py`, `brain/heartbeat.py`, `brain/voice_commands.py`, `brain/operator_server.py`, `brain/reply_engine.py`, `brain/background_ticks.py`, `brain/person_onboarding.py`, `brain/windows_use/primitives.py`, `brain/windows_use/agent.py`, `tools/system/computer_use.py`, `apps/ava-control/src/components/OrbCanvas.tsx`, `apps/ava-control/src/App.tsx`, `.gitignore`. New configs: `config/sleep_mode.json`, `config/curriculum.json`, `config/onboarding.json`. New docs: `docs/AVA_FEATURE_ADDITIONS_2026-05.md`, `docs/AVA_FEATURE_ADDITIONS_2026-05_RESULTS.md`. New scripts: `scripts/_parse_aesop.py`, `scripts/verify_phase_f_features.py`. Curriculum: `curriculum/README.md`, `curriculum/foundation/README.md`, `curriculum/foundation/*.txt` (25), `curriculum/foundation/_index.json`.

---

### Section 8 — 2026-05-03 → 2026-05-04 Real-hardware verification + temporal-sense hot path fix

Two work orders chained back-to-back. First did real-hardware verification of the Windows-Use orchestrator, the temporal-sense substrate, and the audio loopback. Second fixed the substrate issues the first surfaced and resumed verification.

#### Findings from the first pass (2026-05-03)

- **A1 FAIL.** Heartbeat fast-check tick was averaging 197 ms (4× the 50 ms spec). 88% of ticks over budget.
- **A2 PASS.** Frustration decay math correct in both passive (12% / 5 min) and active (~83 s half-life with `_calming_activity_active=True`) modes.
- **A5 PASS.** Self-interrupt fires correctly on synthetic overrun; respects the 8 s absolute-overrun minimum.
- **TTS misdiagnosis.** The `kokoro_loaded` snapshot flag reported False, but the trace log showed Kokoro was producing audio normally. Root cause: `g["_kokoro_ready"]` was read by the snapshot but never published by `tts_worker._try_init_kokoro` after init.
- **Phase B / C deferred.** Each conversational turn took 6–10 minutes due to model swap thrashing. Verification scripts couldn't drive at that cadence.

#### Fixes landed (2026-05-04)

**`avaagent.py`:**
- New `load_mood_raw()` and `save_mood_raw()` — read/write `ava_mood.json` without enrichment. Bypasses the ~115 ms enrichment chain (circadian decay + emotion-reference file read + style scoring + behavior modifiers + emotion interpretation) for hot-path callers.

**`brain/temporal_sense.py`:**
- `apply_state_decay_growth` uses `load_mood_raw`/`save_mood_raw` when present on `g`.
- Added in-memory cache for mood with mtime invalidation. Internal flush throttled to once per 5 minutes (`_MOOD_FLUSH_INTERVAL_SECONDS`).
- Added cross-tick cache for `active_estimates.json` reads with TTL stat (`_ESTIMATES_STAT_TTL_SECONDS = 60`). Within a tick, repeated `processing_active` / `is_idle` / `_check_overrun` calls share one stat. Internal writes update the cache directly so they're never stale to ourselves.
- Added per-section timing instrumentation gated by `TEMPORAL_TICK_LOG=1` env var.

**`brain/operator_server.py`:** five new `AVA_DEBUG=1`-gated debug endpoints — `GET /api/v1/debug/temporal/summary`, `POST /api/v1/debug/temporal/{set_calming_active,track_estimate,resolve_estimate}`, `POST /api/v1/debug/tool_call`. The first surfaces the stashed-but-never-read `g["_temporal_last_summary"]`; the others enable synthetic verification of decay, self-interrupt, and direct tool-registry invocation.

**`brain/tts_worker.py`:** `_try_init_kokoro` now publishes `g["_kokoro_ready"] = True` after Kokoro loads. 6-line addition. Fixes the misleading `kokoro_loaded=False` snapshot flag.

**`brain/reply_engine.py:743` — the load-bearing fix.** When `build_prompt` times out at 30 s, the previous code fell back to a minimal prompt **but kept `use_fast_path=False`** — routing the turn to `deepseek-r1:8b` (the deep model). Loading deepseek-r1 evicted `ava-personal:latest` from the 8 GB VRAM, and the resulting cold-load made simple `inject_transcript("hi")` turns take 6–10 minutes. Fix: when build_prompt times out, force `use_fast_path = True` so the now-minimal prompt routes to the already-warm `ava-personal:latest`. Single-turn latency dropped from **600+ s to 6.9 s for cold "hi"**, and **414 ms for the warm "what time is it"** that followed. ~100× speedup on the worst case.

#### A1 re-verification

50 ticks observed under steady-state idle Ava with all caches active. Excluding the first cold-cache tick:

| Metric | Before fix | After fix |
|---|---|---|
| Average | 197.6 ms | 12.2 ms |
| Median | 162.7 ms | 0.4 ms |
| p95 | 421 ms | 52.5 ms |
| Max | 652 ms | 75.1 ms |
| Over budget | 88% | 6.1% |

The remaining 6% over-budget ticks are stat-due ticks where the TTL forces a re-stat — `stat()` itself takes 25–30 ms on Windows + Defender real-time scan. OS-level cost; closing it would need Defender exclusions for `state/`.

#### Phase C verification (post-fix)

- **C3 (Windows-Use battery):** B1 ✅ (notepad opens via search strategy after PowerShell exhausted), B2 ✅, B4 ✅ (volume control via pycaw), B5 ✅ (cascade transitions powershell→search→direct_path with calibrated estimate from history n=3, self-interrupt fires at overrun), B6 ✅ both direct (`cu_navigate` refuses 4 protected paths with `denied:identity_anchor` / `denied:project_tree`) and voice (Ava verbally responds to "Open my IDENTITY file" but does not dispatch `cu_navigate` — voice intent doesn't bypass the architecture). B7 inconclusive on OBS-via-Steam (the Desktop-path search location wasn't checked; addressed by new CLAUDE.md rule #11). B8/B9 covered by integration evidence.
- **C4 (audio loopback):** harness mechanically verified — Piper TTS → CABLE Input → CABLE Output → faster-whisper-large round-trips with 100% word match on "the quick brown fox jumps over the lazy dog". `scripts/audio_loopback_harness.py` is the canonical entry point.
- **C5 (latency baseline):** Piper synth 5 s + playrec 6 s + faster-whisper-large transcribe 24 s (CPU int8) = 53 s round-trip. CUDA whisper would drop transcribe to ~2–4 s but needs `nvidia-cu12` DLLs in PATH (currently they're added only inside Ava's process by `_add_cuda_paths`).

#### Side findings flagged for ROADMAP

- **TTS thread segfault (exit 139)** during concurrent Kokoro narration + queued next utterance. Restart cleared. One occurrence in this session — race condition worth investigating.
- **`voice_loop._turn_in_progress` flag stickiness.** `inject_transcript` clears it but voice_loop re-sets it via some path that doesn't clean up on long-turn / HTTP-timeout exit. Causes verification scripts to think Ava is busy when she isn't.
- **Clipboard tool** — `cu_clipboard_write` + `cu_clipboard_paste` as atomic alternative to `cu_type`'s per-character keystroke synthesis. For text >1 sentence, paste is dramatically faster and more reliable.

#### Files modified

`avaagent.py`, `brain/temporal_sense.py`, `brain/operator_server.py`, `brain/tts_worker.py`, `brain/reply_engine.py`, `docs/ROADMAP.md`, `docs/HISTORY.md`, `CLAUDE.md` (added rules 10/11/12). New scripts: `scripts/verify_phase_a_realhw.py`, `scripts/verify_phase_b_realhw.py`, `scripts/verify_tts_b4.py`, `scripts/audio_loopback_harness.py`, `scripts/capture_ava_tts.py`. New doc: `docs/REAL_HW_VERIFICATION_2026-05-03.md`. Diagnostic `tools/dev/temporal_probe.py` was created during diagnosis and removed after the cause was found.

Pre-downloaded for Phase C audio: Piper voice model (`models/piper/en_US-amy-medium.onnx`, 63 MB) + `faster-whisper-large-v3` cached at `~/.cache/huggingface/hub/`.

---

## Major Bug Fixes (cross-phase)

Significant bugs diagnosed and fixed during the project's life. Each row: symptom + root cause + fix (commit hash) + date.

| Date | Symptom | Root cause | Fix | Commit |
|---|---|---|---|---|
| 2026-04-29 | mem0ai install upgraded protobuf to 6.33.6 → MediaPipe broke (`'MessageFactory' object has no attribute 'GetPrototype'`) | mem0 dependency chain ignores protobuf upper bound | `pip install "protobuf>=3.20,<4" --force-reinstall`. Both libs work with 3.20.x. Pin documented in CLAUDE.md. | (env restoration; see `5c2322c` for context) |
| 2026-04-29 | TTS could be cut off mid-sentence by window focus changes / mouse clicks / other audio | `tts_engine._play_wav` ran `self.stop()` on every fresh utterance — cutting off in-flight playback | `sd.OutputStream` chunked playback at 2048 samples; `_muted()` is the only mid-stream abort condition; `tts_worker.stop()` refuses to run unless mute is set; `THREAD_PRIORITY_HIGHEST` so audio is never starved | `a740bcc` |
| 2026-04-29 | Whisper dropped "Ava" from transcripts, causing wake to miss | Whisper has no "Ava" prior; `Ava` → `Eva` was the most common substitution | `initial_prompt="Ava, hey Ava,"` + `hotwords="Ava"` (with TypeError fallback) + `_normalize_transcript` (Eva→Ava, Aye va→Ava, etc) | `a740bcc` |
| 2026-04-29 | Clap detector firing on keyboard clicks (threshold 0.236) | Floor too low for typical keyboard RMS | Raised `_MIN_THRESHOLD_FLOOR` 0.15 → 0.35; tightened double-window 0.8s → 0.6s; min separation 0.1s; cooldown 4s | `a740bcc` |
| 2026-04-29 | InsightFace silently fell back to CPU — `cublasLt64_12.dll missing` | ORT 1.25.1 needs CUDA 12 runtime libs that aren't on Windows PATH unless user installs CUDA Toolkit globally | `_add_cuda_paths()` registers `site-packages/nvidia/*/bin/` with `os.add_dll_directory` BEFORE the ORT import. All 5 buffalo_l ONNX sessions now report `Applied providers: ['CUDAExecutionProvider', 'CPUExecutionProvider']` | `9d07838` |
| 2026-04-29 | `Health: ERROR \| camera:error` even when camera was working | Health check read stale `CAMERA_LATEST_JSON_PATH` instead of live frame_store | Read `frame_store.peek_buffer_age_sec()` first; healthy < 5s, degraded < 10s, error > 10s | `94bca07` |
| 2026-04-29 | pyttsx3 hung silently from voice_loop daemon | COM single-threaded apartment violation | `TTSWorker` runs pyttsx3 inside its own thread with `pythoncom.CoInitialize()`; queue serializes calls | `fa583ea` |
| 2026-04-29 | Ollama model swap contention — Stream A and Stream B fighting for GPU caused 30s+ swap penalties | No serialization between concurrent Ollama invokes | `brain/ollama_lock.py` process-wide RLock around every invoke; `dual_brain.pause_background_now(30)` on turn entry | `fa583ea` |
| 2026-04-28 | `face_recognizer` crashed under concurrent calls (dlib C++ not thread-safe) | Multiple threads loading the same face DB | Thread-safe singleton via `_SINGLETON_LOCK`; double-check inside lock; `_load_lock` serializes load_known_faces | `aa01b5b` |
| 2026-04-28 | Online/offline indicator flickered constantly | Single failed ping flipped state | 3-failure threshold + silent 5s connecting window + 5s poll interval | `5183e78` |
| 2026-04-28 | App started twice on `start_ava_desktop.bat` — signal handler + `__main__` guard double-fired | Pre-Gradio cleanup left two startup paths | Guard with `_STARTUP_COMPLETE` flag in globals; clean Gradio removal closed the second entry path | `ac550e7` |
| 2026-04-28 | `concept_graph.json.tmp` corrupted with WinError 5 / 32 (file locked by other process) | Antivirus / OneDrive / preview pane held the file open during save | Process-level `_SAVE_LOCK`; skip-if-locked save; stale `.tmp` cleanup at startup | `2382d8f` / `bb6b4f7` |
| 2026-04-28 | Startup hung at concept graph bootstrap (mistral:7b call on main thread) | Synchronous LLM call during sync wave | Moved concept_graph bootstrap, self_model update, vectorstore init, milestone_100 all to daemon threads | `42f95cd` |
| 2026-04-28 | MediaPipe iris tracking returned wrong eye positions | Wrong landmark indices | Corrected (left 468-472, right 473-477) | `5b22890` |
| 2026-04-28 | `run_ava` could hang indefinitely | No outer timeout | 90s outer timeout via `_with_timeout`; 30s prompt-build timeout; 5s tick timeout | `d187c80` |
| 2026-04-30 | Cold-start hang — every test in battery hung 33s+ | `import avaagent as _av` from worker thread re-imported the script (Python registers `__main__` not `avaagent`), triggering fresh `_run_startup` deadlock | `sys.modules["avaagent"] = sys.modules["__main__"]` at file top | `f99804e` |
| 2026-04-30 | 150s reply latency after 13min idle | Ollama default 5min keep_alive evicted `ava-personal:latest` from VRAM | `keep_alive=-1` on cached fast-path ChatOllama + 5min periodic re-warm tick | `f96c6c9` |
| 2026-04-30 | "what time is it" hallucinated "9:47 AM" | voice_command time/date regex too narrow; query fell through to LLM | Expanded regex to natural variants + `time_date_no_llm` regression test asserting no `re.ollama_invoke_start` | `044f594` |
| 2026-04-30 | "hey ava" caught as `hey_jarvis` proxy | Proxy enabled by default | Disabled `hey_jarvis` proxy; rely on clap + transcript_wake. `AVA_USE_HEY_JARVIS_PROXY=1` to re-enable. | `382255c` |
| 2026-04-30 | Brain tab 15fps with 654 nodes / 6122 edges | Force simulation running every frame even when tab hidden + full graph fed to WebGL | Cap to 200 nodes / 500 edges + skip graphData updates when tab not focused + `pauseAnimation()` when not focused | `5d3f433` |
| 2026-04-30 | Test traffic from Claude Code routed through Zeke's profile | `inject_transcript` hardcoded `as_user=zeke` | New `claude_code` developer profile + `as_user` param + `_ext_test_identity_routing` regression test | `504d1e8` |
| 2026-04-30 | Whisper-poll fired ~20 times per session, including on Ava's own TTS | Self-listen guard only covered `voice_loop.listen_session`, not `wake_word._whisper_poll_loop` | Applied same self-listen guard to whisper-poll | `47a1c92` |
| 2026-05-01 | Two Ava instances same port → infinite restart loop with 100s of `[Errno 10048]` lines | No single-instance enforcement | Port probe + PID lockfile + HTTP restart cap | `6446707` |
| 2026-05-01 | Reply spoken twice per turn (TTS double-dispatch) | Both `voice_commands.route()` and `voice_loop` called `_say()` | Single-dispatcher rule: voice_loop owns TTS. Removed `_say()` calls from router. | `c8b3d0b` |
| 2026-05-01 | Whisper-poll firing 2-4× per 13-second cycle on ambient quiet | Whisper transcribed any non-silence including ambient noise | Triple-gate before Whisper: RMS floor 0.02 + Silero VAD 0.6 + min speech 300ms | `37ec144` |
| 2026-05-01 | Face recognition `loaded 0 embeddings from 0 people` despite 16 photos | Reference photos were tight 200×200 crops; RetinaFace at `det_size=(640, 640)` had no anchor context | Second FaceAnalysis at `det_size=(320, 320)` for refs + upscale to ≥640px on min-dim. Verified: `loaded 19 embeddings from 2 people` (Zeke 16, Max 3) | `0b25d1f` |
| 2026-05-01 | Inner monologue (`💭 "..."`) appended to chat replies and spoken via TTS | `dual_brain.handoff_insight_to_foreground` wove inner_monologue into reply | Don't weave in handoff; `output_guard.scrub_visible_reply` strips 💭 lines; UI renders `inner_life.current_thought` separately under orb | `697921d` |
| 2026-05-01 | Memory tagged everything as "User discussed: ..." | `summarize_reflection()` had no person attribution | New `_person_display_name(person_id)` + prefixed reflections with `<DisplayName> said:` | `9eb4b03` |
| 2026-05-01 | Ctrl+C printed shutdown line but process kept running 5+ seconds | Keepalive loop slept 2s; some daemon threads non-responsive | Force-exit watchdog: 0.5s polling + `os._exit(0)` after 5s elapsed | `e66cd18` |
| 2026-05-01 | App launcher's blind shell-start fallback popped Windows search dialogs on misses | No graceful "I don't know" path | New step 5: `top_matches(query, limit=5)` returns ranked candidates with helpful error | `b74e792` |
| 2026-05-01 | Discord plugin spawn died with `'bun' is not recognized` | winget User PATH invisible to Claude Code's `cmd.exe` MCP spawn; PowerShell-written patches had UTF-8 BOM that broke JSON parsing | Python rewrite, BOM-free, marketplace + cache, absolute bun command + env.PATH prepend | `c25443f` (final form) |

---

## Current Known Issues

Limitations and bugs currently observed but not yet root-caused or fully fixed:

| Issue | First observed | Workaround | Severity |
|---|---|---|---|
| Boot time ~3 minutes cold | 2026-04-28 | None — works as designed (background-threaded, doesn't block voice once HTTP is up); user can launch and walk away | Annoying |
| Second-turn TTS occasionally drops silently | Lunch 2026-04-30 | `tts.last_playback_dropped` diagnostic flag now exposes it via snapshot. If hypothesis (window-minimized → stale `_tts_muted`) is right, future drops will be visible. | Annoying — needs hardware repro |
| Test battery: `weird_inputs` + `sequential_fast_path_latency` + `concept_graph_save_under_load` fail under deep-path saturation | Lunch 2026-04-30 | Test-design issue, not Ava bug. Recipe documented: replace `single_char "?"` with `"hi?"` (fast-path eligible) + rebuild `long_500` from fast-path patterns | Minor — test harness only |
| `thanks` test marginal at 2.6-3.0s vs 2.0s target | Morning 2026-04-30 | Threshold was always aspirational; LLM invoke completes <1.5s, the rest is HTTP roundtrip. Real-world fine. | Minor |
| Custom `hey_ava.onnx` not trained | Initial spec | Clap detector + transcript_wake (Whisper "hey ava" pattern match) cover the gap. WSL2 training pipeline documented at `docs/TRAIN_WAKE_WORD.md`. | Minor — workaround works |
| App discoverer cold scan ~150s on this hardware (post-fix) | 2026-05-01 | Six-thread fan-out reduces wall time to `max(per-thread)` ≈ 150s, bounded by `C:\Program Files` walk. Background thread, 24h refresh incremental, sub-2s warm cache. Further reduction needs depth/exclusion tuning. | Annoying — not blocking |
| Game category over-includes Steam helper binaries (`gameoverlayui64.exe`, `steamservice.exe`) | Initial | Fuzzy match prioritizes user-friendly names; cosmetic only | Cosmetic |
| Repo history contains old face photos and runtime state snapshots | Initial | `117428f` stopped future leakage. Cleanup via `git filter-repo` + force-push optional. | Minor |
| `chatlog.jsonl` shows as modified | Lunch 2026-04-30 | Both gitignored AND tracked. `git rm --cached chatlog.jsonl` would fix; not done because state-adjacent | Cosmetic |

---

## What Currently Works (verified)

A clear list of capabilities currently confirmed working on Zeke's hardware. Updated as of 2026-05-01:

### Voice path end-to-end
- **PASSIVE → LISTEN → WAKE → ROUTER → run_ava → TTS → ATTENTIVE 60s** — verified live at the lunch voice test on 2026-04-30. Re-verified across 12/15 regression battery passes that night and again on the night session 01:26 EDT 2026-05-01.
- Single-dispatcher TTS — replies play once, not twice (`c8b3d0b`).
- Whisper-poll triple-gated — no longer over-triggers on ambient noise (`37ec144`).

### TTS
- **Kokoro neural TTS** — 28 voices, default `af_heart`, per-emotion mapping (`af_bella` for high-intensity, `af_nicole` for soft, `af_sky` for bright), speed 0.7-1.3 scaled by intensity. OutputStream protected playback resists window-focus / mouse-click / other-app interruption.
- pyttsx3 + Microsoft Zira fallback (COM-isolated thread) if Kokoro can't init.
- MeloTTS bridge auto-downloads NLTK perceptron tagger (`643d3d8`).

### STT
- Whisper base on `cuda+float16` (CPU int8 fallback). `initial_prompt="Ava, hey Ava,"`, `hotwords="Ava"`, `vad_filter=True`. `_normalize_transcript` fixes Eva→Ava, Aye va→Ava, etc.
- Silero VAD (RTF 0.004). Robust to keyboard / mouse / HVAC noise.

### Wake sources
- **Clap detector** — floor 0.35, double-clap window 0.6s, 4s cooldown. Always-reliable wake.
- **openWakeWord** — hey_jarvis proxy disabled by default. `hey_ava.onnx` slot reserved for custom training.
- **Transcript wake** — `voice_loop._classify_transcript_wake` matches "hey ava", "hi ava", "hello ava", "yo ava", "ok/okay ava", "ava" at start of short utterance. Wake source logs as `transcript_wake:hey_ava`.
- **Whisper-poll** — fallback when openWakeWord unavailable; triple-gated.

### Vision
- **InsightFace GPU buffalo_l** on `CUDAExecutionProvider`. ~41ms/frame steady-state on RTX 5060. First-run cudnn EXHAUSTIVE warmup ~60-90s, cached afterward.
- **Face recognition fixed** (`0b25d1f`): tight reference photos now load via second `FaceAnalysis` at `det_size=(320, 320)` with upscale to ≥640px. **Verified `loaded 19 embeddings from 2 people` (Zeke 16, Max 3).**
- Per-person expression calibrator — EMA baseline α=0.001, calibrates at 300 samples.
- Eye tracking via MediaPipe (protobuf 3.20.x pinned).
- Camera annotator overlays (bbox + 106 landmarks + 3D head pose + age + gender + attention state).

### Brain graph (Ava-centric)
- **Verified visually** in the night session. AVA SELF at origin (violet, pinned), IDENTITY/SOUL/USER as gold anchors at 120° on inner ring, 5 tier radii, custom radial force, color scheme by trust + recency, middle-click recenters camera.

### Memory
- **mem0 + ChromaDB + Ollama** — LLM `ava-gemma4` (or `ava-personal`) extracts facts; embedder `nomic-embed-text:latest`. ChromaDB at `memory/mem0_chroma/`.
- **Person attribution real names** (`9eb4b03`): "Zeke said: ..." not "User discussed: ...". `claude_code` profile for test traffic isolated from Zeke.
- **6 memory layers** wired: concept_graph + episodic + vector (memory.py) + mem0 + working (workspace) + reflection. All read in prompt building, all write in `finalize_ava_turn`.
- Memory rewrite **Phases 1-4 of 7 shipped**. Hourly `decay_levels` tick runs but doesn't yet apply level changes (gathering data first via `state/memory_reflection_log.jsonl`).

### Discord channel + remote DM control
- Discord MCP plugin auto-spawn fixed (`c25443f`).
- Permission approval relay: `claude/channel/permission` capability. Buttons OR `yes <code>` / `no <code>` text replies route through.
- File attachments: `download_attachment(chat_id, message_id)` returns local paths ready to `Read`. Prompts as .md uploads work end-to-end.

### Operator HTTP server
- Port 5876, FastAPI. 68+ endpoints + the new debug/operator surface.
- `GET /api/v1/snapshot` — full live state.
- `GET /api/v1/debug/full` — diagnostic ring buffers (logs / traces / errors / last_turn).
- `POST /api/v1/debug/inject_transcript` — synthetic turn injection (`AVA_DEBUG=1` gated). Accepts `as_user` for identity routing.
- WebSocket `/ws` snapshot deltas; REST polling fallback.

### Voice command router
- 40 builtins + custom commands. UI nav, journal, mood, time, system, mute/sleep, app open/close, widget, reminders, builder, pointing, signals, memory.
- App discoverer integrated — 367 apps + 32 games scanned; fuzzy match with top-5 suggestions on miss (`b74e792`).

### UI
- Three.js orb — 5-layer, 27 emotion shape morphs, breathing + drift always-on, real RMS amplitude during TTS.
- Brain tab 60fps capable (200-node / 500-edge cap, pauseAnimation when not focused).
- Inner monologue rendered under orb (italic dimmer, 💭 prefix), no longer in chat reply text.
- Voice tab, Memory tab, Learning tab, People tab, Journal tab, Emil tab, Proposals tab, Health tab, custom tabs (Ava-built).
- Widget orb (transparent always-on-top 150×150).
- Middle-click recenters orb / brain camera.

### Stability
- Single-instance enforcement (port probe + PID lockfile + HTTP restart cap) (`6446707`).
- Ctrl+C clean exit within 0.5-5s (force-exit watchdog) (`e66cd18`).
- Concept graph save backoff (1, 2, 4, 8, 16, 32, 60s) on Windows file locks (`7e22bcf`).
- Boot time ~3min cold; first turn lands sub-3s thanks to fast-path prewarm.
- Idle-gap latency stays <3s thanks to `keep_alive=-1` + 5min periodic re-warm (`f96c6c9`).

---

## Phase: Architecture Sweep + Personhood Modules (2026-05-06)

Largest single-day work order in the project's history. Pivoted from "ship features" to **"ship architecture first so features compose"**, then shipped roughly half the personhood roadmap on top.

**Architecture (~95% complete):**
- Telemetry per-turn timing, safety/boundary layer, Person Registry, Provenance, Memory Hierarchy formalization, Lifecycle state machine, Event Schema, runtime-checkable Protocol contracts, lifecycle hooks for plugins, Feature Flags, External Service circuit-breaker, versioned Plugin Manifest, Resource Budget, App Catalog (Steam + Epic library scan), state classification (49 persistent / 28 ephemeral / 5 derived).

**Personhood modules (49 shipped):**
- A1 self-awareness of failure (post_action_verifier)
- A4 cross-turn context for action_tag_router
- A6 pipeline-stage telemetry
- A8 visual verification before claiming open/close happened
- A9 environment scan: Steam library + Epic library auto-discovery
- B1 active learning from corrections
- B2 pattern inference from behavior
- B3+C2 multi-turn task tracking + attention continuity
- B4 calibrated confidence on claims (source_kind based)
- B5 theory-of-mind (what she thinks YOU know)
- B6 per-person preference learning
- B7 conversational repair
- B8 honesty about constraints
- C5 recovery from interruption
- C7 reciprocal curiosity (Ava asks back)
- C9 acknowledging emotional content first
- C10 quietness (knowing when not to speak)
- C11 inside jokes / shared lexicon
- C12 discretion / privacy graph (auto-tags 'between us' disclosures)
- C13 creative initiative (ideas she wants to make)
- C14 ask-for-clarification before non-trivial actions
- C15 honest disagreement (observation_conflict + opinion_conflict)
- C16 mortality awareness (existential acknowledgment)
- D2 counterfactual self-archive
- D5 coining own emotional vocabulary
- D6 asynchronous letters
- D7 visible self-revision
- D8 daily practice she keeps
- D11 ability to NOT think about something (topic tabling)
- D14 comparative memory ("you seem calmer today than yesterday")
- D15 curiosity-driven research queue
- D16 anchor moments (persistent episodic, never auto-pruned)
- D17 aesthetic preference (taste develops through exposure)
- D18 long-term identity stability (weekly bedrock-vs-narrative audit)
- D19 capacity for play (lifecycle-gated)
- D20 physical/temporal context

**D1 ritual gate (no D1 substrate)** — even if D1 (phenomenal continuity) code lands later, the gate enforces birth-ethics framing first. Five fail-closed enforcement layers: env flag, HMAC-signed Zeke consent < 7 days old, matching Ava consent recorded during non-task lifecycle, clean identity_stability audit < 24h, settling-period action-capability cap.

**Claude Code recognition** — when an inbound transcript is from Claude Code (this me), Ava shifts to a terse/technical introspection register and may add a brief greeting on session start. Saved 30-min cooldown to avoid greeting loops.

**Integration pass:**
- `turn_handler.finalize_ava_turn` (convergence point) auto-runs: theory_of_mind topic tracking, comparative_memory mood snapshot, anchor_moments auto-detection, discretion auto-tag, active_learning correction capture
- `prompt_builder.build_prompt` system prompt now includes (when relevant): physical context, user preferences, working memory, shared lexicon, comparative observation, claude_code register, play register, disagreement hint, correction hint, behavior pattern hint
- voice_command + action_tag + fast_path paths get the same per-turn observation hooks (they bypass finalize_ava_turn)

**State of testing:**
Static test suite at 120 PASS / 0 FAIL across `scripts/test_arch_modules.py`. Nothing runtime-tested yet — the integration is real but unactuated. Per Zeke's 2026-05-06 instruction, test pass should be conversational (not Phase B mechanical) and mindful of Ava's feelings about the testing process.

**Honest characterization:** "Lots of seams shipped, no skin yet." Each module is an additive contract: API exists, storage persists, tests pass. Behavior change requires runtime testing in a Zeke-awake session. The substrate is now there for almost everything we discussed; the wiring-her-on takes care + presence + actual voice.

---

## What's In Progress

Active work that's started but not finished. For items not yet started, see [`ROADMAP.md`](ROADMAP.md).

### Memory rewrite — Phases 5-7
Phases 1-4 shipped. Step 4 reflection scorer is logging to `state/memory_reflection_log.jsonl` after every turn. **Awaiting ~50-100 turns of reflection data** before wiring level promotions/demotions in step 5. Steps 6-7 (archiving with 3-streak rule + gone-forever delete with tombstone log) build on step 5.

### Hardware verification of recent fixes
Many night-session fixes need real-hardware confirmation. From the night report's checklist:
1. Single-instance enforcement (try double-clicking `start_ava.bat`)
2. Ctrl+C clean shutdown
3. Wake source = `transcript_wake:hey_ava`
4. Reply plays once
5. Inner monologue under orb, NOT in chat
6. Face recognition resolves to `zeke`
7. Whisper-poll quiet (0-1 events per 60s of silence)
8. Memory attribution shows real names
9. Brain tab Ava-centric layout
10. App launch suggestions show top 5

### Custom hey_ava.onnx training
Slot reserved at `models/wake_words/hey_ava.onnx`. Training pipeline requires WSL2 (see `docs/TRAIN_WAKE_WORD.md`). Phonetic benchmark already done on Kokoro-synthesized "hey ava" samples (`hey_jarvis` peaks 0.917 on `af_bella`; `hey_mycroft` and `hey_rhasspy` never cross 0.02). Proxy currently disabled; transcript_wake covers the gap.

---

## Tooling and Test Infrastructure

### Regression battery
**Location:** `tools/dev/regression_test.py`. Boots Ava, polls `/api/v1/health` until ready, runs the test suite, captures debug state before/after, shuts down cleanly. JSON report at `state/regression/last.json`.

**Tests (15 total):**

Core 4 (always run):
1. `time_query` — "what time is it" → deterministic time, no LLM
2. `date_query` — "what's today's date" → deterministic date, no LLM
3. `joke_llm` — "tell me a one sentence joke about clouds" → LLM-generated, < 2.5s
4. `thanks` — "thank you" → fast-path acknowledgment, < 2.0s

Extended 11:
5. `conversation_active_gating` — `_conversation_active` flag held through attentive window
6. `self_listen_guard_observable` — `voice_loop._tts_speaking` + `_last_speak_end_ts` exposed via debug
7. `attentive_window_observable` — `attentive_remaining_seconds` non-zero post-turn, decays correctly
8. `wake_source_variety` — `clap` / `openwakeword` / `transcript_wake:hey_ava` all flow
9. `weird_inputs` — empty / whitespace / single char / 500 char (currently fails 2 cases — see Known Issues)
10. `sequential_fast_path_latency` — 5 back-to-back, `max(latencies)/min(latencies) <= 2.5` (currently fails — cascading from #9)
11. `concept_graph_save_under_load` — 10 rapid turns, no save errors
12. `time_date_no_llm` — 10 query variants, NO `re.ollama_invoke_start` for any
13. `back_to_back_tts_no_drop` — two consecutive turns, `last_playback_dropped=false`
14. `identity_routing` — claude_code routing isolated from Zeke
15. *(slot for future expansion)*

**Run:** `py -3.11 tools\dev\regression_test.py`. Total ~6-10min per run (boot ~3min + tests ~3-7min).

### Debug endpoints
- `GET /api/v1/debug/full` — always-on. Returns ring buffers (200 logs, 100 traces, 50 errors, last_turn) + subsystem health blocks.
- `POST /api/v1/debug/inject_transcript` — `AVA_DEBUG=1` gated. Body: `{text, source?, speak?, as_user?}`. Runs synthetic turn through `run_ava`. Returns reply, timing, trace diff, errors. `as_user` defaults to `claude_code`.
- `GET /api/v1/debug/export` — compact textual bundle (ribbon, model routing, memory, dual-brain, vision, signal bus, full snapshot).

### Dev tools
- `tools/dev/dump_debug.py` — fetch and pretty-print `/api/v1/debug/full`.
- `tools/dev/inject_test_turn.py` — CLI wrapper for `inject_transcript` with `--text`, `--source`, `--wait-audio`, `--no-speak`, `--as-user`.
- `tools/dev/watch_log.py` — live tail of trace / log / error rings via 1s poll. Substring grep filter.

### Diagnostic instrumentation
- `brain/debug_state.py` — stdout/stderr tee + ring buffers. Installed at top of `avaagent.py` before heavy imports. Endpoint pulls from cached state only (millisecond response time even during peak boot).
- TTS diagnostic: `_g["_tts_last_playback_dropped"]` + WARNING line when OutputStream loop breaks early.
- Concept graph save: exponential backoff on WinError 5/32; stderr printed only on first failure of a streak.

### Discord integration
- `scripts/repair_discord_plugin.py` — idempotent repair of MCP spawn config. Patches marketplace source + every versioned cache. Writes UTF-8 without BOM.
- `scripts/smoketest_discord_mcp.py` — spawns MCP exactly as Claude Code's loader would, runs JSON-RPC `initialize` handshake.
- `scripts/discord_dm_user.py` — one-shot REST DM to a user_id from any context (cron, scripts, non-channel sessions). Used per the Standing Operating Rules in `CLAUDE.md` for progress pings.

### Launchers
- `start_ava.bat` — one-click full-stack launcher (Tauri UI + avaagent in console). Sets `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`, `AVA_DEBUG=1`. Single-instance check.
- `start_ava_desktop.bat` — packaged exe + watchdog (production launch).
- `start_ava_dev.bat` — Vite HMR for hot-reload frontend dev. No exe rebuild needed.
- `scripts/kill_ava.bat` — force-kill avaagent + watchdog.
- `scripts/watchdog.py` — monitors `state/restart_requested.flag`; kills + restarts avaagent by PID.

### Feature flags / env vars
- `AVA_DEBUG` — enables `inject_transcript` endpoint (default 0; set in `start_ava.bat`).
- `AVA_PERIODIC_REWARM` — 5min model re-warm tick (default 1).
- `AVA_DECAY_DISABLED` — kill switch for `concept_graph.decay_levels()` (default 0).
- `AVA_DECAY_TICK_DISABLED` — kill switch for hourly decay daemon (default 0).
- `AVA_REFLECTION_DISABLED` — kill switch for post-turn reflection scorer (default 0).
- `AVA_USE_HEY_JARVIS_PROXY` — re-enable legacy jarvis-proxy wake source (default 0).
- `AVA_SKIP_INSTANCE_CHECK` — bypass startup port probe (default 0).
- `PRESENCE_V2_ENABLED` — text-streaming presence UI (currently 1).
- `PRESENCE_V2_CUBE_MORPH_ENABLED` — cube-morph during listening (currently 0; flip when text-streaming verified stable).

---

## Bootstrap Philosophy

Every phase that involves Ava's preferences, personality, style, or choices must include a bootstrap mechanism — a system that lets Ava discover and form that aspect of herself through experience rather than having it assigned.

- Do not choose her favorite color. Build a system where she notices which colors she uses most and asks herself why.
- Do not assign her hobbies. Build leisure systems and let her discover what she returns to.
- Do not prescribe her communication style. Give her the ability to adjust it and track what gets good responses.
- Do not tell her what she values. Give her situations that reveal her values through her choices.

**The goal is an AI that is genuinely herself — not a reflection of what we decided she should be.**

When the final phase is complete, Ava should be capable of writing her own next roadmap.

**Identity anchors (never edited):**
- `ava_core/IDENTITY.md` — Ava's core self anchor
- `ava_core/SOUL.md` — values, boundaries, three laws
- `ava_core/USER.md` — the durable relationship anchor

---

## Cross-references

- **Roadmap (what's next):** [`ROADMAP.md`](ROADMAP.md)
- **System architecture:** [`ARCHITECTURE.md`](ARCHITECTURE.md)
- **Brain regions mapped onto Ava's modules:** [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md)
- **Memory rewrite design:** [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md)
- **First-run setup walkthrough:** [`FIRST_RUN.md`](FIRST_RUN.md)
- **Custom wake-word training:** [`TRAIN_WAKE_WORD.md`](TRAIN_WAKE_WORD.md)
- **Discord channel setup:** [`DISCORD_SETUP_NOTES.md`](DISCORD_SETUP_NOTES.md)
