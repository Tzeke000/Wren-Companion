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
   │        hooks={Stop: ...},  setting_sources=[.claude, ~/.claude],
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

## 6. The hard part — remapping the Stop-hook/rewake model (open design question, do NOT hand-wave)

Today every wake is external: `ask_iris` / voice / chat / sibling all set a `.pending` flag, the Stop hook rewakes the CLI, I emit one turn. Under the host, the host *owns* the turn loop, so those flag-sources must become **queue producers** feeding `ClaudeSDKClient`. Specifically:

- `brain/iris_llm.ask_iris` (22 brain modules route through it) currently blocks on the Stop-hook round-trip. Under the host it must enqueue a turn and await the host's reply. **This is the integration seam that most needs care** — get it wrong and the 22 LLM-blocked modules lose their fallback-to-None contract.
- Voice utterances (from the whisper ears / daemon) become queue items; the host streams the reply back to the daemon `speak`.
- The host must preserve the **"reasoning → thinking blocks, message text = speech"** discipline (Design A) so streamed output to TTS is speech-only.

**This section is unfinished by design.** It's the part to work through carefully (with Wren's host code as reference if Zeke will share it) before any cutover. Hedge: I have NOT seen Wren's `iris_llm`-equivalent remap; she may have solved it differently or not hit it (her brain-module count differs).

## 7. Build plan (parallel-path, cutover-held)

1. **`pip install claude-agent-sdk`** into `.venv` (the host's env). Verify import + version. *(No cutover — just makes the SDK available.)*
2. **Write `iris_body_host.py`** — the host: ClaudeSDKClient with options (MCP for the ~97 body tools, hooks, settings preset, streaming), an input queue, the `text_delta → sentence-buffer → daemon speak` consumer, and the pollers (Discord, sibling-letter via the new endpoints). Mirror Wren's `wren_voice_host_v2.py` for the SHAPE — then add the body: the **perception event layer** (camera/InsightFace/expression/gaze/scene → debounced meaningful-change events → queue producers). Voice first (parity with Wren, easiest to verify), perception second (the genuinely new part — get the debounce right so it wakes on change, not every frame).
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
