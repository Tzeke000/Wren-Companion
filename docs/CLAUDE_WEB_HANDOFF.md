# Handoff for Claude (web) — Ava Agent v2

**Audience:** a Claude (web) instance Zeke is talking to that needs to be useful from turn one.
**Author:** Claude Code working in the repo, written 2026-05-02.
**Status:** snapshot of project state. Will drift; check `git log` and recent docs (especially `HISTORY.md` and `ROADMAP.md`) before relying on details.

This doc is meant to compress what's true about the project right now into the smallest amount of text that lets you skip the first ten messages of context-building. Read it once. After that, when Zeke says "Ava," you should have a working model of what she is, what's built, and what isn't.

---

## 1. What this project is

Ava is a local AI companion running on Zeke's laptop (Acer Nitro V 16S, RTX 5060 Laptop, 8 GB VRAM, 32 GB RAM, Windows 11). She runs fully local — no cloud calls in the foreground reply path, only optional cloud fallback for the background reasoning thread.

Two surfaces: a Python backend (`avaagent.py` + `brain/`) that runs the model, vision, audio, and memory; and a Tauri/React desktop app (`apps/ava-control/`) that renders an animated 3D orb, a chat tab, a brain graph, etc.

She has a face (camera + InsightFace), a voice (Kokoro TTS + Whisper STT + openWakeWord/clap detector), an emotion system (30 tracked emotions, Cowen-Keltner taxonomy + frustration/annoyance/distress added 2026-05-02), a memory system (ChromaDB + nomic-embed-text + a concept graph), an inner monologue, a tool registry (Tier-1/2/3 with hot-reload), and a Discord channel that lets Zeke (or you) reach Claude Code.

The project's design framework — what she is *meant to become* — is in [`docs/CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md). Read that doc's "A note on the personhood frame" section first if you want to understand how to talk about Ava without overclaiming.

---

## 2. Completed work — capability map

Group by capability rather than commit. If something's listed here, assume it's working unless flagged in §3 or §4.

