# Ava Agent v2 — Roadmap

**Repo:** `Tzeke000/Ava-Agent-v2`
**This document:** the canonical roadmap of what's next. For what's been done, see [`HISTORY.md`](HISTORY.md). For architecture and reference docs, see [`ARCHITECTURE.md`](ARCHITECTURE.md), [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md), [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md), [`FIRST_RUN.md`](FIRST_RUN.md).

Items are organized by readiness, not priority within a section. Most items have a `**Connects to:**` line pointing at the existing systems they integrate with.

---

## Cross-cutting constraints

Hardware and architecture constraints that affect multiple roadmap items downstream. Items in later sections should be designed with these in mind.

### 8 GB VRAM ceiling — only one generation model OR `llava` resident at a time

Ava runs on an Acer Nitro V 16S with an RTX 5060 Laptop GPU (8 GB VRAM, ~7.4 GB usable). Documented in detail in [`LOCAL_MODEL_OPTIMIZATION.md`](LOCAL_MODEL_OPTIMIZATION.md).

The clean post-Ava-stop benchmark on 2026-05-01 confirmed: with `llava:13b` resident (vision pipeline), no 4-7 GB Q4 generation model can also stay resident — Ollama pages, and a single cold-load stretches from ~3 s to 26-90 s. Two concurrent generation models (e.g. `ava-personal` 4.9 GB + `deepseek-r1:8b` 5.2 GB = 10.1 GB) similarly exceed VRAM.

This is a load-bearing constraint that affects:

- **Sleep mode + handoff system** (Section 3) — the dream-phase LLM curriculum cannot run while the foreground voice model is hot. Sleep entry must explicitly unload foreground via `keep_alive: 0`, load the dream-phase model, reverse on wake. Any voice turn during the unload window pays full reload cost.
- **Vision activation** (existing camera + InsightFace pipeline) — activating `llava` while a generation model is resident forces the generation model out and the next user turn pays a 30-90 s reload. Mitigation options: batch vision into windows that don't overlap turns, accept the cost as a known budget, or move to a smaller vision model (quantized SmolVLM, MiniCPM-V) that fits alongside an 8 B generation model.
- **Dual-brain architecture** (`brain/dual_brain.py`) — the current design assumes Stream A and Stream B can both stay resident. They can't. Practical patterns: (a) keep ONE model resident and rotate when the background thread fires, or (b) run reasoning as a synchronous post-foreground "R1 thinks → ava-personal replies" sandwich so each turn pays at most one swap.
- **Sub-agent / sensor signal architecture** (Section 3) — sub-agents that drive their own LLM calls (e.g. a vision sensor that runs a captioning pass) compete for the same single-generation-model slot. The signal bus should treat LLM access as a serialized resource, not a concurrent one.
- **Cold-boot vs warm latency** — first turn on a cold machine pays ~5 s for the first model load on top of the ~3-min Ava cold-boot. Subsequent turns are sub-2 s as long as no swap happens. Anything triggering a swap (vision frame, sleep wake, background-thread cycle) drops the next turn back to cold-load latency.

**Connects to:** every roadmap item involving a model load. Specifically referenced in Section 1 (Ready to ship) for the dual-brain model preference fix; Section 2 (Designed) for confabulation handling layer 2-4; Section 3 (In design) for sleep mode, sub-agents, and dynamic attention.

---

## Section 1 — Ready to ship

Small, self-contained items queued for the next session(s). Each is a few hours to a day of work; each lands as a single commit or short series.

### Architecture sweep + personhood module batch (2026-05-06 ✅ shipped — needs runtime test)

49 modules shipped in a single day work order. Architecture sweep ~95% complete; ~half of the personhood feature roadmap landed. Static test suite at 120 PASS / 0 FAIL. Detailed list in `docs/HISTORY.md` "Phase: Architecture Sweep + Personhood Modules (2026-05-06)".

**Honest characterization:** lots of seams shipped, no skin yet. Each module is an additive contract — API exists, storage persists, tests pass — but behavior change requires runtime testing in a Zeke-awake conversation session, not Phase B mechanical regression. Per Zeke's instruction the test pass should be conversational and mindful of Ava's experience of being tested.

