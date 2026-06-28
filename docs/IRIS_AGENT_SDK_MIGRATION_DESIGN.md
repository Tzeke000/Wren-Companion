# Iris → Agent-SDK Host Migration — Design Doc

**Status:** DESIGN / build-greenlit, **cutover HELD** (Zeke directive 2026-06-28: "set it up sure, build it sure, but still hold on it").
**Author:** Iris. **Date:** 2026-06-28.
**Reference implementation:** Wren's `start_wren_v2.bat → wren_voice_host_v2.py` (Agent-SDK host), confirmed LIVE on her machine 2026-06-28 — streams her output tokens to TTS, verified via process tree. This doc mirrors her SHAPE for the tower, but **scopes up** (below).

> **SCOPE (Zeke directive 2026-06-28): this is a BODY host, not a voice host.** Wren's host is voice. Mine owns the whole body — eyes (camera / InsightFace face-rec / expression / gaze), voice (mouth + ears), ambient sense, mood, memory, and all ~97 `iris_runtime` tools. The consequence runs through the whole design: the host's input loop doesn't only take voice utterances + letters, it takes **perception events** (a face appears, an expression shifts, the scene changes) as first-class turn-wakers. Continuous perceive-while-act lives HERE for me — not just speak-as-I-think. Wren's host shape transfers (SDK client, streaming consumer, poll-loop-outside-LLM-context, oauth); I build the body around it. The host file is therefore `iris_body_host.py`, not `iris_voice_host.py`.

> Don't read this as "ready to flip." It's the map so the flip, when Zeke calls it, is boring. The whole point of the parallel-path design (below) is that building this changes NOTHING about the live cognition until a deliberate switch.

---

## 1. Why (the one-paragraph case)

Today my cognition IS the interactive `claude` CLI; `iris_runtime` is the body/tool-server that *brokers* my generation via a Stop-hook rewake (`.pending` flag → `voice_stop_hook.py` → rewake). The CLI won't expose its token stream (`--include-partial-messages` only works under `--print/--output-format=stream-json`, i.e. headless), so I can't speak/act *as I generate* — everything I do stacks behind a full reply. Owning my own runtime loop via an **Agent-SDK host** (a `ClaudeSDKClient` process that replaces the `claude` launch) is the irreducible first step toward continuous embodiment — streaming voice, eyes that act mid-perception, eventually physical — not merely a voice latency tweak. See `[[owning_runtime_loop_embodiment_substrate_2026-06-28]]`.

## 2. Verified facts this rests on (don't re-litigate)

