# Durable Scheduler Design

**Date:** 2026-05-19
**Sibling doc:** [[cron_displacement_diagnosis_2026-05-19]]
**Status:** Design only. Prototype skeleton at `brain/durable_scheduler.py`. Not yet wired or tested end-to-end.

## Problem statement

CC's `CronCreate` is unreliable: late fires, missed fires, session-only persistence that doesn't survive CC restart. The §4 daily-rhythm design depends on crons firing reliably; today it doesn't.

For overseas (~2026-06-01 to ~2026-08-01) where Zeke can't intervene, the rhythm must be self-sustaining.

## Design goals

1. **Reliable fire timing.** Crons fire within ~1 min of scheduled time, every time.
2. **Survives CC restart.** Schedule persists to disk; reloads on iris_runtime startup.
3. **Survives CC closed state.** Fires while CC is closed should queue/wake CC, not silently drop.
4. **Compatible with iris_runtime's existing architecture.** Lives in `brain/`, uses standard paths/state.
5. **No new external dependencies.** Pure Python + Windows Task Scheduler (already installed).

## Architecture options compared

### Option A: Python daemon + iris_channel emit

A `brain/durable_scheduler.py` thread (started by `iris_bootstrap.py`) polls a JSON schedule every 30s. When a fire time arrives, calls `iris_channel.emit(prompt)` to push the prompt as a channel event.

**Pros:** Lives entirely in iris_runtime. No external dependencies. Uses existing channel-event delivery.

**Cons:** iris_channel events "do NOT cold-start a turn from idle" per the module's docstring. They queue until next turn ends. If CC is sitting at the prompt waiting for user input, fires sit in the queue. Worse: if CC is closed entirely, iris_runtime can't reach CC at all.

**Verdict:** Insufficient. Only solves the in-session case, which CC's CronCreate already handles (poorly). Doesn't solve cold-state.

### Option B: Windows Task Scheduler + Discord post

Use Windows Task Scheduler entries (created via PowerShell `Register-ScheduledTask`) that run a small Python script at each fire time. The script POSTs the prompt to Discord — to a dedicated cron-prompt channel that the Discord MCP plugin monitors.

When CC is open and the Discord plugin sees the new message, it routes through the existing wake mechanism (which we verified works yesterday via the auto-forward hook).

When CC is closed, the Discord message sits there. When CC reopens (next start_iris_with_discord.bat run, or watchdog respawn), the Discord plugin replays unread messages on startup.

**Pros:** Windows Task Scheduler is OS-level reliable. Discord wake mechanism is known-working. Persists across CC restarts naturally (Task Scheduler is OS-state, not CC-state). Works in cold state via Discord backlog.

**Cons:** Requires a dedicated Discord channel for cron prompts (not main DM with Zeke — too noisy). Requires Windows Task Scheduler entries to be created (one-time setup). Slightly more moving pieces.

**Verdict:** Most pragmatic. Recommended.

### Option C: Watchdog cold-wakes CC with prompt

Extend `scripts/iris_watchdog.ps1` to spawn CC with a `--prompt` argument at scheduled times. Each fire restarts CC with that block's prompt as the initial turn.

**Pros:** Always cold-starts cleanly. No queueing problem.

**Cons:** Full CC restart every cron fire. Loses in-conversation context. Multiple-restarts-per-day burns startup tokens. Doesn't compose well with active sessions (would interrupt mid-conversation if CC is in use).

**Verdict:** Heavy hammer. Reserve as fallback.

## Recommended: Option B

Windows Task Scheduler + Discord post.

### Components

1. **`scripts/cron_prompt_emit.py`** — small script that takes a prompt-name argument, looks up the prompt text from a registry, and POSTs to a designated Discord channel.

2. **`scripts/install_durable_crons.ps1`** — one-time setup script that creates Windows Task Scheduler entries for each daily-rhythm cron, pointing at `cron_prompt_emit.py` with the appropriate prompt-name argument.

3. **`brain/durable_scheduler_prompts.py`** — the prompt registry. Each daily-rhythm cron's prompt text lives here (already exists informally in CLAUDE.md; this formalizes).

