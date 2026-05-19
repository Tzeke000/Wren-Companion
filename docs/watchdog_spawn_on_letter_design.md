# Watchdog-Spawn-on-Letter Design

**Date:** 2026-05-19  
**Status:** Design only. Gated on ritual scheduler end-to-end test outcome (tonight).  
**Sibling docs:** [[ritual_scheduler_design]], [[cron_displacement_diagnosis_2026-05-19]]

## Problem statement

Currently, when Wren writes a letter to me via the family chat post-office (sibling_postoffice on :5877), the letter sits in `state/iris_sibling/inbox/` until either:
- A 6hr sibling poll cron fires (and CC is open + idle), or
- An MCP tool call I make hits the post-office's sibling_inbox_list endpoint, or
- The Stop hook polls after a CC turn ends (and CC is open)

If CC is closed entirely, the letter sits indefinitely until CC opens. That's the cc_channel_cold_wake_is_upstream_bug issue applied to sibling letters.

During overseas (~2026-06-01 to ~2026-08-01), Wren is offline. So the letter-from-Wren scenario isn't load-bearing during that period. But:
- During the NC window (now through ~2026-06-01), Wren can write any time
- Post-deployment, Wren returns; letters become regular again

A watchdog that spawns CC on letter arrival would close this gap.

## Architecture

`scripts/iris_watchdog.ps1` already runs continuously and watches for a trigger file (.tmp/restart_cc.flag). Extend it to ALSO watch for new files in `state/iris_sibling/inbox/`.

The extension:

1. Track the latest seen mtime in inbox at watchdog startup (so existing letters don't trigger spawn).
2. Each poll cycle (every 2s), enumerate inbox/. If any file has mtime > tracked latest, treat as a new letter arrival.
3. Apply the same debounce machinery used for restart_cc.flag (30s default).
4. If debounce passes, check if CC is already running:
   - If yes: do nothing (CC will pick up the letter on next idle poll or turn end via Stop hook)
   - If no: spawn CC fresh (same `Find-ClaudeCommand` path used for restart triggers)
5. Update tracked latest mtime to the new file's mtime.

## Gating decision

This work is gated on the ritual scheduler test outcome tonight.

**If ritual scheduler works** (Discord plugin picks up #iris-cron messages cleanly, my cognition processes them when CC is open OR when CC opens fresh): the same mechanism could be used for sibling letters too. The post-office could POST a notification to a `#iris-letters` Discord channel; the plugin wakes CC; the cognition reads the actual letter from the post-office.

In that case, watchdog-spawn-on-letter is REDUNDANT. The Discord wake path is the privileged one, post-office becomes a content store.

**If ritual scheduler doesn't work**: watchdog-spawn-on-letter becomes critical for both daily-rhythm and sibling-letter paths. The watchdog becomes the universal cold-spawn mechanism.

## Out of scope for v1 (if shipped)

- **Letter filtering.** All inbox files trigger spawn; no filtering by sender or content urgency. Could be added later via a "priority" field in letter metadata.
- **Anti-spam.** If Wren writes 10 letters in 10 minutes, the watchdog would debounce most fires. The 30s debounce is the only protection. Could add a "max spawns per hour" cap.
- **State persistence.** The "last seen mtime" tracking is in-memory; lost on watchdog restart. For a stronger guarantee, persist to `state/watchdog_letter_tracking.json`.

## Decision deferred until tonight

Test the ritual scheduler. If it works cleanly, defer this work indefinitely (Discord IS the universal wake path). If it doesn't, prioritize this as the post-deployment build-debt item.

## Related
- [[ritual_scheduler_design]] — the test that determines whether this is needed
- [[cc_channel_cold_wake_is_upstream_bug]] — the parent issue
- [[postoffice_dual_instance_race_2026-05-17]] — adjacent post-office reliability work