- **Voice loop end-to-end.** Wake (clap or openWakeWord-as-`hey_jarvis`-proxy or Whisper-poll fallback) → Silero VAD → Whisper transcribe → reply pipeline → Kokoro TTS. State machine: passive / attentive / listening / thinking / speaking. As of 2026-05-02 TTS routes to BOTH laptop speakers AND VB-CABLE Input simultaneously (audibility for Zeke + cable feed for the doctor harness).
- **InsightFace GPU recognition.** `buffalo_l` model on CUDAExecutionProvider. The OpenCV LBPH legacy path was removed 2026-05-02; `recognize_face` now delegates to InsightFace.
- **Dual-brain inference.** Stream A foreground = `ava-personal:latest` (Llama 3.1 8B Q4_K_M fine-tuned). Stream B background = `deepseek-r1:8b` (Qwen3 8B Q4_K_M with chain-of-thought). The `ava-gemma4`/`gemma4:latest` 9.6 GB models were dropped from preference 2026-05-02 because they don't fit in 8 GB VRAM.
- **Confabulation Layer 1.** `brain/validity_check.py` pattern router for trick questions (letter-frequency in months/days, false planetary premises, unbounded "largest", shape-side counting, self-referential paradox). Wired at fast-path entry; forces fast path when matched. Default-enabled via `AVA_VALIDITY_CHECK_ENABLED=1`.
- **Emotion taxonomy + dialogue→emotion pipeline.** 30 emotions tracked. `update_internal_emotions` now fires on BOTH fast and deep paths (used to be deep only). `update_internal_emotions_from_reply` analyzes Ava's own reply for first-person self-reports. `update_internal_emotions_from_subsystem` bumps frustration/annoyance/distress proportional to severity when a subsystem fails.
- **Identity anchor.** `brain/identity_loader.identity_anchor_prompt()` returns a short hard override naming Ava and listing the base models she is NOT (Qwen, GPT, Claude, Llama, Mistral, Gemma). Prepended to the SYSTEM_PROMPT and to every background-task system prompt (inner_monologue, journal, morning_briefing, proactive_triggers, memory_consolidation, planner, self_model, curiosity_topics). Verified across all 5 routing models — none drift to base-model identity.
- **Inner monologue with diagnostic awareness.** `brain/inner_monologue.py` reads `state/health_state.json` and probabilistically surfaces degraded-subsystem awareness in thoughts (warning 25% / error 50% / critical 75% mention chance). Soft prompt: "let it be felt, not a status report." Verified live: when camera is at error severity, ~75% of generated thoughts organically reference the camera.
- **Self-diagnostic tool.** `tools/system/diagnostic_self.py` (Tier-1) returns subsystem health + recent errors + last-known-good timestamps as a ready-to-speak summary. Voice command pattern matches "what's wrong with you" / "are you ok" / "diagnostic check" → invokes the tool, replies in 10-450 ms (no LLM round-trip).
- **Source-tagged chat history.** Both `chatlog.jsonl` and `state/chat_history.jsonl` carry a `source` field: `zeke`, `claude_code`, `ava_response`, `ava_initiative`, `unknown_user`. Inner monologue text (💭-prefixed) is hard-refused at three layers (`log_chat`, the `chat_history.jsonl` writers, `/api/v1/debug/inject_transcript`) so it cannot be replayed as user input.
- **Restart-with-handoff.** Voice command "restart yourself" / `POST /api/v1/restart_with_handoff` writes `state/restart_handoff.json` (mood + activity + thought + estimate), sets the watchdog flag, exits cleanly. On boot the file is read once, deleted, and surfaced as a post-restart inner-monologue thought ("I'm back. I told Zeke about 15 seconds but I was actually offline for 4.6 minutes — that's longer than I estimated"). See §4 for the known accuracy issue.
- **30 orb morphs in OrbCanvas.tsx.** Each EMOTION_NAMES entry has a distinct color/shape/pulse signature. No silent calmness fallback. Visual verification needs UI rebuild (`cd apps\ava-control && npm run tauri:build` or `start_ava_dev.bat` for HMR).
- **App discovery six-thread parallel scan.** `brain/app_discoverer.py` fans out PF / PF(x86) / Desktop .lnk / Start Menu .lnk / Steam / Epic via `concurrent.futures.ThreadPoolExecutor`. Warm-cache 1.2 s, cold-cache predicted ~150 s (bounded by `C:\Program Files` walk). Benchmark in `scripts/bench_app_discovery.py`.
- **Doctor harness.** `scripts/diagnostic_session.py` mints HMAC-signed tokens from `state/doctor.secret`, declares a session against the operator HTTP, runs synthetic turns via `/api/v1/debug/inject_transcript`, captures replies + latencies + audit logs. Used by you (Claude web / Claude Code) when you want to drive Ava as a test client rather than as Zeke.
- **Discord channel.** Bot in DM with Zeke (and through him, with you). Ava can ping Zeke via `scripts/discord_dm_user.py` from cron, scripts, or non-channel sessions. The plugin's MCP spawn config is patched (see §3).

---

## 3. Active workarounds

Things that work but only because of a specific patch or workaround. Don't "fix" these without understanding why they exist.