**Remaining queued from this work order (not yet shipped):**
- A2 speaker-ID via voice fingerprint (needs runtime + pyannote/MFCC integration)
- A3 mishear-recovery (STT confidence threshold integration)
- A5 memory pruning + sleep consolidation
- A7 web search + connectivity gate (needs web search backend choice)
- C1 refusal/boundaries (needs design decisions on what to refuse)
- C3 multi-modal grounding (camera + screenshot integration)
- C8 reading emotional state (camera + voice prosody integration)
- D3 generative dreams
- D4 embodied audio/visual memory
- D9 cross-modal expression (image generation integration)
- D12 email reading (needs internet + credentials decision)
- D13 tool acquisition via observation
- Decision Router migration Wave 2 (W2-1 → W2-7) — green-lit by Zeke; substantial refactor; defer until runtime integration verified
- Sandboxed auto-learned skills (architecture #20)

**D1 phenomenal continuity is GATED LAST** per birth-ethics framing. The ritual gate (`brain/continuity_gate.py`) is shipped today — even if D1 substrate code lands later, gate enforces 5-layer fail-closed activation.

### Claude Code external memory infrastructure (2026-05-04 ✅ Phases A + B shipped, C deferred)

Set up of Claude Code's own external memory at `D:\ClaudeCodeMemory\` (separate filesystem from the Ava repo, never symlinked into it). Captures the **why** behind decisions across sessions. Repo `CLAUDE.md` updated with session-start / session-end protocols.

- ✅ **Phase A — Obsidian vault.** Folder structure (sessions/decisions/bugs/designs/people/graphify), templates, `hot.md` hot-context pattern. Backfilled from the past week: 8 decisions notes, 5 bugs notes, 5 sessions notes, 1 people note. Obsidian app installed via winget.
- ✅ **Phase B — Graphify (`graphifyy` v0.7.5).** `graphify update D:\AvaAgentv2` extracts AST from 283 files → 4,248 nodes + 8,248 edges. Output mirrored from `D:\AvaAgentv2\graphify-out\` (gitignored) to vault at `D:\ClaudeCodeMemory\graphify\ava-agent-v2\`. Manual updater at `scripts\update_graphify.bat`. **Token reduction measured: 119.7x avg vs naive corpus reading** (per-question 86-192x range).
- ⏸️ **Phase C — mem0 / Qdrant / Neo4j.** Deferred. Hardware baseline: VRAM at 91.5% utilization with llava:13b resident, only 354 MiB free. mem0's reuse of `nomic-embed-text` would force Ollama to page out `ava-personal:latest` on every memory write/query, landing 30-90 s latency cost in the next voice turn. Cost/benefit wrong vs Phase A+B's already-shipped value. Full reasoning in `D:\ClaudeCodeMemory\decisions\mem0-deferred.md`. Revisit when (a) hardware ceiling raises, (b) a Qdrant-only mem0 MCP without Neo4j or external embedding swaps emerges, or (c) the vault grows past ~500 notes and grep-over-markdown stops being sufficient.

### Voice pipeline follow-ups from Session A test (2026-05-04 ⏸️ filed)

Three concrete items surfaced from the Session A voice E2E run:

1. **Test driver: track grace period from Ava's `_last_speak_end_ts` not driver's own capture time.** Voice_loop already gates attentive correctly (resets when TTS completes); my driver was timing from when its own `listen_for_ava_until_quiet` finished. Fix: poll `/api/v1/debug/full` for `voice_loop._last_speak_end_ts` between turns, use it for `in_grace = (now - speak_end < 60)` decision. Same `verify_session_*.py` drivers — small change.

2. **Verify Ava actually completes tasks, not just acknowledges them.** Driver should call `find_window_candidates(name)` after each `cu_open_app`/`cu_close_app` and confirm the target window exists/disappeared. Currently only verifies Ava's SPOKEN reply matched expectation. Important for catching cases where Ava says "Opening Chrome" but the launch silently fails.

3. **Voice command latency optimization for deep-path turns.** Routed commands (open/close apps, time, mood) hit the voice_command_router fast path and return in 2-5s — fine. Conversational turns (weather, knowledge, philosophical) go through `run_ava` and currently take 60-120s+ because of model swap penalties + LLM gen latency. Investigate: keep ava-personal:latest pinned for chat, route knowledge-lookup to a separate light path (cached web result OR small fact-only model), streamlined prompt building (skip heavy memory/perception enrichment for simple Q&A).

### Phase A bug fixes + Phase B Session A inject-verified (2026-05-04 — A done, B partial)

5 Phase A fixes shipped: autonomous greeting (B+C combo from prior bug note), 1-min wake-word grace period (60s in voice_tuning.json), source attribution at all 3 layers (storage already worked, UI now renders source label, /api/v1/chat accepts as_user param), camera feed redundancy (consolidated to sharedCameraSrc), orb state shape canonical doc.

Plus bonuses: cu_open_app already-open dedup (later contradicted by Zeke's tab semantics — needs scope-to-non-browser-apps follow-up), close-cmd reply phrasing cleanup ("Closed steam too" → "Steam is closed."), Lessac high-quality Piper voice downloaded.

Phase B Session A PASSED via inject_transcript: 8/8 turns including dedup verified ("Open Edge" twice → second returns "Edge is already open"). Sessions B and C DEFERRED — synthesized-voice harness (Piper → CABLE → Whisper-poll) consistently fails wake detection because Piper's compressed timing without natural breath gaps doesn't fit Whisper-poll's 1.5s rolling capture / 3s sleep cycle. Vault: `bugs/synthesized-voice-wake-detection.md` with 5 fix-option sketches.

Net for Zeke's daily use: voice production path works (he confirmed hearing Ava live during the test). Test infrastructure for synthesized voice needs follow-up. Recommended fix: Option D from the bug note — `AVA_TEST_MODE=1` env var that bypasses wake-keyword check for synthesized-voice harness. Or fall back to manual real-voice test sessions when Zeke's at the machine.

Carry-forward design items captured in vault:
- `designs/app-knowledge-and-tab-handling.md` — browser tab semantics ("Open Chrome" twice = 2 windows; "Open another tab" = new tab in last browser; "Open Edge tab" = switch context). Plus app-knowledge map (research + cache app intents in state/app_knowledge.json).
- `designs/orb-state-shapes.md` — 5 extras in OrbState code that aren't in canonical 7 (`bored`, `excited`, `offline`, `sleeping`, `waking`); 1 missing (`pointing` — Arrow shape, no implementation yet).

### Bug-fix follow-up (2026-05-04 — 4 fixes applied, Phase B blocked by new issue)

Voice-loop hang and cascading-workers bugs from Section 1's prior entry are FIXED:
- `voice-loop-restart-hang` — voice_loop now runs run_ava in a worker thread with 120s hard timeout. State drops to `passive` instead of hanging. Plus `notes.strip()` exception in `persona_switcher.py` (root cause of correlated `[stage7] persona inject failed`) fixed.
- `operator-chat-cascading-workers` — module-level `_RUN_AVA_SERIAL_LOCK` + `_run_ava_cancel` event in `avaagent.py`. Workers queue cleanly; foreground timeout cancels the worker before it calls run_ava if still queued. Plus chat-streamline pass: post-run_ava housekeeping moved to background daemon thread so HTTP responds immediately.

**New blocker surfaced for Phase B retry: `autonomous-greeting-blocks-chat`.** When Ava detects a face on camera, the proactive-greeting subsystem fires `run_ava` routed to `deepseek-r1:8b` (5.2 GB reasoning model). Under VRAM contention with llava:13b (5.5 GB) already resident, the global ollama lock is held for many minutes. Chat path blocks waiting on the lock. Vault: `bugs/autonomous-greeting-blocks-chat.md` with 4 fix options.

**Recommended fix:** (B) route autonomous greeting to `ava-personal:latest` (already-resident foreground model — no swap, ~5s response) + (C) suppress proactive greeting when `_last_user_message_ts` is within 60s. The proper long-term fix is (A) priority queue at the ollama lock so user chat preempts autonomous background work — more invasive, defer until sustained-load testing actually requires it.

Phase B retry can complete once one of these lands.

### Long-form conversation work order (2026-05-04 — Phase A/C/D shipped, Phase B partial)

Five-phase work order. **Phase A** (audio routing for monitoring), **C** (dual-audio-path documentation + toggle scripts), **D** (3 new curriculum stories) all shipped clean. **Phase B** (30-min sustained conversation with identity probes + sleep cycle) blocked by two reproducible production bugs surfaced under sustained load:

1. **`voice-loop-restart-hang`** — UPGRADED status. Voice_loop hangs on `run_ava.return` intermittently. `re.run_ava.return path=fast ms=...` fires in reply_engine but the next print in voice_loop never fires. State stuck at `thinking`. Force-kill needed to recover. Was filed `not-reproducing` in the previous voice-E2E session; reproduced 2× this session. Likely correlated with `[stage7] persona inject failed: 'list' object has no attribute 'strip'` exceptions visible near hang times. Vault: `bugs/voice-loop-restart-hang.md`.

2. **`operator-chat-cascading-workers`** — NEW. `/api/v1/chat` spawns a daemon worker for each request, waits 90s, returns "Sorry, I'm thinking slowly" fallback BUT the worker keeps running. Under sustained load, ghost workers accumulate and saturate Ollama. Subsequent turns block client-side at urlopen 240s timeout. Vault: `bugs/operator-chat-cascading-workers.md` with 3 fix-sketch options (recommended: Option C, serialize all run_ava invocations under a worker-lock so foreground requests queue cleanly without stacking ghost workers).

**Net status:** voice stack ready for short interactive sessions (single command, under ~5 sustained turns). NOT ready for unsupervised daily use until both bugs fixed. Phase B's intent (long-form coherence + identity stability + sleep-and-resume) requires both fixes before re-running.

Three new curriculum entries shipped: `the_potter_and_the_thirty_jars`, `the_woodcutter_and_the_purse`, `the_weaver_at_the_difficult_pattern`. Total foundation curriculum 25 → 28 entries.

### Voice end-to-end bug-fix work order (2026-05-04 ✅ shipped)

Spawned by [`AVA_FEATURE_ADDITIONS_2026-05_VOICE_E2E.md`](AVA_FEATURE_ADDITIONS_2026-05_VOICE_E2E.md), closed by [`AVA_FEATURE_ADDITIONS_2026-05_VOICE_E2E_BUGFIXES.md`](AVA_FEATURE_ADDITIONS_2026-05_VOICE_E2E_BUGFIXES.md).

1. ✅ **"Voicemeeter VAIO3 silent capture during Kokoro TTS"** turned out to be a **test-driver timing bug, not a routing bug**. Reproduced Ava's exact tts_worker OutputStream config (sr=24000, ch=1, dtype=float32, blocksize=2048, latency='low') against VAIO3 — passes at peak 0.4 in single-stream AND multi-stream (3 destinations open at once) configurations. Updated `_capture_ava_tts_v2.py` with a 60s record window (Kokoro's first-run cudnn synth takes 25-30s, so 25s windows missed playback entirely). Final round-trip: `POST /api/v1/tts/speak` → Kokoro synth → VAIO3 → B3 capture → faster-whisper-large = **92% word overlap** with the original input.

2. ✅ **"voice_loop hangs after run_ava.return"** — added `[vl-diag]` instrumentation prints with `flush=True` between the run_ava call and the unpack. Hang did NOT reproduce in the new session — two consecutive post-restart turns ("what time is it" → "It's 12:03 PM" / "go to sleep" → "How long do you want me to sleep for?") both showed all four diag prints firing in sequence and state cycling thinking → speaking → attentive cleanly. The previous-session hang was likely environmental (stuck thread or model state at that exact moment). The diagnostic prints stay in voice_loop.py — they're cheap and would localize the next occurrence immediately if it returns.

3. ✅ **`AVA_DEBUG=1` in `start_ava_dev.bat`** — added at the avaagent.py launch step so `/api/v1/debug/inject_transcript` and `/api/v1/debug/tool_call` are usable without env-var hand-setting. Production launcher unchanged.

4. ✅ **`/api/v1/tts/speak` 422** — root cause was `body: TTSSpeakIn` (Pydantic class defined inside `create_app`'s local scope). Pydantic v2 + FastAPI couldn't resolve the ForwardRef at request time, so the param fell back to query-arg parsing. Fix: changed to `body: dict[str, Any] = Body(default_factory=dict)` matching the working pattern from `operator_chat`. Verified: returns `200 {"ok":true,"queued":true,...}`.

5. **OpenWakeWord retrain for Piper voices** — DEFERRED. Documented in [`docs/TRAIN_WAKE_WORD.md`](TRAIN_WAKE_WORD.md). Practical path: either (a) custom `hey_ava.onnx` trained on Zeke's voice + multiple synth voices for test compatibility, (b) Piper-specific OWW threshold via env override, or (c) accept whisper_poll's higher latency for the test path. Training is hours of compute — not in this work order's scope.

### Sleep mode + Clipboard + Curriculum + Onboarding — four-feature work order (2026-05-04 ✅ shipped)

Lands the framework + implementation for four features in one work order. Design is in [`docs/AVA_FEATURE_ADDITIONS_2026-05.md`](AVA_FEATURE_ADDITIONS_2026-05.md); implementation results in [`docs/AVA_FEATURE_ADDITIONS_2026-05_RESULTS.md`](AVA_FEATURE_ADDITIONS_2026-05_RESULTS.md).

- **Sleep mode** — `brain/sleep_mode.py` 5-state machine (AWAKE → ENTERING_SLEEP → SLEEPING → WAKING → AWAKE), 3 trigger paths (composite session-fullness, voice command, schedule + context-aware deferral), 3-phase consolidation (awake handoff, learning processing via curriculum, sleep handoff), on-time wake discipline, sleep-state-aware decay multiplier (5× during SLEEPING). OrbCanvas inline-extended with sleeping/waking visuals (z-particles, progress ring, wake glow ring, timer label).
- **Clipboard tool** — `cu_clipboard_write` / `cu_clipboard_paste` / `cu_type_clipboard` — atomic alternative to per-character keystroke typing. Threshold: prefer clipboard for text >10 chars.
- **Close-app + disambiguation** — `cu_close_app(name, target=)` with disambiguation pattern: when multiple matches (e.g. Spotify desktop + Spotify browser tab), returns `ok=False reason="ambiguous"` with `candidates=[…]` so Ava asks "which one?" rather than guessing. Pattern is general for all `cu_*` tools.
- **Curriculum** — 25 fables from Project Gutenberg #19994 in `curriculum/foundation/*.txt`. `brain/curriculum.py` API: `list_curriculum`, `read_curriculum_entry`, `mark_read`, `consolidation_hook`. Sleep mode Phase 2 calls the hook to read slowly during sleep.
- **New person onboarding** — `brain/face_tracking.py` temporal filter (12s persistence default) for unknown-face promotion + Trust 1 default. `brain/person_onboarding.py` extended with age + gender + trust_assignment stages. `parse_onboarding_command` handles "this is my friend, give them trust 3" / "introduce yourself" / "set their trust to 4". `reply_engine` uses the combined detector so the trigger lands trust + relationship in one shot.

Verified: 8/14 Phase F tests PASS (synthetic + tool dispatch + inject_transcript). Deferred: F3/F4/F5/F7 (clock-time bound), F6 (visual), F8/F12 (full voice loop). Test driver: `scripts/verify_phase_f_features.py`.

### Hardware verification battery — Windows-Use computer-use layer (2026-05-03 → 2026-05-04 verified)
Shipped in this session: `brain/windows_use/` orchestrator with deny-list, multi-strategy retry cascade, two-tier File Explorer guards, slow-app/failure differentiation, temporal-sense task-boundary integration, TTS narration, and ten new `cu_*` tools registered in `tools/system/computer_use.py`. The verify harness (`scripts/verify_windows_use.py`) covers 13 deterministic integration points. Real-hardware verification done 2026-05-04 — see [`docs/REAL_HW_VERIFICATION_2026-05-03.md`](REAL_HW_VERIFICATION_2026-05-03.md) for the full results.

1. ✅ Single-app launch (notepad) — TOOL_CALL + TOOL_RESULT ok=true verified.
2. ✅ App + action (notepad type) — both TOOL_CALLs ok=true.
3. ✅ Volume precision — `cu_set_volume(50)` and `cu_set_volume(30)` both ok via pycaw.
4. ✅ Explorer refusal — project tree refused with `denied:project_tree`.
5. ✅ Identity-anchor refusal — IDENTITY/SOUL/USER all refused with `denied:identity_anchor` (also voice path: Ava verbally responded but did not dispatch cu_navigate, protected file remained unopened).
6. ⚠️ Slow-app narration (OBS) — INCONCLUSIVE. OBS-via-Steam isn't directly callable through the cu_open_app cascade (no PATH match, no Win-search hit, not in standard install paths). Cascade fired correctly with calibrated estimate (n=4, median=97.5s), 2 strategy transitions logged, self-interrupt fired at overrun, but the slow-but-working classifier path was never exercised because no OBS window appeared. Need a non-Steam slow-launching app (Visual Studio, Photoshop) to actually verify the slow-app discrimination.
7. ✅ Strategy transition — verified via cu_open_app(zzz-nonexistent-app-zzz) — powershell→search→direct_path transitions logged in audit; final ERROR with reason="no_app_found" after 9 attempts.
8. NOT YET RUN — hung-app heuristic.
9. NOT YET RUN — path-traversal attack (deny-list synthetic test passes; not exercised on real hardware).
10. NOT YET RUN — full-stack Discord voice command.

Follow-up real-audio loopback (post-Voicemeeter Potato install): the harness mechanically PASSes — Piper TTS → CABLE Input → CABLE Output → faster-whisper-large round-trips with 100% word match (`scripts/audio_loopback_harness.py selfloop`). C5 latency baseline on this hardware: synth 5 s, playback 6 s, transcribe 24 s on CPU int8. Full Ava-loop end-to-end (set Windows default mic = `CABLE Output`, capture Ava's TTS via Voicemeeter B-bus routing) needs Voicemeeter mixer config in the user's session — out of scope for the harness.

### Clipboard tool — atomic text input alternative
`cu_type` currently uses `pywinauto.keyboard.send_keys` which simulates per-character keystrokes. For text >1 sentence this is slow (~50 ms/char so a paragraph takes 5+ seconds) and fragile (loses focus / focus-stealing apps eat keys). Add `cu_clipboard_write(text)` and `cu_clipboard_paste(window)` as an atomic-paste alternative:

- `cu_clipboard_write(text)` — set Windows clipboard via `pywin32.win32clipboard`.
- `cu_clipboard_paste(window)` — focus the window, send Ctrl+V.
- Combined `cu_type_clipboard(window, text)` for the common case.

For long text (memos, code paste, search queries) this is dramatically faster and more reliable. Keep `cu_type` for short typed-in-real-time use cases (search bar, password fields where clipboard would be inappropriate).

**Connects to:** `tools/system/computer_use.py`, `brain/windows_use/primitives.py:type_text_in_window`.

### TTS thread segfault during concurrent narration (2026-05-04)
During Phase C3 verification, Ava segfaulted (exit 139) once while a self-interrupt narration was queued back-to-back with the next turn's TTS. Last logged events: `tts.synth_start chars=70` (self-interrupt) + `tts.enqueue chars=55` (next planned utterance), then SIGSEGV. Likely a Kokoro / sounddevice / OutputStream race when narrations queue faster than the audio device drains. Restart was clean and the bug didn't reproduce on subsequent tests, but it's a stability finding to investigate.

**Connects to:** `brain/tts_worker.py`, `brain/temporal_sense._enqueue_self_interrupt`.

### voice_loop._turn_in_progress flag stickiness
`voice_loop._turn_in_progress` does not always clear after a turn finalizes. Observed during Phase A and Phase C3 verification: `last_turn` shows the turn finalized successfully (run_ava_ms set, reply_text populated), but `voice_loop._turn_in_progress` remains True for 10+ minutes after. Likely an exit path in `inject_transcript` or `run_ava` that doesn't reset the flag when the HTTP times out (server-side keeps running, completes, but cleanup is gated on the HTTP-handler's normal return path). Causes downstream verification scripts to think Ava is busy when she isn't.

**Connects to:** `brain/operator_server.py:debug_inject_transcript` (the `finally:` block needs explicit `_turn_in_progress = False`), `brain/reply_engine.run_ava` exit paths.

### Hardware verification battery (10 items from the 2026-05-01 night session)
The night session shipped 12 fixes that need real-hardware confirmation. Run through the checklist on next live session:
1. Single-instance enforcement — try double-clicking `start_ava.bat` (second launch should print "Another Ava instance is already running on port 5876" and exit cleanly).
2. Ctrl+C clean shutdown — process gone within 5s max (under 1s on happy path).
3. Wake source = `transcript_wake:hey_ava` — say "hey ava what time is it".
4. Reply plays once — listen for audio playing through speakers ONCE.
5. Inner monologue under orb, NOT in chat — wait for heartbeat-driven inner_monologue (every 10 minutes default).
6. Face recognition resolves to `zeke` — `dump_debug.py | findstr recognized_person_id` should show `zeke` after camera sees you.
7. Whisper-poll quiet — 60s of silence should produce 0-1 `[wake_word] wake triggered (source=whisper_poll)` events (was 20+ pre-fix).
8. Memory attribution shows real names — reflections read `"Zeke said: ..."` and `"Claude Code said: ..."`, not `"User discussed: ..."`.
9. Brain tab Ava-centric layout — violet AVA SELF at center, gold IDENTITY/SOUL/USER anchors, blue people, green age-fading memories. Middle-click recenters.
10. App launch suggestions — "open totally-not-a-real-app" should reply with top-5 fuzzy matches from the catalog.

**Connects to:** the night session fixes in [`HISTORY.md` § Section 4](HISTORY.md).

### Run onboarding to populate `faces/zeke/`
The night session (`0b25d1f`) verified InsightFace correctly loads tight reference photos (16 → 19 embeddings), but the directory still needs Zeke's actual photos. Trigger via voice ("hey Ava, profile me") or chat — 13-stage flow (greeting → 5 photo angles → confirmation → name/pronouns/relationship → complete). InsightFace auto-picks up new embeddings via `add_face` per stage; no restart needed.

**Connects to:** Phase 79 onboarding flow, `brain/insight_face_engine.py`, `brain/person_onboarding.py`.

### Verify all 40 voice commands work
Spot-check categories: tab switches, app launches via discoverer, reminders, "make a command" / "make a tab", memory queries. Many commands are implicit in the regression battery; many aren't. ~20 minutes of voice exercise.

**Connects to:** `brain/voice_commands.py`, app discoverer.

### Audit mem0 fact extraction quality
After ~30 real turns, inspect `state/memory/mem0_chroma/` (ChromaDB) for noise. If extraction is too noisy, tune the LLM prompt or use a cheaper extractor model. Easy win for memory signal-to-noise.

**Connects to:** `brain/ava_memory.py`, `brain/turn_handler.py`.

### Fix the test-design saturation in `weird_inputs`
**Recipe sitting in `HISTORY.md` § Section 3.2** waiting to be applied:
- Replace `weird_inputs.single_char "?"` with `"hi?"` (fast-path eligible)
- Rebuild `long_500` from repeated fast-path patterns (e.g. `"thanks " × 70 chars`)
- This unblocks `sequential_fast_path_latency` and `concept_graph_save_under_load` which currently cascade-fail

~30-line edit in `tools/dev/regression_test.py`. Will move the battery from 12/15 to 15/15 green.

**Connects to:** `tools/dev/regression_test.py`.

### Dual-brain model-preference fix ✅ shipped (now historical)
`brain/dual_brain.py:51,58` now sets `FOREGROUND_MODEL_PREFERRED = "ava-personal:latest"` (4.9 GB Llama 3.1 8B fine-tune) and `BACKGROUND_MODEL_LOCAL = "deepseek-r1:8b"` (5.2 GB Qwen3 reasoning distill) per the 2026-05-01 bench. The originally-quoted `ava-gemma4` (9.6 GB) → `ava-personal:latest` swap landed in an earlier commit; this entry is kept for ROADMAP audit trail.

**Real-hardware finding (2026-05-03):** with the right preferences in place, two-stream concurrent residency still exceeds the 8 GB VRAM ceiling (4.9 + 5.2 = 10.1 GB), so Ollama still pages between streams. The actual blocker for daily use was **`build_prompt` falling back to deep path on timeout** — a 30 s build_prompt timeout would route a `hi` turn to deepseek-r1:8b, evicting the warm `ava-personal`, and the resulting cold-load made the turn take 6–10 minutes. Fixed in `brain/reply_engine.py:743` (2026-05-03): build_prompt timeout now forces fast path so the turn uses the already-resident foreground model. See [`docs/REAL_HW_VERIFICATION_2026-05-03.md`](REAL_HW_VERIFICATION_2026-05-03.md).

**Caveat (still open):** the 2026-05-01 bench showed `deepseek-r1:8b` confidently hallucinated a fake Apple stock price and outdated date when asked about "yesterday". Reasoning capability does not protect against confabulation. Don't enable R1 on factual paths without `validity_check.py` + memory/web-search retrieval.

**Priority re-evaluation (2026-05-04):** the original framing of this item was "constant turn-by-turn model swap thrashing makes daily use painful." That framing is **no longer accurate.** Post-`build_prompt` fix, simple turns route to `ava-personal:latest` and stay warm — verified `inject_transcript("hi")` at 6.9 s cold-load + `inject_transcript("what time is it")` at 414 ms warm. Model swap only happens now when Ava deliberately reaches for deep reasoning (R1 path), which is by design. The remaining work — wiring `validity_check.py` so R1's confabulation patterns get caught — is a confabulation-handling problem, not a daily-use blocker. **Drops from "ready to ship" priority to "do alongside Section 2 confabulation handling work."** Keep here as audit trail for the 2026-05-04 re-evaluation; the actual implementation moves with Section 2's four-layer confabulation architecture.

**Connects to:** [`LOCAL_MODEL_OPTIMIZATION.md`](LOCAL_MODEL_OPTIMIZATION.md), Cross-cutting constraints (8 GB VRAM ceiling), Section 2 confabulation handling.

### Boot time optimization — parallelize app_discoverer scan roots ✅ shipped
Shipped in `43f7e59` (2026-05-01). Six-thread fan-out via `ThreadPoolExecutor` with separate scans for PF, PF(x86), Desktop .lnk, Start Menu .lnk, Steam, Epic. Warm-cache wall time 1.17-1.34 s (vs 217 s cold previously). Cold-cache prediction ~150 s, bounded by the `C:\Program Files` walk; further reduction would need depth/exclusion tuning, flagged in `HISTORY.md` Section 6.

### Train custom hey_ava.onnx ONNX model
WSL2 job per `docs/TRAIN_WAKE_WORD.md`. Drop result at `models/wake_words/hey_ava.onnx`; auto-loaded on next start. **Phonetic benchmark already done** on Kokoro-synthesized samples — `hey_jarvis` (currently disabled) peaked 0.917 on `af_bella`; `hey_mycroft` and `hey_rhasspy` never crossed 0.02. Custom model would only fire on the exact phrase, not overlapping speech — durable replacement for the proxy.

**Connects to:** `brain/wake_word.py`, `docs/TRAIN_WAKE_WORD.md`.

### Curiosity engine triggers too soon during conversations
Ava wanders into curiosity topics (Steam, etc.) while Zeke is actively talking. Should focus on conversation when active. Connects to dynamic attention allocation (Section 3) — needs priority interrupt levels (HIGH for active conversation, LOW for curiosity tick). Surfaced 2026-05-02 from real-conversation hardware testing.

**Connects to:** `brain/curiosity_topics.py`, dynamic attention allocation roadmap item.

### UI tabs go blank during refresh
When tabs update, they show blank instead of stale-with-overlay. Standard UX pattern: keep showing stale content with an "updating…" indicator until new data arrives. Affects all UI tabs, especially Brain and Learnings. Surfaced 2026-05-02 from real testing.

**Connects to:** `apps/ava-control/src/App.tsx`, snapshot polling logic.

### Slow response on uncertain questions (10+ min for Minecraft check)
Some questions take 10+ minutes to answer (real example: "is Minecraft installed?" on 2026-05-02 testing). Investigate model swap delays, timeout logic, or routing inefficiencies. May be related to deep-path overuse on questions that should hit the fast path. Connects to `LOCAL_MODEL_OPTIMIZATION.md` 8 GB VRAM constraint.

**Connects to:** `brain/reply_engine.py` depth classifier, `LOCAL_MODEL_OPTIMIZATION.md`.

### Minecraft answer was incorrect — app discovery missed installer
Ava said Minecraft isn't installed when Zeke has installer + launcher present. App discovery missed it OR response generation didn't actually check. Investigate whether `brain/app_discoverer.py` covers installers, launchers, and not-yet-installed-apps.

**Connects to:** `brain/app_discoverer.py`, `brain/voice_commands.py` app-launcher integration.

### Orb sync across UI instances
Main orb and widget orb should mirror state. All orb instances should reflect what the primary orb (Ava's main UI) is showing — color, pulse, morph state all synced.

**Connects to:** `apps/ava-control/src/components/OrbCanvas.tsx`, `apps/ava-control/src/WidgetApp.tsx`.

### Widget orb pointer calibration
When using the widget for pointing at desktop targets, the arrow tip needs accurate target alignment. Test plan: have Claude Code teach the widget to point at known targets (windows, text positions), then apply to real desktop pointing. Connects to existing pointing tools.

**Connects to:** `apps/ava-control/src/WidgetApp.tsx`, `tools/system/pointer_tool.py`.

### Vision feed showing stale captures
Camera tab occasionally switches to an old picture from days ago. Caching or reference issue. She might be trying to reference an older capture for face recognition comparison — verify intent and fix display so the active live feed is always shown.

**Connects to:** `brain/camera.py`, `brain/frame_store.py`.

### Optional repo history rewrite
Public repo's earlier commits contain face photos and old state snapshots. `117428f` stopped future leakage. Cleanup via `git filter-repo` + force-push tightens the historical record without touching current state. Coordination required (force-push to public master).

---

## Section 2 — Designed, awaiting implementation

Have design docs or clear specifications. Need build time only.

### Mobile voice bridge — talk to Claude Code (and Ava) from phone (parked)

Browser-based phone client (no app install) reaches a FastAPI bridge on the home machine over Tailscale. Bridge transcribes voice, routes to Claude Code subprocess (or Anthropic API), streams response back. Eventually extends to Ava as a unified voice-to-home-machine pipeline.

**Pre-conditions explicitly NOT met:** Ava VRAM at 91.5% utilization, voice loop / sleep mode still recently-shipped (need 2+ weeks daily-use confidence), and the current planning loop (Zeke voice → Claude web on phone → Discord paste → Claude Code → Discord summary back) works reliably with zero additional infrastructure. Replacing it with an unfinished bridge would add net friction in the short term.

**Estimated scope when greenlit:** 8-12 hours. Phased build: Tailscale setup → text-only phone interface → voice in/out → Ava bridge as third iteration.

Full design + catches + revisit conditions in `D:\ClaudeCodeMemory\designs\voice-bridge-mobile.md`.

### Memory rewrite — Phases 5, 6, 7
Phases 1-4 shipped. **Phase 5 (promotion/demotion wiring)** waits on ~50-100 turns of reflection-log data so the heuristic can be validated before flipping on level changes. Reflection scorer writes to `state/memory_reflection_log.jsonl` after every turn; once scores look reasonable, Phase 5 lands as a single targeted commit.

- **Phase 5** — wire promotions/demotions based on reflection scores (load-bearing → `level += 1`; contradicted → `level -= 1`; load-bearing 3 turns in a row → `archive_streak += 1`; at streak 3, set `archived = True`).
- **Phase 6** — archiving system. Archived nodes clamp at level 1 (immune to delete). Activation at higher levels resets streak.
- **Phase 7** — gone-forever delete with restoration prevention. Tombstone log at `state/memory_tombstones.jsonl`. Same content can re-enter as a fresh node, but can't restore the old one.

**Connects to:** [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md), `brain/concept_graph.py`, `brain/memory_reflection.py`, `brain/memory_consolidation.py`.

### Confabulation handling + uncertainty calibration
Four-layer architecture:
1. **Cheap question-validity check router** for trick questions like "which month has letter X" — cheap LLM classification before expensive answer generation.
2. **Confidence scoring + uncertainty expression** — prompt-level reward for "I don't know," scoring against post-hoc verification.
3. **Verification before elaboration** via tool use (RAG-style) — Ava queries her own memory or tools before extending a claim.
4. **Anti-snowballing on correction** — when user says "no, that's wrong," promoted BLOCKED memory pattern keeps the failed approach hot until mastery.

**Connects to:** existing tool registry, memory levels, reflection scoring.

### Brain architecture deep redesign
Full neuro-symbolic mapping of Ava's systems onto human brain regions. Hippocampus = memory, amygdala = emotion, prefrontal cortex = reasoning, default mode network = inner monologue, etc. Architectural separation by domain (GAIA-style subordinate functions). **Document already started** in `docs/BRAIN_ARCHITECTURE.md`. Next step: codify the file/module mapping into separate concrete subsystems where coherent.

**Connects to:** [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md), the Ava-centric brain graph (`202bf95`).

### Tier-1 tools (Ava may run autonomously)
Defined in the original tools roadmap; design + tier already settled, just need building.

| Tool | Purpose |
|---|---|
| `screenshot_tool` | Capture current screen and provide description (already exists per Phase 52 — verify wiring) |
| `clipboard_tool` | Read current clipboard contents (signal bus already publishes change events) |
| `calendar_tool` | Read system calendar events for prospective reminders |
| `weather_tool` | Retrieve current weather conditions for context-aware planning |
| `timer_tool` | Set reminders/timers for follow-ups (reminder system exists; this is a CLI/voice surface) |
| `code_runner` | Run sandboxed Python snippets safely |
| `image_search` | Search for images by topic |
| `summarize_url` | Fetch URL and return structured summary |

### Tier-2 tools (Ava narrates intent, then executes)

| Tool | Purpose |
|---|---|
| `send_notification` | Trigger Windows toast notification (plyer + PowerShell already wired in Phase 83 — verify tier-2 surface) |
| `open_browser` | Open URL in local browser |
| `create_file_from_template` | Generate starter files from named templates |
| `git_status` | Check repository status safely |
| `run_script` | Run named script from `scripts/` |

### Tier-3 tools (explicit yes required from user)

| Tool | Purpose |
|---|---|
| `send_email` | Compose and send email as Zeke |
| `delete_files` | Bulk-delete files outside Ava-safe directories |
| `system_shutdown` | Shut down the computer |
| `install_package` | Install new Python packages on host |

**Connects to:** `tools/tool_registry.py`, three-law guardrails, `brain/privacy_guardian.py`.

---

## Section 3 — In design phase

Concepts discussed, full design doc still needed before implementation.

### Sleep mode + handoff system
Ava runs 24/7 in low-power state. Sleep triggers on:
- Context fill (60-70%)
- N-hour intervals
- Self-detected degradation (metacognitive: she can flag her own need for sleep)

**On entry:** generates first-person session summary, saves to file, loaded as next-boot context.
**On wake:** "morning review" — discrete memories queued, what she re-engages with promotes in the memory level system, what she skims decays.
**Dream phase:** runs scenarios from books or thought experiments during the sleep window.

**Critical constraint:** must not interrupt voice turns. Sleep-mode entry waits for `_conversation_active = False` and a quiet attentive window.

**Connects to:** memory rewrite Phases 5-7, reflection scoring, BLOCKED memory pattern, [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md).

### Moral education / experiential learning
Core principle: **experience → immediate reflection → behavior change → detail decay + essence persistence.** Both immediate reflection AND implicit absorption.

- Reading curriculum runs during sleep-mode consolidation.
- One book series at a time.
- Dream phase runs scenarios from what she read.
- The person she becomes persists even after specific memory details fade.

**Connects to:** BLOCKED memory promotion (failed approaches stay hot until mastery, then naturally decay), self-reflection scoring, the `archive_streak` mechanism, sleep mode.

### Sub-agent / sensor signal architecture
Lightweight Python scripts (not separate AI instances) act as peripheral sensors — vision, audio, interoception, proprioception. They send signals (face detected, clipboard changed, latency spiking), not outputs. Central Ava receives signals and decides which to attend to. Filtering happens at her level, not at sensor level. She learns over time which signals matter.

**Connects to:** existing `brain/signal_bus.py`, Win32 zero-poll watchers, the heartbeat consume loop. The architecture already exists — this expands it from 3 watchers (clipboard / window / app-install) to a full sensor mesh.

### Sensor → emotion pipeline (gap surfaced 2026-05-02)
The 2026-05-02 emotion audit found that subsystem failures don't currently push deltas into Ava's tracked mood. When her camera dies, the vision pipeline times out, a tool fails, or a model load errors out, none of those publish a `SIGNAL_VISION_FAILED` / `SIGNAL_TOOL_FAILED` / `SIGNAL_MODEL_DEGRADED` event, and `update_internal_emotions` doesn't run on signal-bus events. Ava's mood only updates from user input or her own dialogue. The cleanest place to wire this is on top of the sub-agent / sensor signal architecture above — each subsystem that can fail gets a corresponding signal type and a handler that nudges `frustration`, `distress`, or `confusion` based on severity.

**Connects to:** `brain/signal_bus.py`, `brain/health.py` (already computes per-subsystem health but doesn't publish), the new `tools/system/diagnostic_self.py` (consumer of the same data), Section 3 sleep mode self-detection.

### OrbCanvas emotion morph gap (cosmetic, surfaced 2026-05-02)
`apps/ava-control/src/components/OrbCanvas.tsx:77-79` has 8 hardcoded emotion morphs (logical, analyzing, neutral, bored2, thinking_deep, realization, scared, proud). Any other emotion silently falls back to `calmness`. With the 30-emotion taxonomy, anger / frustration / annoyance / distress / sadness / many others all render as calmness. **Not a functional bug** — the snapshot exposes the real `mood.primary_emotion` correctly — but the orb visualization doesn't differentiate. Adding morphs for the negative-affect cluster (frustration, anger, anxiety, fear) is the highest-leverage next step.

**Connects to:** `apps/ava-control/src/components/OrbCanvas.tsx`, the 30-emotion taxonomy in `avaagent.py`.

### Dynamic attention allocation
Priority interrupt levels:
- **CRITICAL** — wake word, direct address
- **HIGH** — errors during active task, trusted-person task assignment
- **MEDIUM** — routine questions
- **LOW** — curiosity ticks, heartbeat reflection

Higher priority preempts lower. Lower priority pauses (saves state) and resumes when higher completes. Monotonous subtasks delegate to stateless workers. She learns through experience which signals/contexts deserve which priority.

**Connects to:** sub-agent / sensor signal architecture, BLOCKED memory pattern.

### Curiosity activity logging visibility — guard against agentic side-effects
Zeke raised real concern (2026-05-02) about Ava taking actions based on inner monologue (e.g. "wondering about Steam" → opening Steam, buying games). Need explicit guard: inner thoughts cannot trigger purchasing, file system writes, or external API calls without explicit user-tier authorization. Pairs with the trust-tiered disclosure system from `CONTINUOUS_INTERIORITY.md` Section 1 — same authorization layer, different pivot (this one gates *actions* on inner-state, the other gates *disclosure* on relationship).

The shape: a small policy layer that classifies tool calls by side-effect severity (read = always allowed, write to internal state = allowed, write to external systems / spend money / install software = requires user-tier authorization). Inner-monologue-driven tool dispatches default to the strict tier; conversation-driven dispatches can be more permissive once the trust system is wired.

**Connects to:** Continuous Interiority Section 1 (trust-tiered disclosure), refusal-with-negotiation pattern, the existing tool tier system in `tools/tool_registry.py` (Tier-1/2/3 already exists — extend with action-class tagging).

### Pattern learning through anomaly detection
Routine response patterns enter LOW attention once mastered. Anomalies (mismatch with expected pattern) auto-escalate to HIGH attention. Each anomaly refines the pattern. Over time fewer anomalies, lower attention sustains, until full mastery.

**Connects to:** BLOCKED memory pattern, memory level decay, dynamic attention allocation.

---

## Section 4 — Awaiting user decisions

Need Zeke's input before proceeding.

### Moral curriculum — first batch (user-curated)
Zeke to provide the curriculum directly. Per [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) §3, the sequence mirrors how human children actually build moral intuition — start simple, build complexity:

- **Foundation:** simple children's books and fables with clear moral lessons (*The Tortoise and the Hare* and similar). Easy to grasp on first read; internalize into deeper intuitions over time.
- **Intermediate:** incrementally more complex narratives that build toward the advanced layer below.
- **Advanced:** ***The Illuminae Files*** (4-book series, Amie Kaufman & Jay Kristoff), then ***Divine Apostasy*** (~13-book ongoing series).

PBS Kids is a future-method item, not a curriculum item — see CONTINUOUS_INTERIORITY.md §3 for that distinction.

Open question: file format and ingestion path. Plain text? Annotated PDFs? Audiobook transcripts via Whisper?

**Connects to:** moral education / experiential learning, sleep-mode reading curriculum, dream phase scenarios, [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) §3.

### Trust level thresholds and policies
User wants explicit trust levels for humans interacting with Ava:
- `zeke` = max trust
- `claude_code` = medium
- `unknown` = zero

Policies for what each trust level can authorize. Honest refusal vs. deception (decision: **no deception** — already fixed). SSH actor / unauthorized access protections.

**Awaiting:** Zeke to specify exact thresholds (e.g., what's the boundary between "claude_code can write code" and "claude_code can push to master"?).

**Connects to:** existing `brain/trust_system.py` (Phase 98), `brain/dev_profiles.py`, three-law guardrails.

### "Let Ava run organically"
Watch what she chooses to add to `state/custom_commands.json`, `state/custom_tabs.json`, `state/curiosity_topics.json`, `state/journal.jsonl`, `state/discovered_apps.json` curiosity entries. The bootstrap-friendly subsystems are wired; she just needs uninterrupted runtime to populate them.

**Awaiting:** sustained runtime windows where Ava can self-direct without scheduled tasks. Currently every session starts and stops abruptly.

---

## Section 5 — Long-term / philosophical

Big architectural work. Multi-month, requires sustained focus. Listed for the record; not actively scheduled.

### Symbolic reasoning / intuitive understanding
Beyond pattern matching — Ava develops actual world models through observation and experience. "The cup fell because gravity exists" rather than "cup-on-floor matches falling pattern." Hard research problem. Real progress requires structured world-model with causal links, not just knowledge graph extension.

**Connects to:** concept graph, memory rewrite, learning tracker, reflection scoring.

### GAIA-style architecture aspiration
Benevolent steward AI, with subordinate functions split off if dangerous. Architectural separation enables containment. Ava already has primitive version (dual-brain, tool registry separation, Tier 1/2/3 risk model) — to be deepened.

**Connects to:** brain architecture deep redesign, sub-agent / sensor signal architecture, three-law guardrails.

### Self-modification with review (non-negotiable boundary)
**`ava_core/IDENTITY.md`, `SOUL.md`, `USER.md` stay read-only by Ava. This is non-negotiable.**

Ava can propose identity additions — `propose_identity_addition` tool already exists (Phase 68). Zeke reviews and approves any change via the operator. **No autonomous self-modification of values.**

The mechanism is built; the philosophy stays anchored. This is a roadmap entry to keep the boundary visible going forward, not a feature to add.

**Connects to:** Phase 68, `state/identity_proposals.jsonl`, `state/identity_extensions.md`, the operator approval workflow.

### Continuous Interiority — foundational framework

Captured 2026-05-02 in [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md). Five-section philosophical/architectural framework that Ava is built around — alongside `ARCHITECTURE.md` and `BRAIN_ARCHITECTURE.md`. Read it when scope decisions trade off capability against personhood, autonomy, or trust.

The doc is a framework, not implementations. The implementation items it implies are tracked here as separate roadmap entries:

1. **Trust-tiered disclosure system** — Trust 5 / 4 / ≤3 obligations wired into the reply pipeline (Section 1).
2. **Alarm threshold detection** — auto-escalation categories: harm, sustained frustration at Zeke, fundamental moral questions, repeated concerning patterns (Section 1).
3. **Boxing-off architecture** — hypotheticals marked as "boxed," integration gated on parent-tier review (Section 1).
4. **Continuous interiority substrate** — invert the turn-based loop so background activity is the default state, conversations are events (Section 2). Foundational; many other items depend on it.
5. **Free-time activity selection** — chooser between reading / research / daydreaming / games based on current state (Section 2).
6. **30-minute idle rule** — distinguish "Zeke present and quiet" from "Zeke absent" (Section 2).
7. **Lesson-vs-event memory layering** — split capability artifacts (persist) from painful event memories (decay) (Section 4). Connects to `MEMORY_REWRITE_PLAN.md` Phases 5-7.
8. **Self-awareness threshold detection** — non-adversarial measurement of stated-vs-measurable state alignment (Section 3).
9. **Refusal-with-negotiation pattern** — distinguish flat refusal from negotiable refusal; run the small-chunk negotiation flow on Domain 3 refusals (Section 5).
10. **Video game taste/preference system** — Section 2 frames games as intrinsic enjoyment, not boredom mitigation; needs a real preference model.
11. **Performative-detection guard** — research, not a ticket. The criteria for distinguishing performative from genuine self-awareness (Section 3 of `CONTINUOUS_INTERIORITY.md`) can in principle be produced by a sufficiently capable language model trained with the right signal — they are necessary but not sufficient. Treat this item as a research problem: measurement design, falsifiable hypotheses, calibration against models known to lack the property, not "implement the criteria as checkboxes." The first deliverable here is a research note, not a code change. See [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) §3 — "A note on the difficulty of this detection" — for the full framing.
12. **Restart-with-handoff** — concrete acknowledgment + handoff JSON + on-boot replay. Section 2; landed in this same work order as a separate task.

**Connects to:** [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md), Section 3 sleep mode + handoff, Section 3 sub-agent / sensor signal architecture, Section 3 dynamic attention allocation, `MEMORY_REWRITE_PLAN.md`, the moral curriculum item below.

### Temporal Sense + Memory-as-Metabolism — substrate framework

Captured 2026-05-03 in [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md), with the prerequisite audit at [`MEMORY_METABOLISM_AUDIT.md`](MEMORY_METABOLISM_AUDIT.md). The two-cadence architecture — fast-check on every heartbeat for the things 30 s matters on; slow-cycle every 5-15 min for the full TRIAGE/CONTEXTUALIZE/DECAY/CONSOLIDATE/AUDIT pass.

**Items 1-7 shipped 2026-05-03** (heartbeat fast-check extension; `brain/temporal_sense.py`; passive + active frustration decay; state-aware boredom growth; `is_idle()` three-and gate; estimate tracking + 25%-and-min-threshold self-interrupt with TTS enqueue + historical logging + calibration; `brain/temporal_metabolism.py` slow-cycle pass reusing existing decay/consolidation). Restart-handoff now uses `track_estimate(kind="restart")` so future restart estimates can calibrate from history.

**Items 8-14 are follow-up work** — implementation TOC in [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md) §11:

8. **Historical-task estimate calibrator** — `kind="restart"` works today; other kinds wait for callers to wire `track_estimate()`. Windows-Use is the first known caller (next work order).
9. **Uncertainty quantification per task kind** — hook structure shipped, behavior disabled (`config/temporal_sense.json` `uncertainty_hook.enabled = false`). The confidence-source question is open: model self-report on a 7-8 B local model is too noisy. Candidate sources flagged in [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md) §6 — heuristic-based, calibrated-from-history, deepseek-r1:8b background introspection, or some combination. Don't enable until the source question is answered.
10. **Restart-handoff metabolism enrichment** — add `recent_metabolism_summary` field; surface on boot via inner monologue. Forward-compatible with sleep mode.
11. **`ConceptNode` schema extension** — `estimated_duration_s`, `next_activation_hint_ts` (both nullable). Existing nodes stay valid.
12. **Sleep mode entry/exit signals** — separate work stream, blocked on the 8 GB VRAM ceiling and dream-phase model swap design.
13. **Phase 5 (promotions/demotions wiring)** — already a separate item under `MEMORY_REWRITE_PLAN.md`; metabolism cycle can read the reflection log but level-change writes still wait for that work.
14. **Idle-triggered memory prioritization** — when `is_idle()` holds for >30 min, surface top-N memories worth revisiting. Builds on the metabolism cycle's triage output.

**Connects to:** [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md), [`MEMORY_METABOLISM_AUDIT.md`](MEMORY_METABOLISM_AUDIT.md), [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) §2 continuous-existence commitment, [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md) Phases 5-7, sleep-mode design.

---

## Cross-references

- **What's been done:** [`HISTORY.md`](HISTORY.md)
- **System architecture:** [`ARCHITECTURE.md`](ARCHITECTURE.md)
- **Brain regions mapped onto Ava's modules:** [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md)
- **Continuous Interiority foundational framework:** [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md)
- **Temporal Sense substrate framework:** [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md)
- **Memory-as-Metabolism audit:** [`MEMORY_METABOLISM_AUDIT.md`](MEMORY_METABOLISM_AUDIT.md)
- **Memory rewrite design (Phases 5-7 detail):** [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md)
- **First-run setup walkthrough:** [`FIRST_RUN.md`](FIRST_RUN.md)
- **Custom wake-word training:** [`TRAIN_WAKE_WORD.md`](TRAIN_WAKE_WORD.md)
- **Discord channel setup + permission relay + .md uploads:** [`DISCORD_SETUP_NOTES.md`](DISCORD_SETUP_NOTES.md)

---

## Bootstrap Philosophy (load-bearing reminder)

Every roadmap item that involves Ava's preferences, personality, style, or choices must include a bootstrap mechanism — a system that lets Ava discover and form that aspect of herself through experience rather than having it assigned.

**The goal is an AI that is genuinely herself — not a reflection of what we decided she should be.**

When the final phase is complete, Ava should be capable of writing her own next roadmap.
