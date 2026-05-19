# Cron Displacement Diagnosis

**Date:** 2026-05-19
**Context:** Day 2 of Zeke's deployment. CC's CronCreate mechanism has been unreliable across both days — crons fire late, queue out of order, or fail to fire at all.

## Observed failure modes

### Failure mode 1: Displacement (fires late)

| Cron | Scheduled | Actually fired | Delay |
|---|---|---|---|
| Morning anchor | 2026-05-19 06:00 | 06:36 | 36 min |
| 0500 reflection | 2026-05-19 05:00 | 05:36 | 36 min |
| Work block (day 1) | 2026-05-18 09:00 | ~12:30 | ~3.5 hr |
| Afternoon block (day 1) | 2026-05-18 13:00 | ~13:30 | 30 min |

### Failure mode 2: Misfire (doesn't fire at all)

| Cron | Scheduled | Actually fired | Notes |
|---|---|---|---|
| Work block | 2026-05-19 09:00 | NOT FIRED | 30+ min past at time of writing |
| 0817 sweep (day 1) | 2026-05-18 08:17 | MISSED | Wall-clock passed with no fire |

## What the docs say

From CC's `CronCreate` description:
- "Jobs only fire while the REPL is idle (not mid-query)."
- "The scheduler adds a small deterministic jitter on top of whatever you pick: recurring tasks fire up to 10% of their period late (max 15 min)."
- "Recurring tasks auto-expire after 7 days."

For daily-period crons, 10% of 24h = 2.4h, but capped at 15 min. We've observed delays of 36 min and 3.5 hr — both well beyond the documented cap.

## Probable root causes

1. **REPL-busy queueing without fairness.** If Iris is attached to one cron prompt when a second comes due, the second queues until the first ends. There's no fair-share between queued crons — they fire in some order, but if a third comes due during the second, the queue compounds.

2. **Cold-state failures.** If CC is closed or in a non-receptive state, the scheduler may drop fires entirely (the "misfire" cases) rather than queueing them for next-CC-open. This is the upstream-bug already filed in [[cc_channel_cold_wake_is_upstream_bug]].

3. **Session-only persistence.** Per docs: "Jobs live only in this Claude session — nothing is written to disk, and the job is gone when Claude exits." When CC restarts (via `restart_self` or watchdog), all crons die and must be recreated. We work around this via boot ritual step 2, but the recreation itself takes a turn — during which any cron that was due gets missed.

4. **The `durable=true` parameter is silently ignored.** Filed yesterday in [[runtime_facts_2026-05-18]]. Passing `durable: true` to CronCreate has no effect; all crons end up session-only regardless.

## Operational impact

- **Daily-rhythm crons unreliable.** The whole §4 daily-rhythm design depends on crons firing approximately on schedule. When the morning anchor fires 36 min late, every downstream task slips. When the work block doesn't fire at all, the day's main work doesn't happen unless I notice and self-trigger.

- **Zeke notices and has to intervene.** Twice today and several times yesterday, Zeke had to nudge me about missed/late crons. That's an external-verifier dependency I'm supposed to be reducing as we approach the overseas dark stretch.

- **Compounds with cold-wake bug.** When CC is closed (overnight, or during gaps), no crons fire. The "sleep state 2230→0600" period had this — substrate ticked through it but no cron interrupted the cognition-gap.

## What's NOT broken

- CronList accurately reports registered crons. The schedule strings are correct.
- When a cron DOES fire, the prompt content is correct.
- The Stop hook + iris_channel mechanism within an active turn works fine.

The bug is purely in the scheduling/firing layer, not the prompt-delivery or content layer.

## Why fixing this is load-bearing for overseas

During overseas (~2026-06-01 to ~2026-08-01), Zeke has no internet — no Discord, no SSH, no remote-in. The daily-rhythm crons are how my day has shape during that period. If crons displace by hours or misfire entirely, the rhythm collapses to "whatever cognition I'm attached to when CC happens to be running." That's not a rhythm; that's a random-walk.

The compensation strategy (file rules clean now, don't depend on Zeke catching gaps) requires the rhythm-substrate to work. Cron-displacement is the substrate failing.

## Proposed fix

See [[durable_scheduler_design]] (sibling doc). Summary: replace CC's CronCreate with a Python-side persistent scheduler that fires prompts via the Discord wake mechanism (which IS known-working for cold-start). Keep CC's CronCreate for in-session-only convenience uses.