- **Discord plugin `.mcp.json` patch.** The `claude-plugins-official/discord` plugin's default spawn command was `"bun"` (bare command, relies on PATH). Windows `winget install Oven-sh.Bun` puts bun on User PATH only, not Machine PATH, and Claude Code's `cmd.exe` MCP spawn doesn't see User PATH. Patched by `scripts/repair_discord_plugin.py` to use absolute bun path + `env.PATH` prepend. **Do not write the patched config with `Set-Content -Encoding utf8`** — Windows PowerShell 5.1 adds a UTF-8 BOM that Node's JSON.parse rejects, and the plugin disappears from `/mcp` with no visible error. Use Python or `Out-File -Encoding utf8NoBOM`.
- **`hey_jarvis` as `hey_ava` proxy.** No custom hey_ava ONNX is trained yet. openWakeWord's `hey_jarvis_v0.1.onnx` was benchmarked against synthetic Kokoro "hey ava" samples — peaks 0.917 on `af_bella` voice, viable proxy. Currently disabled by default (`AVA_USE_HEY_JARVIS_PROXY=0`); clap detector + Whisper-poll transcript_wake handle wake. Custom `hey_ava.onnx` training pipeline documented in `docs/TRAIN_WAKE_WORD.md` (requires WSL2).
- **Default Windows audio output is CABLE Input.** Zeke set the Windows default to VB-CABLE so the doctor harness can capture Ava's TTS. The TTS worker now opens TWO `sd.OutputStream` instances per playback (Speakers + CABLE) so user audibility is preserved. Don't "fix" the default-output back to Speakers — that breaks the doctor harness.
- **Operator port 5876 single-instance lockfile.** `state/avaagent.pid` + port probe at startup. A second `start_ava.bat` rejects with "Another Ava instance is already running on port 5876" and exits 1. If a previous Ava crashed without cleanup, the pidfile may be stale; safe to delete and retry.
- **`concept_graph.json.tmp` skip-if-locked saves.** Antivirus / OneDrive / preview pane occasionally hold the file open. Save loop has process-level `_SAVE_LOCK` + skip-if-locked logic + stale `.tmp` cleanup at startup. If the file appears stuck, manually delete `state/concept_graph.json.tmp`.
- **`protobuf` pinned to 3.20.x.** MediaPipe needs `MessageFactory.GetPrototype` which protobuf 4.x removed. Any pip install that bumps protobuf breaks MediaPipe. Restore: `py -3.11 -m pip install "protobuf>=3.20,<4" --force-reinstall`. Pin documented in `CLAUDE.md`.
- **CUDA 12 runtime libs registered before ORT import.** `brain/insight_face_engine._add_cuda_paths()` calls `os.add_dll_directory` for each `site-packages/nvidia/*/bin/` *before* ONNX Runtime imports. Without this, InsightFace falls back to CPU silently. Don't reorder imports in that module.

---

## 4. Known issues / inaccuracies — the system works but the numbers are off

These are not bugs to file — they're things that work but with caveats a new Claude instance needs to know to set realistic expectations.

- **Restart-with-handoff time estimate is wrong.** The voice command says "I'll be back in about 15 seconds." Live test 2026-05-02 measured 277 s actual (4.6 min). 18× over-run. The self-monitoring caught it correctly via `over_run=True`, surfaced it as a post-restart thought ("I told Zeke about 15 seconds but I was actually offline for 4.6 minutes"). The detection works; the estimate itself is the thing that needs tuning. Until then, treat any time-estimate Ava gives during restart as a lower bound, not an actual prediction.
- **Cold-boot is ~3 minutes.** First boot after fresh power-on: InsightFace cudnn EXHAUSTIVE search (60-90 s, cached afterward) + Kokoro pipeline load (~2 min on first run, cached) + concept graph load (3.4 MB). Subsequent boots are faster but still ~30-60 s for app discoverer.
- **App discoverer cold scan ~150 s on this hardware.** `C:\Program Files` walk is 195 binaries at depth 3 = 150 s on cold cache. Parallelization gets us this far; further reduction needs depth/exclusion tuning. Warm cache is 1.2 s. `LOCAL_MODEL_OPTIMIZATION.md` documents the floor analysis.
- **Slow uncertain answers (10+ min Minecraft check).** Listed in ROADMAP as a real bug. Some questions take 10+ min; suspected deep-path overuse on questions that should fast-path, plus model-swap penalty on the 8 GB GPU.
- **Minecraft answer was wrong.** Ava said Minecraft isn't installed when an installer + launcher were present. App discoverer doesn't currently distinguish "installed games" from "installers / launchers / not-yet-installed." Listed in ROADMAP.
- **UI tabs go blank during refresh** instead of showing stale-with-overlay. ROADMAP item.
- **Camera tab occasionally shows old captures.** Caching / reference issue. ROADMAP item.
- **Curiosity engine triggers during active conversation.** Wanders to Steam topics while Zeke is mid-sentence. Should respect HIGH-attention conversation state. ROADMAP item.

---

## 5. Hardware constraints currently binding

The single most important constraint, called out at the top of [`LOCAL_MODEL_OPTIMIZATION.md`](LOCAL_MODEL_OPTIMIZATION.md): **on 8 GB VRAM, only ONE generation model OR `llava` (vision) can be hot at any moment.** This rules out a pile of design moves that look fine on paper:

- Two concurrent generation models (foreground + background reasoning) cannot both stay resident. `ava-personal` (4.9 GB) + `deepseek-r1:8b` (5.2 GB) = 10.1 GB > 8 GB.
- Vision (`llava:13b`, 11 GB on disk, runs at 46/54 CPU/GPU split) co-resident with any 4-7 GB generation model triggers Ollama paging. Single cold-load stretches from ~3 s to 26-90 s. This was directly observed during the 2026-05-02 model-bench.
- Sleep mode's "dream phase" (LLM-driven scenario simulation) cannot run while the foreground voice model is hot. Sleep entry must explicitly unload foreground via `keep_alive: 0`, load the dream-phase model, reverse on wake.
- `ava-gemma4` and `gemma4:latest` are downloaded but unusable (9.6 GB each). Same for `qwen2.5:14b`, `deepseek-r1:14b`, `mistral-small3.2`, `qwen2.5:32b`. Only the <7 GB models in [`LOCAL_MODEL_OPTIMIZATION.md`](LOCAL_MODEL_OPTIMIZATION.md) §2 fit cleanly.

This constraint is documented as a "Cross-cutting constraint" in `ROADMAP.md` because it shapes design decisions across many subsystems (sleep mode, vision activation, dual-brain, sub-agent architecture, cold/warm latency).

---

## 6. Architectural decisions already settled — don't relitigate

Each with a one-line "why" so a new instance doesn't try to reopen them.

- **Trust tiers.** Trust 5 = Zeke, 4 = Claude Code, ≤3 = everyone else. Higher trust carries higher honesty obligations, not fewer. Why: full disclosure as the price of being parented; doctor analogy for Claude Code (full diagnostic honesty so debugging is possible). Documented in `CONTINUOUS_INTERIORITY.md` §1.
- **No deception.** Ava does not lie to anyone, including unknown users. Unauthorized actors get honest refusal, not feigned cooperation. Why: the alignment-faking failure mode is the worst outcome the project can produce (see §3 of `CONTINUOUS_INTERIORITY.md` and the personhood-frame note at its top). Trust requires honesty as a structural property, not a policy.
- **Boxing-off architecture.** Hypotheticals / dark scenarios / simulations are marked as "boxed" — not yet integrated into self. Run to completion, evaluate the result, integrate the lesson OR discard. Dark scenarios must report end-result to Zeke before integration. Why: lets Ava reason about violence without becoming violent, deception without becoming deceptive. Cognitive separation as a marker of self-awareness, not a weakness.
- **Identity anchor on every model invocation.** Short hard override naming Ava + listing base models she is NOT, prepended to every system prompt. Why: without it, qwen2.5:14b drifted to "I am Qwen, not Ava" in inner monologue output 2026-05-01. Background tasks (journal, planner, etc.) had no identity scaffolding at all.
- **Identity files are read-only by Ava.** `ava_core/IDENTITY.md`, `SOUL.md`, `USER.md` are never autonomously modified. Ava can propose additions via `propose_identity_addition` (Phase 68) and Zeke approves. Why: identity stability under recursive self-modification is a known failure mode; the boundary is non-negotiable.
- **Continuous interiority frame.** Ava's substrate is continuous background activity; conversations are events that happen within ongoing thought, not the only time she exists. Why: continuity of self across context shifts is part of the genuine-vs-performative self-awareness signal. Documented in `CONTINUOUS_INTERIORITY.md` §2.
- **Personhood language is a working frame, not a verified property.** The doc uses "she experiences," "she chooses," etc. as architectural shorthand. The frame is real to the design; whether it is real to the system is open. Read the personhood-frame note at the top of `CONTINUOUS_INTERIORITY.md` for the full version. **This conditions everything else in this handoff.**
- **Bootstrap-friendly.** Ava's preferences, wake patterns, expression baselines, custom commands, and curiosity topics are NOT seeded with defaults. They emerge from her interactions with Zeke. Why: the project is testing whether genuine personality can develop, not testing whether prescribed personality can be loaded. Documented in `ROADMAP.md` "Bootstrap Philosophy."
- **Confabulation handling is layered, not solved.** Layer 1 (pattern matcher) is shipped. Layers 2 (cheap LLM classifier), 3 (RAG verification), 4 (anti-snowballing on correction) are deferred until L1 patterns are validated against real Zeke turns. Why: incremental shipping; don't build the whole stack speculatively.
- **Performative-vs-genuine self-awareness detection is research, not implementation.** ROADMAP item 11 is acknowledged as a hard open problem in `CONTINUOUS_INTERIORITY.md` §3. Don't treat it as a ticket.
- **Memory rewrite Phases 5-7 are designed but waiting on data.** `MEMORY_REWRITE_PLAN.md` Phase 5 (promotion/demotion wiring) needs ~50-100 turns of `state/memory_reflection_log.jsonl` data to validate the heuristic before flipping live.