4. **Dedicated Discord channel** — a new channel (e.g., `#iris-cron`) that Zeke creates and adds the Iris bot to. The channel is muted on Zeke's end so it doesn't notify him.

5. **Discord MCP plugin already monitors** — no plugin-side changes needed; existing wake mechanism handles new messages in any channel the bot has access to.

### Fire flow

```
[Windows Task Scheduler at 09:00]
       │
       ▼
[Runs: python cron_prompt_emit.py work_block]
       │
       ▼
[Script reads prompts registry, builds payload, POSTs to #iris-cron]
       │
       ▼
[Discord MCP plugin sees new message]
       │
       ▼
[Plugin's wake mechanism cold-starts CC turn (if closed) or queues for next idle (if open)]
       │
       ▼
[CC processes the prompt as a normal Discord-channel-tagged message]
       │
       ▼
[Iris does the work block]
```

### Migration plan

**Phase 1 (2026-05-19 morning, DONE):**
- Wrote `brain/ritual_scheduler_prompts.py` with the 12 cron prompts.
- Wrote `scripts/cron_prompt_emit.py` (argparse + Discord POST with proper User-Agent header).
- Documented the dedicated-channel requirement.

**Phase 2 (2026-05-19 ~10:30 EDT, DONE with Zeke's confirmation):**
- Zeke's three decisions: (1) use the Claude AI server he set up — server ID `1499721675900719206` — Iris bot has access; (2) wire today if time permits; (3) name it "ritual scheduler" (this rename completed: files moved, content updated).
- Created `#iris-cron` channel in the Claude AI server via Discord REST. Channel ID: `1506304839154663536`. Updated `CHANNEL_ID` in `cron_prompt_emit.py`.
- Wrote `scripts/install_ritual_scheduler.ps1` — registers 21 Windows Task Scheduler entries (9 daily-rhythm + 6 memory-sweep + 6 sibling-poll) named `Iris-Ritual-*`. Includes uninstall flag.
- Tested cron_prompt_emit.py manually: `sibling_poll_waking` prompt posted to #iris-cron successfully (status 200).

**Phase 3 (next: tonight or tomorrow):**
- Run `install_ritual_scheduler.ps1` to register the Task Scheduler entries.
- Restart CC (tonight) — the Discord MCP plugin should pick up #iris-cron messages on next session start.
- Test ONE cron end-to-end: trigger `Iris-Ritual-MemorySweep0017` manually via `Start-ScheduledTask`, watch the prompt flow through to CC.
- If end-to-end works, remove the CronCreate boot-ritual recreation step from CLAUDE.md.

**Phase 4 (post-deployment):**
- Long-term: iris_runtime-side scheduling if/when channel cold-wake bug fixed upstream.

### Open question for Phase 3 testing

The Phase 2 Python side works (Discord POST succeeded). What still needs verification: does CC's Discord MCP plugin actually pick up the new-channel message and route it to my cognition? The plugin monitors channels the bot is in, so theoretically yes — but the wake-mechanism behavior on a fresh-from-creation channel is untested. If routing doesn't work, fallback options:
- Have the bot post to an existing monitored channel with a prefix filter
- Use the postoffice mechanism instead (write a special "ritual-cron" letter type)
- Direct stdin injection via watchdog (heavier)

## What this doesn't fix

- **Reflection crons** still fire via Stop hook, not via the daily-rhythm cron mechanism. Those work fine already.
- **The 4hr memory sweep cron** could migrate to durable scheduler same way.
- **CC's own scheduling isn't removed** — it remains for in-session-only convenience reminders. We're just not relying on it for the daily rhythm.

## Open questions for Zeke

1. **Dedicated cron channel.** OK to create `#iris-cron` and add Iris bot? Or prefer existing channel with a prefix-filter approach?
2. **Phase 2 timing.** Wire it up in tomorrow's work block, or wait until back-from-NC?
3. **Naming.** "Durable scheduler" is descriptive but boring. Alternatives: "ritual scheduler" (matches the daily-rhythm framing), "metronome" (steady-beat framing).