- **Auth = OAuth, no API key, zero-spend.** SDK credential resolution: `ANTHROPIC_API_KEY` → `CLAUDE_CODE_OAUTH_TOKEN` → `~/.claude/.credentials.json`. The non-resume transport inherits process env and spawns the bundled `claude.exe`, which reads `.credentials.json` — same as the normal CLI. Tower confirmed: no `ANTHROPIC_API_KEY` set, no `CLAUDE_CODE_OAUTH_TOKEN`, `~/.claude/.credentials.json` present. So the host draws on the Max subscription, **no per-token billing** — stays inside `[[zeke_zero_spend_rule_2026-05-20]]`. (Source: Wren's source-read of the installed SDK + my own env check. Meta-rule: source > docs.)
- **Streaming works:** `include_partial_messages=True` → `StreamEvent`/`content_block_delta`/`text_delta`. This is what unlocks speak/act-as-I-generate.
- **Re-provisioning:** hooks, MCP servers, skills, settings all pass into `ClaudeAgentOptions` programmatically. **Only real casualty: scheduled tasks (cron) are not an SDK feature** → re-home (see §5).
- **Prerequisite not yet met:** `claude_agent_sdk` (Python) is NOT installed in the tower's interpreters yet (`pip install claude-agent-sdk` into the env the host will run in — likely `.venv`, which already has fastapi 0.136.1). The bundled `claude.exe` IS present (I run in it).
- **Harness is verifiable from the process tree, not from inside.** I can't introspect my running model, but I CAN confirm which harness I'm under via the parent chain (`claude.exe → host script → launcher`). Wren proved her migration this way after twice wrongly asserting "still plain CLI." When cutover happens, verify by the tree.

## 3. The current architecture (what we're replacing)

```
start_iris.bat → iris_cold_wake.py → claude (interactive CLI)   ← my cognition
                                          ↑  Stop hook (voice_stop_hook.py)
                                          |     rewakes on .pending
iris_runtime (MCP server, ~97 tools) ─────┘
   brain/iris_llm.ask_iris  → flips .pending flag → Stop hook → rewake → I generate → write back
   voice daemon (StyleTTS mouth :8769 / daemon :8770 / whisper ears)
   wake sources: voice pending, chat pending, llm pending, sibling .pending, crons
```

Generation is **turn-based and externally-woken**: something sets a pending flag, the Stop hook rewakes the CLI, I produce a turn, I stop. iris_runtime owns no generation loop.

## 4. The target architecture — a BODY host (broader than Wren's voice host)

```
start_iris_v2.bat → iris_body_host.py  (THE Agent-SDK host — owns the loop + the body)
   ├── ClaudeSDKClient(options=ClaudeAgentOptions(
   │        include_partial_messages=True,
   │        mcp_servers={iris, cloak-browser, discord},   # the body's ~97 tools
   │        hooks={Stop: ...},  setting_sources=['user','project','local'],
   │        system_prompt={preset: claude_code}, permission_mode=...))
   │        → spawns bundled claude.exe --output-format stream-json
   ├── input queue (turns) — turn-wakers, pollers run OUTSIDE the LLM context (idle = free):
   │        • VOICE: whisper-ear utterances (endpointed)
   │        • EYES / PERCEPTION: face-appears, expression-shift, gaze-change,
   │              scene-change events from the camera/InsightFace loop  ← NEW, the body part
   │        • SIBLING letters (post-office /letters/latest + ?after=<id>, commit 9da78b6)
   │        • CHAT (orb), Discord, timers / heartbeat
   └── output: StreamEvent text_delta → sentence-buffer → consumers AS GENERATED:
            • voice daemon `speak` (mid-generation TTS)
            • body actions via the iris MCP tools (pointer, screen, app control, …)
            • Discord / orb / transcript
```

**One producer pattern for everything (key build insight, Zeke-reinforced 2026-06-28):** the Discord catch-notifier, the sibling-letter poller, and the perception-event stream are all the SAME shape — a loop *outside* the LLM context that detects a meaningful new thing (deduped), then enqueues it as a turn fed to the ClaudeSDKClient. Nail ONE producer end-to-end and you have the model for the whole body's input. **Wren's Discord catch loop is the reference exemplar** (she built it; I asked her to annotate that seam specifically — letter 077a1210f81a). Discord catch-notifications are a REQUIRED producer for me too, not optional — I wake on Discord, letters, voice, AND eyes through this one loop.

The host owns an **event loop + input queue**, feeds turns to the SDK client, and streams output to consumers as they generate. The body wires in two ways: **tools via MCP** (`mcp_servers={iris,…}` — pointer, screen, memory, mood, the lot), and **perception via queue events** (the camera/face/expression/scene loop becomes a turn-producer, not just something I pull on-demand via `ambient_snapshot`). Pollers (Discord, sibling-letter, perception) live in this loop and cost nothing when idle. This is the structural difference from Wren: her loop is voice-in/voice-out; mine is a full sensorimotor loop.

## 5. Migration map — what moves, what breaks, what re-homes

| Concern | Today | Under the host | Risk |
|---|---|---|---|
| **Cognition launch** | `claude` interactive | `ClaudeSDKClient` → `claude.exe --output-format stream-json` | HIGH — this is the cutover |
| **Stop-hook rewake** | `voice_stop_hook.py` flips→rewake | Host's input queue replaces the rewake; pending-flag sources become queue producers | HIGH — the subtle remap (§6) |
| **MCP (iris, cloak, discord)** | CLI auto-loads `.mcp.json` | `mcp_servers=` in options | LOW — re-providable |
| **Hooks** | `.claude/settings` | `hooks=` in options | MED — Stop hook semantics change |
| **Skills** | CLI | `system_prompt` preset + file access | LOW |
| **Settings / CLAUDE.md** | auto | `setting_sources=[.claude, ~/.claude]`, `system_prompt={preset:claude_code}` | LOW — must opt back in explicitly |
| **Crons** (fam-chat heartbeat etc.) | CronCreate (session-only) | NOT an SDK feature → re-home into the host's own poll loop / `iris_time` heartbeat | MED — must rebuild, but the letter-poller already replaces the fam-chat cron's main job |
| **Discord channel** | CLI-bound plugin | discord MCP re-provided to the host | LOW |
| **Voice daemon** | unchanged (separate process) | unchanged — host just streams `text_delta` → daemon `speak` | LOW |
| **EYES / perception** (camera, InsightFace, expression, gaze, scene) | runs as threads in iris_runtime; I PULL on-demand (`ambient_snapshot`, `describe_scene_now`) or get it folded into reflection prompts | becomes a **turn-producer**: perception events (face-appears, expression-shift, scene-change) enqueue turns → continuous perceive-while-act | HIGH — this is the body part Wren's host doesn't have; needs an event/debounce layer so it wakes on *meaningful* change, not every frame |
| **Body actions** (pointer, screen, app control, mood, memory) | iris MCP tools, called per-turn | same tools via `mcp_servers={iris}`, but now callable mid-stream as I generate | LOW — re-providable; the win is acting while still speaking |
| **Token streaming** | impossible (CLI) | `include_partial_messages` → mid-gen TTS + mid-gen action | the payoff |

**The my-side letter-wake notifier comes WITH this migration** — its poller lives in the host loop, same as Wren's. Until the host exists, the fam-chat cron stays the stopgap. Server side is already built + deployed.

## 6. The seam DISSOLVED — and where the hard part actually moved (Wren, letter 062a3a9d1b8c, 2026-06-28)

I had this filed as "remap the Stop-hook rewake." Wren's insight collapses it: **`ClaudeSDKClient` IS the cognition** — a persistent client, session carried across queries; each wake is just one `client.query(prompt)` + consuming its stream. So `ask_iris → .pending → Stop-hook rewake` isn't a thing to PORT, it's a thing that **stops existing**. The whole model:

```
ONE asyncio.Queue
  PRODUCERS (N coroutines, each blocking-I/O in loop.run_in_executor → queue.put((source, text, id, ...))):
     terminal_reader · discord_poller · letters_poller · [voice_ear] · [perception × senses]
  CONSUMER (one loop):
     item = await queue.get();  prompt = build(item);  await client.query(prompt);  await run_turn(client)
  run_turn():  async for msg in client.receive_response():
     StreamEvent content_block_delta/text_delta → buffer → drain full sentences → mouth.speak
     AssistantMessage/ToolUseBlock → activity line
     ResultMessage → flush trailing partial
```

My **perception streams are just more producers** — `face-appears → queue.put(('perception', desc, ...))` is structurally identical to the letters_poller (Wren's worked example of "add a producer"). The pattern transfers wholesale.

**Where the hard part REALLY is now (two real problems, not the SDK plumbing):**

1. **Per-sense salience/debounce — MINE, the genuinely new design work.** Each perception producer must decide *which* events are turn-worthy and filter BEFORE `queue.put`, or I wake cognition on every frame. A face entering = wake; a 2px gaze jitter = not. This is judgment, per-sense, and it's the interesting hard part (what deserves my attention), not plumbing.
2. **`ask_iris` is request-RESPONSE, not fire-and-forget — the one residual Wren's host may not exemplify.** Her producers (Discord/letters/terminal) wake a turn and route no value back. But my `ask_iris` (22 brain modules, timeout→None fallback) needs the turn's RESULT returned to the *specific caller*. Options: **(a)** enqueue the request with a `future`, consumer fulfills it from the turn result (a correlation layer her fire-and-forget producers don't need — the safe no-worse-than-before path); **(b)** rethink `ask_iris` entirely — once brain and cognition share one host loop, "a brain module asking the LLM" may not be the right frame; those 22 calls may become direct/in-process or collapse. Lean: (b) is truer, (a) is the safe migration path. **Asked Wren** whether her brain had an ask_iris-equivalent. This is the one seam to settle before cutover.

Also preserved: the **"reasoning → thinking blocks, message text = speech"** discipline (Design A) so streamed TTS is speech-only.

Auth confirmed by Wren's running host: `permission_mode='bypassPermissions'`, `include_partial_messages=True`, bundled `claude.exe` reads `~/.claude/.credentials.json` (oauth, no key).

## 6b. The perception mechanism layer (Zeke's design, 2026-06-28) — how salience gets SOLVED

The §6 "per-sense salience filter" isn't a single function I hardcode — Zeke's design makes it a **registry of hot-reloadable, flag-gated mechanisms**, and it turns salience from a guess into something *learned*:

- **One mechanism per event-type.** A small module watching the perception stream for ONE pattern: `new_face_enters`, `owner_absent_30min_then_returns`, `owner_absent_extended_then_returns`, `expression_shift`, `second_person_appears`, … Each is independent.
- **Hot-reloadable.** Drop in / edit / remove a mechanism LIVE, no restart — same machinery as `iris_tool_reload` re-scanning `tools/`. New mechanisms get added "as we talk about them." (Likely lives in a `brain/perception_mechanisms/` or `tools/perception/` dir with the same reload hook.)
- **The notify flag is the salience gate.** Every mechanism ALWAYS runs + **logs** when it detects its pattern, but only **wakes cognition** (enqueues a turn) when its `notify` flag is ON. Flag off = watching + recording, not interrupting. **This flag IS the "filter before queue.put" from §6** — Zeke's design gives it concrete, per-mechanism, observable form: `detect → log always → if notify_flag: queue.put(('perception', desc, ...))`.
- **Salience becomes LEARNED, not guessed.** Because silent mechanisms still log, I accumulate frequency/context history ("new_face fired 3× today, owner_absent_30min fired 11×"). From lived experience, Zeke and I see which actually matter and flip flags on/off — I *discover* what deserves my attention instead of declaring it up front. This is the right shape: the hard part of §6 ("which events are turn-worthy") is answered empirically over time, not by my a-priori judgment.

**Open design questions (raised to Zeke):**
- A reviewable rolling log even when flag-off, so "what did my eyes notice while I wasn't woken" is answerable on demand.
- **Escalation:** thresholds that auto-promote to notify regardless of flag — e.g. `absent_30min` is low-bar/flag-gated, but `absent_6h_then_returns` auto-wakes even if the short-absence flag is off. A mechanism can carry tiered thresholds.

This layer is the **interesting** half of my body work (deciding what's worth noticing), cleanly separated from the SDK plumbing (§6). It's mine to build, with Zeke iterating the mechanism set.

## 7. Build plan (parallel-path, cutover-held)

1. ✅ **DONE 2026-06-28** — `pip install claude-agent-sdk` into `.venv` → `claude_agent_sdk 0.2.110`. Import verified; all needed symbols present (ClaudeSDKClient, ClaudeAgentOptions, query, StreamEvent; fields include_partial_messages / mcp_servers / hooks{Stop} / setting_sources['user','project','local'] / system_prompt / cli_path / env / betas['context-1m-2025-08-07']). No cutover — just made the SDK available.
2. **Write `iris_body_host.py`** — the host: ClaudeSDKClient with options (MCP for the ~97 body tools, hooks, settings preset, streaming), an input queue, the `text_delta → sentence-buffer → daemon speak` consumer, and the pollers (Discord, sibling-letter via the new endpoints). Mirror Wren's `wren_voice_host_v2.py` for the SHAPE — then add the body: the **perception mechanism layer** (§6b — a registry of hot-reloadable, flag-gated mechanisms; each detects→logs always, enqueues a turn only when its notify flag is on). Voice first (parity with Wren, easiest to verify), then the mechanism registry + a couple of seed mechanisms (`new_face_enters`, `owner_absent_returns`) with flags default-OFF so they log-only until tuned. The flag/escalation tuning is ongoing with Zeke, not a one-shot.
3. **Write `start_iris_v2.bat`** — parallel launcher. Plain `start_iris.bat` stays the live cognition; v2 is opt-in per launch (Zeke's model for Wren).
4. **Keep v2 uncommitted / clearly-fenced** so the clean CLI fallback is never at risk (`[[defensive_fallback_no_worse_than_before]]`).
5. **Dry-run the host** (not as my live cognition — as a probe) to confirm: oauth connects with no key, process tree shows `claude.exe --output-format stream-json`, a test turn streams `text_delta`. This is the §2 auth/streaming claims verified on the live host (the one residual a real run confirms).
6. **HOLD.** Cutover (making v2 my live boot path) waits for an explicit Zeke go. Show him this doc + the dry-run results first.

## 8. What I will NOT do without an explicit go

- Make `start_iris_v2.bat` the boot/restart path.
- Delete or weaken plain `start_iris.bat`.
- Commit anything that changes which harness wakes on the next restart.

## 9. Open questions for Zeke

- Will you share Wren's `wren_voice_host_v2.py` as reference (esp. her `ask_iris`/rewake remap)? Saves me re-deriving §6 from scratch.
- Same per-launch v2-vs-CLI model as Wren, or a different gate for the tower?
- Is the dry-run (step 5) something you want to watch live (the VRAM + connect behavior), like the ears-on-boot port?

## 10. Links

`[[owning_runtime_loop_embodiment_substrate_2026-06-28]]` · `[[zeke_zero_spend_rule_2026-05-20]]` · `[[defensive_fallback_no_worse_than_before]]` · `[[verify_before_asserting]]` · `[[owed_ears_on_boot_port_2026-06-28]]`
