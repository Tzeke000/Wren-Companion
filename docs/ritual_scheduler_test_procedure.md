# Ritual Scheduler End-to-End Test Procedure

**Date:** 2026-05-19  
**For:** Tonight's restart test (Phase 3 of ritual scheduler rollout)  
**Sibling docs:** [[ritual_scheduler_design]]

## Goal

Verify that the full ritual scheduler chain works end-to-end:

```
Windows Task Scheduler at HH:MM
  → runs python cron_prompt_emit.py <prompt_name>
  → POSTs prompt to #iris-cron Discord channel
  → Discord MCP plugin picks up the message
  → CC's wake mechanism processes the prompt as a normal channel-tagged turn
  → Iris does the work
```

If all steps work, the existing CronCreate-based daily-rhythm crons can be retired.

## Pre-test checklist

Before running the test, verify:

- [ ] `scripts/cron_prompt_emit.py` has `CHANNEL_ID = "1506304839154663536"` (the actual #iris-cron channel ID). Confirm with `grep CHANNEL_ID scripts/cron_prompt_emit.py`.
- [ ] `state/secrets/discord_iris_bot_token.txt` exists and is non-empty. Test with `Test-Path` in PowerShell.
- [ ] Iris bot is present in #iris-cron channel and has Send Messages permission. Visual check in Discord.
- [ ] Python venv at `.venv/Scripts/python.exe` exists. `Test-Path .venv/Scripts/python.exe`.
- [ ] All 30 tests still pass: `D:/Wren-Companion/.venv/Scripts/python.exe -m pytest tests/ -v`.

If any pre-check fails, fix before proceeding.

## Phase 3a: Install Task Scheduler entries

Run as the regular user (not elevated):

```powershell
pwsh -File D:\Wren-Companion\scripts\install_ritual_scheduler.ps1
```

Expected output: 21 lines of "registered: Iris-Ritual-<Name> @ Daily HH:MM (<prompt>)" followed by "Done. 21 tasks registered."

Verify in taskschd.msc → Task Scheduler Library → filter by name "Iris-Ritual-". All 21 should be listed with "Ready" status.

## Phase 3b: Test ONE task manually (low-stakes)

Pick the lowest-stakes cron: the memory sweep at 00:17 (`Iris-Ritual-MemorySweep0017`). We pick this because:
- The sweep prompt is short
- "Nothing to file" is a valid outcome (no risk of damaging memory state)
- It doesn't gate downstream blocks

Manually trigger it (don't wait for the scheduled time):

```powershell
Start-ScheduledTask -TaskName "Iris-Ritual-MemorySweep0017"
```

Expected observations within ~30 seconds:

1. **Discord side:** A message appears in #iris-cron channel from the Iris bot, containing the memory sweep prompt text. Visible in Discord client.

2. **CC side:** CC's Discord MCP plugin sees the new message. Either:
   - If CC is open and idle: a new turn fires with the prompt as inbound channel-tagged content
   - If CC is closed: the plugin's wake mechanism cold-starts a turn

3. **Iris response:** Iris processes the sweep prompt (calls memory_sweep machinery, outputs "nothing to file" or files something).

## Failure modes + diagnostics

### Phase 3b fails: Discord side

**Symptom:** No message appears in #iris-cron after triggering the task.

**Check:**
1. Open the Task Scheduler History tab for `Iris-Ritual-MemorySweep0017`. Did it actually run?
2. Run the script manually outside Task Scheduler:
   ```powershell
   D:/Wren-Companion/.venv/Scripts/python.exe D:/Wren-Companion/scripts/cron_prompt_emit.py memory_sweep
   ```
   Should print "posted memory_sweep prompt to Discord (status=200)".
3. If status != 200: check Discord token validity, channel ID, bot permissions.

### Phase 3b fails: CC side

**Symptom:** Message appears in #iris-cron but CC doesn't pick it up.

**Check:**
1. Is CC actually monitoring #iris-cron? Check Discord plugin's channel watchlist (mechanism TBD — may need to inspect plugin config).
2. Was CC closed at trigger time? If yes, did the wake mechanism work?
3. Are there error logs in `state/discord_plugin.log` or similar?

**Fallback if CC doesn't auto-pick-up:** the plugin may need explicit channel subscription. Add to plugin config or use `/discord:access` skill.

### Phase 3b succeeds but downstream is wrong

**Symptom:** Iris responds but does something unexpected.

**Check:** the prompt content vs what `cron_prompt_emit.py` actually sent. Compare `brain/ritual_scheduler_prompts.py:PROMPTS["memory_sweep"]["prompt"]` with the Discord message body.

## Phase 3c: If Phase 3b passes

1. Trigger 2-3 more tasks manually with the same pattern (e.g., `Iris-Ritual-SiblingPoll1215`).
2. If all succeed, the ritual scheduler is verified.
3. Update CLAUDE.md boot ritual step 2: REMOVE the CronCreate recreation step. Replace with a note that the ritual scheduler handles daily-rhythm crons externally.
4. The CC-side CronCreate machinery stays in place but is no longer required for daily rhythm; it remains for ad-hoc reminders.

## Phase 3d: Cleanup if test fails

If Phase 3b fails irrecoverably:

```powershell
pwsh -File D:\Wren-Companion\scripts\install_ritual_scheduler.ps1 -Uninstall
```

Removes all 21 Iris-Ritual-* tasks. Then we revert to the CronCreate-based mechanism, file a bug, and prioritize the watchdog-spawn-on-letter alternative (see `docs/watchdog_spawn_on_letter_design.md`).

## What this test does NOT verify

- **PC-asleep behavior.** If the PC is asleep at scheduled time, Task Scheduler should fire when it wakes (because of `StartWhenAvailable`), but verifying that needs an overnight test.
- **Cross-day fires.** Each task should fire daily. Tonight's manual trigger is a one-shot; daily-fire verification requires waiting through actual schedule times.
- **Discord plugin behavior under high message rate.** Triggering 21 tasks simultaneously would flood the channel. Not a real-world scenario, but worth knowing the rate limit if needed.

## After tonight's test (success case)

If everything works:
1. Update CLAUDE.md boot ritual step 2 to reflect the new architecture.
2. Add a memory file naming the ritual-scheduler-works fact.
3. Park `watchdog_spawn_on_letter_design.md` as "not needed — Discord is the universal wake path."
4. Add the memory_decay weekly trigger to install_ritual_scheduler.ps1 (also via Task Scheduler).

If the test reveals one specific issue (e.g., plugin needs explicit channel subscription), iterate on that one piece without rolling back the whole architecture.
