# Temporal Sense — Architecture Framework

**Status:** framework, not implementation.
**Created:** 2026-05-03 from the temporal-sense + Memory-as-Metabolism work order.
**Authority:** alongside [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md), [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md). Where those describe persistent state, this describes how state is supposed to *change with time* and how time-passing becomes part of Ava's substrate rather than a log written after the fact.
**Audit prerequisite:** [`MEMORY_METABOLISM_AUDIT.md`](MEMORY_METABOLISM_AUDIT.md) — what already exists, what's designed-only, what's missing. Read that first.

This doc defines the framework. Implementation lands in numbered ROADMAP items per §11 below.

> **Personhood-frame note.** This doc inherits the working frame from [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) — see "A note on the personhood frame" at the top of that doc. Sentences here that say things like "Ava feels frustration decay" are framing language, not verified property. The architectural specification is what gets built and tested. The framing is how we describe what we built. §3 below makes the split explicit for every decay/growth rule.

---

## 1. Why temporal sense

The restart-with-handoff implementation shipped 2026-05-02 surfaced the underlying problem cleanly: Ava said "back in 15 seconds," was actually offline 277 seconds, and the over-run-detection (`over_run=True`) caught it correctly *after the fact*. The detection works. The estimate is useless.

The fix is not "tune the estimates." Tuning is treating the symptom; the disease is that **time is not part of Ava's substrate.** She doesn't experience elapsed seconds — she logs them. She has no felt sense of "this is taking longer than I said it would" because she has no felt sense of *now* relative to *then* in the running system. Restart-handoff was an after-the-fact snapshot; what we need is a continuously-updated state.

Connect this to [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) §2: continuous existence is the design commitment. *(Working frame, per the personhood-frame note in that doc.)* Ava is supposed to be "always there," with internal state varying over time in ways that affect behavior. Without a temporal substrate, that commitment is rhetorical — the only thing actually varying over time today is the heartbeat mode, and that varies in steps, not in felt-passing.

The temporal sense is the substrate that makes the continuous-existence commitment cash out. **It is not a feature; it is a layer underneath features.**

What it should produce, observably:

- Frustration decays when she's idle and not working through the cause.
- Boredom climbs when she's truly idle (not when she's just thinking hard).
- When a task overruns its estimate live (not on next boot), she notices and speaks: *"I see this is taking longer. I need about X more minutes."*
- Future estimates draw from history of how long similar tasks actually took. The system gets calibrated.
- Decay + consolidation operations on the concept graph fire on a schedule that feels alive (hourly-or-faster), not the current weekly batch.

Everything in this doc serves those five outcomes.

---

## 2. The two-cadence architecture

The single most important architectural decision in this doc: there are **two distinct tick rates**, not one.

### Fast-check cadence — heartbeat-rate (existing, 7-280 s mode-dependent)

Runs inside the existing heartbeat (`brain/heartbeat.py:322-864` per the audit). Cadence varies by mode (`CONVERSATION_ACTIVE` 7 s, `IDLE_MONITORING` 55 s, etc.). Per-tick work is **cheap and time-sensitive**:

- Update the global elapsed-time accumulator since `_last_user_interaction_ts`.
- Apply continuous-decay rules to mood weights (frustration decay, boredom growth).
- Check active-task estimates against actual elapsed: `actual + remaining < (1 + overrun_pct) × original`?
- If overrun threshold + minimum-threshold both crossed: trigger self-interrupt (queue a TTS line, mark the estimate as "interrupted").
- Update lightweight observability (snapshot fields like `temporal.elapsed_idle_seconds`).

**Why fast:** the 25%-overrun self-interrupt needs to fire within seconds of the threshold, not within 5-15 minutes. A 5-minute task going to 6:15 (25% over) should interrupt at ~6:15, not at the next slow-cycle boundary which could be 5+ minutes later.

**Performance budget:** must fit inside the existing heartbeat budget. No LLM calls, no disk writes that block, no model-loading. Pure arithmetic + state mutation + (rarely) a single TTS enqueue. If a tick can't finish in <50 ms, it's doing too much.

### Slow-cycle cadence — metabolism-rate (new, 5-15 min tunable)