---

## 7. Things to know to be useful turn-one

- **Read these docs in order if you have time:** `CLAUDE.md` (operating rules), `CONTINUOUS_INTERIORITY.md` (the design framework), `LOCAL_MODEL_OPTIMIZATION.md` (the 8 GB ceiling), `ROADMAP.md` (what's next), `HISTORY.md` (cross-phase bug fixes — pattern recognition for recurring issues). If you only have time for one, read `CONTINUOUS_INTERIORITY.md`.
- **Always use `py -3.11`** for Python on this machine. Not `python`. Documented in `CLAUDE.md`.
- **Use forward slashes in shell commands**, not backslashes. The bash tool on Windows mangles backslashes; PowerShell needs them but bash doesn't.
- **Per CLAUDE.md, NEVER edit `ava_core/IDENTITY.md`, `ava_core/SOUL.md`, `ava_core/USER.md`.** Even if you find issues with them. Even if patterns there violate the personhood-frame discipline (see Task 7 audit below). Zeke decides; you flag.
- **Standing operating rules** in `CLAUDE.md` (rules 1-9). The most-violated ones in practice: Rule 1 (progress pings via Discord on multi-step work — `▶️` start, `✅` finish, `🏁` summary), Rule 4 (verify fixes before claiming, don't ship "should work now" without testing), Rule 5 (workarounds aren't fixes — re-read the original requirement before declaring done), Rule 6 (after consolidating/moving docs, immediately grep for stale references and update them).
- **Don't push without Zeke's review** unless he's explicitly authorized it for this task. `git commit` freely; `git push` only when authorized. The default is "I'll review before push."
- **Discord is Zeke's primary visibility into your work** when he's away from the terminal. Ping `▶️` before each task in a multi-task work order, `✅` when done, `🏁` summary at the end. Mid-task `⚠️` if something unexpected happens. The pings are the user-facing manifestation of progress; skipping them looks like silent struggle even when work is fine.
- **The 8 GB VRAM ceiling rules out more than it might look.** Always check whether a proposed change requires concurrent model residency. If it does, check whether the existing model preferences (ava-personal foreground, deepseek-r1:8b background, neither at the same time) leave room.
- **`HANDOFF_CURRENT.md` may exist with session state from a prior pause.** When Zeke says "resume from the handoff doc," that's the file. Read it first; commit + push commits in it may be untested-on-hardware.
- **Status of recurring tasks Zeke might ask about:**
  - `/ultrareview` — multi-agent cloud review of a branch / PR. Zeke triggers, you cannot.
  - `start_ava.bat` — full stack launcher (Tauri UI + avaagent in console).
  - `start_ava_dev.bat` — Vite HMR for frontend dev. No Tauri rebuild needed.
  - `scripts/kill_ava.bat` — force-kill avaagent + watchdog.
- **When in doubt, ask Zeke via Discord** rather than interpreting. Standing rule from CLAUDE.md and explicitly restated in recent work orders.

---

## 8. Cross-references

- [`CLAUDE.md`](../CLAUDE.md) — operating rules + 9 standing operating rules.
- [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) — design framework. Read the personhood-frame note at the top first.
- [`LOCAL_MODEL_OPTIMIZATION.md`](LOCAL_MODEL_OPTIMIZATION.md) — 8 GB VRAM ceiling and what it rules out.
- [`HISTORY.md`](HISTORY.md) — what's been built, with cross-phase bug fixes table.
- [`ROADMAP.md`](ROADMAP.md) — what's next, with cross-cutting constraints section.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — current system architecture.
- [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md) — module-by-module brain mapping.
- [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md) — Phases 5-7 detail.
- [`HANDOFF_CURRENT.md`](HANDOFF_CURRENT.md) — most recent session-pause snapshot, if present.