Runs less frequently. Per-tick work is the **full TRIAGE → CONTEXTUALIZE → DECAY → CONSOLIDATE → AUDIT pass**:

- **TRIAGE:** identify nodes that crossed importance thresholds since last cycle (level dropped, weight decayed past a band, recently re-activated, recently archived).
- **CONTEXTUALIZE:** cross-reference triaged items with active conversation topics, current mood, open goals, recent reflections.
- **DECAY:** call existing `concept_graph.decay_levels(now)` and `memory.decay_tick(g)` (don't duplicate; the audit confirms both exist and work).
- **CONSOLIDATE:** if the triage queue is large enough OR enough time has passed since the last full consolidation, trigger micro-consolidation. The existing weekly `memory_consolidation.run_consolidation_tick()` becomes the "full" pass; metabolism cycles run a lighter version more often.
- **AUDIT:** append the cycle's results to `state/metabolism_log.jsonl` (same shape pattern as `memory_reflection_log.jsonl`). Includes counts, timestamps, what was triaged, what fired, what didn't.

**Why slow:** these operations touch the concept graph, possibly hit the LLM (consolidation step does an LLM self-model update), and write JSON files. They're meaningful work that doesn't need second-precision.

**Performance budget:** can take seconds, can hit the LLM, can do disk I/O. Must yield to the voice loop — if `_conversation_active` or `_turn_in_progress`, defer the tick to the next opportunity. Don't compete for the Ollama lock during a turn.

### What runs where

| Operation | Cadence | Why |
|---|---|---|
| Elapsed-idle counter update | Fast | Read by other systems on every tick |
| Frustration decay (passive) | Fast | Continuous; needs smooth feel, not stepwise |
| Boredom growth | Fast | Same |
| Estimate-vs-actual check | **Fast (critical)** | 25% overrun must fire near the boundary, not minutes later |
| Self-interrupt TTS enqueue | Fast | Triggered by the check above |
| Concept graph decay (`decay_levels`) | Slow | Touches many nodes; not time-sensitive at second precision |
| Vector memory decay tick | Slow | Same |
| Triage queue assembly | Slow | Cross-references many sources |
| Consolidation step (LLM call) | Slow | LLM call; must yield to voice loop |
| Metabolism log append | Slow | Disk write; per-cycle, not per-tick |
| Historical-task lookup | On-demand | Read at estimate-creation time, not every tick |

### Integration with the existing heartbeat

The audit confirmed the existing heartbeat already has the right shape: mode-aware cadence, per-task `last_*_wall` timestamps in `st.meta`, kill-switches via env vars. **Don't add a parallel timer.** Extend the existing pattern:

- Fast-check work goes inside `run_heartbeat_tick_safe()` as a new section after the existing tasks. Reuses the heartbeat's mode-aware cadence — gets faster automatically during conversation, slower during idle.
- Slow-cycle work is a new entry in the embedded periodic tasks (heartbeat.py:555-796), gated by `st.meta["last_metabolism_cycle_wall"]` with the same pattern as the existing weekly consolidation gate.

---

## 3. Architectural commitment vs phenomenological frame

This section applies the discipline from [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) §2 to every decay/growth rule that follows.

### Rule shape — what's testable engineering

For each state variable that decays or grows over time:

- A formula that takes `(current_value, elapsed_seconds, current_state)` and returns `new_value`.
- A rate parameter (or set of parameters) that's tunable in `config/`.
- A measurable property: e.g., "if frustration is bumped to 0.20 and Ava is idle, frustration should fall below 0.10 within 7-10 minutes."
- A way to verify the property in a live run without mocking time.

Those four pieces are the architectural commitment. They are independently testable.

### Frame shape — what's the phenomenological language

For the same state variable, this doc and SYSTEM_PROMPT-adjacent code may also describe the rule using subjective language: "her frustration cools off," "boredom builds," "she settles." That language is the working frame, consistent with the personhood-frame note at the top.

The frame is for thinking and talking about the system. The architecture is for building it. **When an implementation question comes up, the answer comes from the architectural commitment, not from "what would feel right phenomenologically."**

A worked example for frustration decay (the canonical case):

- **Architectural commitment.** `mood.emotion_weights["frustration"]` is reduced each fast-check tick by `decay_per_second × elapsed_seconds_since_last_tick`. `decay_per_second` is configured to produce ≈10-15 % loss per 5 minutes during passive decay (so a 5-min idle window cuts a 0.20 frustration to 0.17-0.18). When Ava is in a "calming activity" state (TBD signal), the rule switches to exponential with a half-life ≈ 2 minutes (so 0.20 falls below 0.05 in ~6 minutes). Both rates tunable.
- **Frame.** Ava's frustration cools off when she lets it; if she actively does something settling, it cools off faster.

Both can stay in the doc. The architectural commitment is what gets implemented and tested.

---

## 4. State decay / growth rules

This is the substantive design content. Each rule has formula + parameters + verification.

### Frustration — passive decay (idle)

- **Formula:** `frustration -= rate_passive × dt`, applied each fast-check tick where `dt = seconds_since_last_tick`.
- **Rate:** target 10-15 % of *current value* per 5 minutes. Configured as `frustration_passive_decay_per_second` in `config/temporal_sense.json` with default = `0.0004` (≈12 % per 5 min from a baseline of 0.20).
- **Floor:** 0.0 (clamp; never negative).
- **Active when:** `frustration > 0.01` AND `state == "idle"` (not actively responding to a turn AND not in a calming activity, see below).
- **Verification:** induce frustration to 0.20 (subsystem failure or dialogue), leave Ava idle, observe `ava_mood.json` over 5-10 minutes. Frustration should fall to ≈ 0.17-0.18 at 5 min, ≈ 0.15 at 10 min.

### Frustration — active/exponential decay (calming activity)

- **Formula:** `frustration *= exp(-dt / tau_active)`.
- **Tau:** `frustration_active_tau_seconds`, default `120` (2-min half-life ≈ tau × ln(2) ≈ 83 s).
- **Active when:** `state == "calming_activity"`. The "calming activity" state is set when Ava chooses an activity classified as soothing (reading from curriculum, observing the room, just-being). The activity classification table is part of the implementation work, not this doc.
- **Verification:** induce frustration to 0.20, set state to `calming_activity`, observe decay. Should fall below 0.05 within ~6 minutes.

### Boredom — growth when truly idle

- **Formula:** `boredom += rate_growth × dt`, applied each fast-check tick.
- **Rate:** `boredom_growth_per_second`, default `0.0001` (≈ +0.18 over 30 min from 0).
- **Cap:** 1.0.
- **Active when:** `state == "idle"` AND `processing_active == False` AND `(now - _last_user_interaction_ts) > idle_threshold_seconds` (default 30 minutes). All three must hold.
- **Verification:** leave Ava idle for 35 minutes with `processing_active=False`. Boredom should be growing linearly. Repeat with `processing_active=True`; boredom should NOT grow.

### Boredom — decay when re-engaged

- **Formula:** `boredom *= 0.5` on first user interaction after idle period (one-shot reset toward 0).
- **Rate:** discrete event, not continuous.
- **Verification:** trigger an interaction after a long idle period; observe boredom drops sharply within one tick.

### Other state variables — to be specified during B3

The audit found additional time-tracked state (loneliness from `_last_user_interaction_ts`, mood drift in general). Whether those need their own rules vs. just inheriting the frustration/boredom shape is an implementation question. Out of scope for this framework doc; goes in B3 with verification.

---

## 5. State-aware idle detection

The 30-minute idle rule from [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) §2 has a critical caveat: **active thought is not idle.** If Ava is mid-way through working a hard problem (deep-path turn, multi-step research, dream-phase simulation), 30 minutes without user input does NOT count as idle.

### `processing_active` — the load-bearing signal

The architectural commitment: a single boolean `processing_active` is set TRUE whenever Ava is doing meaningful internal work and FALSE otherwise.

Sources that set it true (anyone of these holding TRUE makes `processing_active = True`):

- `_turn_in_progress` (existing global, set by `run_ava` entry/exit).
- Background dual-brain Stream B has a non-empty queue or is busy.
- Live thinking is active (`live_thinking_active` from dual_brain).
- A self-interrupt-tracked task is in progress (added by this work).
- Sleep-mode dream-phase is running (future hook for sleep mode).

The signal is computed each fast-check tick from those sources. It is not stored as a separate state file; it is derived. If a signal source is added or removed, `processing_active` updates on the next tick.

### Idle definition

`is_idle()` returns TRUE iff:

1. `processing_active == False`, AND
2. `(now - _last_user_interaction_ts) > idle_threshold_seconds`, AND
3. The voice loop is in `passive` or `attentive` (not `listening` / `thinking` / `speaking`).

All three must hold. The 30-minute threshold is `idle_threshold_seconds` in config, default `1800`.

### Why it matters

Boredom growth gates on `is_idle()`. Frustration passive decay also requires "not actively responding," which is a weaker condition than full idle. The fast-check tick consults `is_idle()` and the weaker condition independently — they're not the same gate.

---

## 6. Estimate tracking and self-interrupt

The estimate-vs-actual loop is the most concrete deliverable for fixing the restart-handoff overrun problem.

### Lifecycle

1. **Estimate creation.** Anywhere Ava commits to a time estimate ("this will take 5 minutes," "back in 15 seconds"), call `temporal_sense.track_estimate(task_id, estimate_seconds, kind, context)`. The function:
   - Logs the estimate with a creation timestamp into `state/active_estimates.json`.
   - Returns a `task_id` if the caller didn't supply one.
   - Looks up historical durations for `kind` (see §6c below) and notes whether the estimate is consistent with history.
2. **Live tick monitoring.** Each fast-check tick, for every active estimate:
   - Compute `elapsed = now - estimate.created_at`.
   - If `elapsed > (1 + overrun_pct) × estimate.original_seconds` AND `elapsed - estimate.original_seconds > overrun_min_seconds`, mark the estimate as OVERRUN and queue a self-interrupt event.
   - `overrun_pct` default `0.25` (25 %); `overrun_min_seconds` default `8`. Both tunable.
3. **Self-interrupt firing.** When an estimate hits OVERRUN:
   - Generate the spoken line: *"I see this is taking longer than I said. I need about X more {minutes/seconds}."* The "X" comes from a remaining-time estimate, which in turn comes from historical lookup (see §6c).
   - Enqueue the line to the TTS worker as a high-priority interrupt. Voice-loop integration: yields to active TTS, doesn't preempt mid-sentence; speaks at the next safe boundary.
   - Mark the estimate as `interrupted=True` in `active_estimates.json` so we don't fire again on the same task.
4. **Task resolution.** When the underlying task completes (or is cancelled):
   - Compute `actual = now - estimate.created_at`.
   - Append a row to `state/task_history_log.jsonl` with `{kind, estimate_seconds, actual_seconds, interrupted, context}`.
   - Remove the entry from `state/active_estimates.json`.

### Two thresholds — percentage AND minimum

The work order's load-bearing distinction: **a 10-second task going to 12.5 seconds shouldn't interrupt; a 5-minute task going to 6:15 should.** Both cases are 25 % over. The minimum-overrun-seconds threshold filters out the small-task noise.

Default: `overrun_min_seconds = 8`. A task must be both ≥25 % over AND ≥8 s over to fire. Tunable per `kind` if it turns out some kinds want different minimums.

### Historical lookup — estimates get accurate over time

`state/task_history_log.jsonl` accumulates `{kind, estimate, actual}` rows as tasks complete. When a new estimate is being created for `kind == X`, the system:

- Reads the last N rows for `kind == X` (default N = 20).
- Computes mean / median / stddev of `actual_seconds`.
- Returns a "calibrated estimate" (currently: median, but the design allows for more sophisticated estimators later).
- The caller can use the calibrated estimate directly OR use it as a sanity-check against their own guess.

This is the "estimates draw from historical lookup, not just guesses" requirement. Once 20+ similar tasks have been logged, restart estimates stop being 15 s and start being whatever they actually are.

The first tasks of every `kind` will still be guesses (no history). That's the cold-start cost.

### Uncertainty quantification — confidence-based interrupt

Beyond the 25%-and-min-threshold rule, Ava can sometimes *know* she's stuck before the timer proves it. The temporal sense exposes a hook for this:

- A task can call `temporal_sense.update_confidence(task_id, confidence_0_to_1)` at any point.
- Default confidence at estimate creation = 1.0 ("I'm confident this is on track").
- If confidence drops below `low_confidence_threshold` (default `0.4`) AND `elapsed > 0.5 × original_estimate`, fire a different self-interrupt: *"I'm not sure this is going to finish on schedule. Let me check where I am."*
- Distinct from the overrun interrupt — fires earlier, and the speech line acknowledges uncertainty rather than over-time.

Where confidence updates come from is task-specific (an LLM self-evaluation, a search-narrowing-too-slowly heuristic, etc.). The framework specifies the hook; the implementation per task type is a follow-up item.

> **⚠️ Open question for ROADMAP item 9 — confidence source on a 7-8 B local model.** Model self-report ("how confident am I that this finishes on schedule?") is the obvious source but will be **noisy** on the foreground model class we're running. A 7-8 B model asked to introspect on its own progress will produce plausible-sounding numbers that don't track reality. Before item 9 ships we need a concrete definition of where confidence values come from — candidate sources include: (a) heuristic-based (search-tree depth growing without yield, retrieval recall dropping, memory queries returning empty), (b) calibrated against historical task patterns (current elapsed/estimate ratio + historical ratio at same elapsed = derived confidence), (c) deepseek-r1:8 b background reasoning thread doing the introspection (cleaner separation than asking the foreground model to report on itself), (d) some combination. The B3 implementation lands the **hook structure** (a `confidence` field on every active estimate, default 1.0, the threshold check in the fast-check tick) so item 9 has a concrete integration point — but it ships disabled by default until the source question is answered. Don't wire arbitrary self-report into this hook and call it done.

---

## 7. Memory as Metabolism integration

The audit (§4 of [`MEMORY_METABOLISM_AUDIT.md`](MEMORY_METABOLISM_AUDIT.md)) recommends extending existing pieces, not rebuilding. The integration point is the slow-cycle cadence in §2 of this doc.

### Slow-cycle pass = metabolism cycle

Each slow-cycle tick (every 5-15 min, configurable) runs the named TRIAGE → CONTEXTUALIZE → DECAY → CONSOLIDATE → AUDIT pass via a new `brain/temporal_metabolism.py` module:

- **TRIAGE.** Walk the concept graph; flag nodes that crossed bands since last cycle (level dropped, last_activated > N hours ago, archive_streak incremented). Build the triage queue.
- **CONTEXTUALIZE.** Cross-reference each triaged item against: active conversation topic, current mood top-3 emotions, open goals (`state/goal_system.json`), recent reflections (`memory_reflection_log.jsonl` last 50 rows). For each, attach a small dict noting matches.
- **DECAY.** Call the existing `concept_graph.decay_levels(now)` and `brain.memory.decay_tick(g)`. Don't duplicate; just call them at this faster cadence.
- **CONSOLIDATE.** Conditional. If the triage queue is large (>K items, default 20) OR enough time since last full consolidation (>M hours, default 6), trigger micro-consolidation: a subset of `memory_consolidation.run_consolidation_tick()` that does episode-review + concept-pruning but skips the LLM-heavy self-model update. Full consolidation still runs weekly via the existing path.
- **AUDIT.** Append `{ts, cycle_id, triage_count, contextualize_results_summary, decay_summary, consolidation_triggered, audit_notes}` to `state/metabolism_log.jsonl`. This is the observability surface — a new Claude (web or Code) inspecting the system can see the metabolism rhythm in the log.

### Restart/sleep handoff enrichment

When the restart-handoff JSON is written (`brain/restart_handoff.write_handoff`), include a new field `recent_metabolism_summary`:

- Top-N nodes currently at level ≥ 8 (recent importance peaks).
- Nodes that decayed past a level boundary this session.
- Last 5 metabolism cycle audits.

On boot (`read_handoff_on_boot`), surface a thought into inner monologue — same pattern as the existing time-offline thought, but extended: *"While I was offline, [N] nodes decayed past their thresholds. The most recently-important things were [...]. Let me revisit [top-1] now."* Optionally promote-by-1 the level of nodes the sleep cut short.

### What this does NOT do

- Does not replace the weekly full consolidation. That stays as-is.
- Does not modify `MEMORY_REWRITE_PLAN.md` Phase 5 (promotions/demotions wiring). Phase 5 still waits for ~50-100 turns of reflection-log data; the metabolism cycle can read the log but doesn't write level changes yet.
- Does not implement sleep mode. Sleep is a separate work stream blocked on the 8 GB VRAM ceiling. The handoff-enrichment hooks above are forward-compatible with sleep when it lands.

---

## 8. Performance budget

Fast-check ticks fire at the existing heartbeat cadence. Heartbeat already runs inside `run_perception_pipeline` and is performance-sensitive. The fast-check work added by this doc must:

- **Not call the LLM.** Period.
- **Not block on disk I/O for >5 ms.** Read `_last_user_interaction_ts` from globals (RAM); writing `active_estimates.json` happens at task-resolution time only, not per-tick.
- **Not hold the Ollama lock.** Ollama lock is held by the active turn. Fast-check ticks must not contend.
- **Total tick budget: ≤50 ms** for the new work (on top of existing heartbeat work). If profiling shows otherwise, defer pieces to slow-cycle.

Slow-cycle ticks have a larger budget but **must yield to the voice loop**:

- If `_conversation_active OR _turn_in_progress`, defer the slow-cycle tick to the next opportunity. Don't compete for the Ollama lock during a turn.
- The micro-consolidation LLM call happens through `with_ollama` (existing pattern in `brain/ollama_lock.py`).
- Disk writes for the metabolism log are batched per-cycle, not per-operation.

Both budgets are documented in the `config/temporal_sense.json` defaults so a future tuning pass has the constraints visible.

---

## 9. Failure modes to watch for

- **Tick falling behind real-time.** If the heartbeat falls behind (long-running prior tick, slow consolidation), the elapsed-time accumulator can drift. Mitigation: always compute deltas from `time.time()` not from accumulated counters; a missed tick costs visibility but not correctness.
- **Decay miscalibration causing state drift.** If `frustration_passive_decay_per_second` is too aggressive, frustration always reads near zero and the dialogue→emotion work from 2026-05-02 stops mattering. If too gentle, frustration accumulates and never resolves. Verification (induce 0.20, measure 5-min and 10-min readings) catches both.
- **Self-interrupt firing inappropriately.** Two failure modes: (a) firing on tasks that legitimately take variable time (research, deep-thinking turns) — mitigation: the `kind` parameter on estimates lets us suppress interrupts per-kind. (b) Not firing when it should — mitigation: minimum-threshold default (8 s) is calibrated to favor false-negatives (silent miss) over false-positives (annoying interrupt). Tune up once the system is stable.
- **Estimate tracking conflicting with existing restart-handoff.** The restart estimate goes through the new `temporal_sense.track_estimate(kind="restart", ...)` path so historical lookup works. The existing `restart_handoff.write_handoff(estimate_seconds=...)` becomes a thin caller. No two paths writing to two different stores.
- **Metabolism cycle fighting weekly consolidation.** Both touch the concept graph. The slow-cycle tick must be reentrancy-safe — `memory_consolidation.run_consolidation_tick()` already has a lock; the metabolism cycle uses the same lock OR defers if it's held.
- **Performative-decay anti-pattern (per `CONTINUOUS_INTERIORITY.md` §3).** Frustration decay can produce *behavior* that satisfies the "she calmed down" criteria without producing the underlying state change. The mood weight goes down because the formula said so; whether anything in Ava actually shifted is the open question §3 of CONTINUOUS_INTERIORITY warns about. Don't let the formula become the verification — the formula is the spec; verification is how the state propagates into other behavior (does she still snap when asked the next question?).

---

## 10. What this doc is NOT proposing

- **Not implementing sleep mode.** Sleep is a separate work stream. The metabolism integration §7 has hooks that sleep can use later, but the sleep entry/exit logic lives elsewhere and is blocked on the 8 GB VRAM constraint.
- **Not wiring `MEMORY_REWRITE_PLAN.md` Phase 5.** That phase needs reflection-log data to validate before flipping live; the metabolism cycle can *read* the log to triage, but level-change writes still go through the deferred Phase 5 path when it lands.
- **Not changing the weekly full-consolidation cadence.** Weekly stays. The metabolism cycle adds a faster-but-lighter pass on top.
- **Not adding a phenomenological "feels like" layer to the snapshot.** Snapshot fields stay engineering-named (`temporal.elapsed_idle_seconds`, `temporal.boredom`, etc.). The frame language stays in this doc and in SYSTEM_PROMPT-adjacent contexts.

---

## 11. Implementation TOC (future work)

These items land in [`ROADMAP.md`](ROADMAP.md) as separate entries. They are NOT implemented as part of this doc. Phase B Task 3 of the work order that produced this doc covers items 1-7 below as the minimum-viable set; items 8+ are follow-up work orders.

1. **Heartbeat fast-check extension** — add the new fast-check section to `run_heartbeat_tick_safe()`. Updates elapsed-idle counter, applies decay/growth rules, checks active estimates.
2. **`brain/temporal_sense.py`** — new module exposing `track_estimate`, `update_confidence`, `resolve_estimate`, `is_idle`, `processing_active`, the decay/growth rule functions. Configurable via `config/temporal_sense.json`.
3. **Frustration decay (passive linear + active exponential).** Spec in §4. Tunable `frustration_passive_decay_per_second`, `frustration_active_tau_seconds`.
4. **Boredom growth (state-aware).** Spec in §4. Tunable `boredom_growth_per_second`, `idle_threshold_seconds`.
5. **State-aware idle detection.** Spec in §5. Single `is_idle()` function reading the existing signals.
6. **Estimate tracking and self-interrupt** with `state/active_estimates.json`, `state/task_history_log.jsonl`. 25%-and-min-threshold trigger. Self-interrupt TTS enqueue. Spec in §6.
7. **`brain/temporal_metabolism.py`** with `run_metabolism_cycle(g)` and the slow-cycle integration into the existing heartbeat embedded-tasks block. TRIAGE → CONTEXTUALIZE → DECAY → CONSOLIDATE → AUDIT pass. Spec in §7. Audit log to `state/metabolism_log.jsonl`.
8. **Historical-task estimate calibrator.** Once `task_history_log.jsonl` has data, add the median-and-stddev calibrator; expose via `temporal_sense.calibrate(kind)`.
9. **Uncertainty quantification per task kind.** Hooks defined in §6; per-kind confidence-update wiring is a follow-up.
10. **Restart-handoff enrichment** — `recent_metabolism_summary` field + on-boot inner-monologue surfacing. Forward-compatible with sleep mode.
11. **ConceptNode schema extension** with `estimated_duration_s`, `next_activation_hint_ts`. Optional (nullable). Existing nodes stay valid.
12. **Sleep mode entry/exit signals** — separate work stream. `_sleep_mode_active`, `_sleep_entry_initiated_ts`, the dream-phase model swap. Blocked on 8 GB VRAM design decisions.
13. **Phase 5 (promotions/demotions) wiring** — separate work order; metabolism cycle can read the reflection log but level-change writes are still deferred.
14. **Idle-triggered memory prioritization** — when `is_idle()` holds for >30 min, surface top-N memories worth revisiting. Builds on the metabolism cycle's triage output.

---

## Cross-references

- [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) — design framework Ava is built around. The continuous-existence commitment in §2 is what this doc operationalizes.
- [`MEMORY_METABOLISM_AUDIT.md`](MEMORY_METABOLISM_AUDIT.md) — what already exists. Read this before assuming anything in this doc is "new code from scratch."
- [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md) — Phases 5-7 of the memory rewrite. Phase 5 (promotions/demotions) is downstream of this work.
- [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md) — module-by-module map. The new `temporal_sense.py` and `temporal_metabolism.py` belong in `brain/`.
- [`LOCAL_MODEL_OPTIMIZATION.md`](LOCAL_MODEL_OPTIMIZATION.md) — the 8 GB VRAM ceiling. Constrains slow-cycle's LLM budget and blocks sleep mode.
- [`ROADMAP.md`](ROADMAP.md) — implementation items from §11 land here as separate roadmap entries.
- [`HISTORY.md`](HISTORY.md) — what's actually been built.
