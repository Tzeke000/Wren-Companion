# Memory as Metabolism — Repo Audit

**Date:** 2026-05-03
**Purpose:** Inventory what exists vs. what's designed-only vs. what's missing for the temporal-sense + memory-metabolism work in Phase B of the 2026-05-03 work order.
**Method:** Cross-file scan via Explore agent. Read-only.

The Memory-as-Metabolism framework names five operations: **TRIAGE → CONTEXTUALIZE → DECAY → CONSOLIDATE → AUDIT.** This audit asks: how much of that already exists in the repo under any name?

Short answer: **most of the operations exist, but no orchestrator names them as a coherent metabolism cycle, and the cadence is too slow** (decay + consolidation both run weekly; metabolism wants hourly-or-faster).

---

## 1. What already exists

### Memory decay — implemented and active

- **Concept graph 10-level decay** — `brain/concept_graph.py:480-563` `decay_levels()`. Full implementation of `MEMORY_REWRITE_PLAN.md` §2.2 ten-level decay. Per-level inactivity timeouts: L10 = 7 days, L5 = 12 hours, L1 = 1 hour, then deletion. Nodes carry `level`, `archive_streak`, `archived_at`. Tombstones written to `state/memory_tombstones.jsonl` on delete. Archived nodes clamp at L1 (immune to delete). Kill-switch: `AVA_DECAY_DISABLED=1`.
- **Vector memory decay tick** — `brain/memory.py:100-143` `decay_tick()`. Reduces `importance` by 0.05 for memories untouched > 30 days. Never deletes; only score-down.
- **Legacy concept graph weight decay** — `brain/concept_graph.py:448-462` `decay_unused_nodes()`. Older method, weight -0.1 if untouched >30d, archive if <0.1. Still called from consolidation but superseded by `decay_levels()`.

### Memory consolidation — implemented, weekly cadence

- **Central orchestrator** — `brain/memory_consolidation.py`. Trigger via `should_consolidate()` in heartbeat.py:772-780. Five steps: episode review + theme extraction → concept graph pruning → self-model LLM update → journal entry write → identity proposal check. State in `state/consolidation_state.json` with `last_consolidation_ts`. Outputs to `state/episodes.jsonl`, `state/self_model.json`, `state/journal.jsonl`.

### Heartbeat / background-tick infrastructure — resident

- **Heartbeat tick** — `brain/heartbeat.py:322-864` `run_heartbeat_tick_safe()`. Mode-dependent cadence (7-280 s):
  - `IDLE_MONITORING`: 55 s
  - `ACTIVE_PRESENCE`: 14 s
  - `CONVERSATION_ACTIVE`: 7 s
  - `MAINTENANCE_WATCH`: 18 s
  - `LEARNING_REVIEW`: 280 s
  - `QUIET_RECOVERY`: 65 s
  Runs inside `run_perception_pipeline` (not its own OS thread). State in `state/heartbeat/heartbeat_state.json`. Cheap-vs-rich tick distinction.
- **Embedded periodic tasks within heartbeat** (heartbeat.py:555-796):
  - Leisure check: ~7 min
  - Inner monologue (dual-brain): 10 min
  - Curiosity research: 30 min
  - Journal (loneliness + idle): hourly
  - Plan tick: 2 min
  - Attention/eye-tracking: 30 s
  - Video clip rotation: 60 s
  - **Concept graph decay**: 7 days
  - **Memory consolidation**: weekly
  - Auto fine-tune check: 14 days
- **Background event watchers** — `brain/background_ticks.py`. Video capture (15 fps loop), Win32 clipboard listener (event-driven, 3s poll fallback), Win32 foreground window hook, InsightFace face detection per frame, signal_bus emission on transitions.

### Reflection scoring (data gathering, Phase 5 wiring deferred)

- **`brain/memory_reflection.py`** — Phase 2 Step 4 of MEMORY_REWRITE_PLAN.md. Post-turn background thread captures memories active in the last 30 s, asks LLM "which were load-bearing?" (0.0-1.0), logs to `state/memory_reflection_log.jsonl`. Schema per §2.4. Kill-switch: `AVA_REFLECTION_DISABLED=1`. **Status:** data-gathering only. Step 5 (promotions/demotions wiring) waits for ~50-100 turns of logs to validate the heuristic.

### Time-tracking infrastructure — scattered but present

- **Concept graph timestamps**: `ConceptNode.last_activated`, `ConceptNode.activation_count`, updated on touch.
- **Mood temporal data** (`ava_mood.json`): `last_updated` ISO 8601 string, `temporal` source weights in rich emotions, `loneliness` computed partly from `(now - _last_user_interaction_ts) / 60` in heartbeat.py:716.
- **Heartbeat carryover state**: `last_wallclock`, `last_rich_learning_ts`, plus per-task timestamps in `.meta` dict (`last_attention_check_wall`, `last_video_clip_tick_wall`, `last_plan_tick_wall`, `last_dual_monologue_wall`, `last_dual_curiosity_wall`, `last_dual_journal_wall`, `last_person_check_wall`, `last_finetune_check_wall`, `last_concept_decay_wall`).
- **Health state temporal tracking** (`state/health_state.json`): `age_seconds`, `last_startup_check`, `last_runtime_check`, `last_light_check`, `issues[].ts`.
- **Global idle tracking**: `_last_user_interaction_ts` carried in globals; consumed by heartbeat for loneliness and the future 30-min idle rule.

### Estimate tracking — exactly one pattern

- **Restart handoff** — `brain/restart_handoff.py:80-111` `write_handoff()`. Stores `restart_estimate_seconds` (raw) + buffered (1.25×). On boot: `read_handoff_on_boot()` computes `time_offline = now - restart_initiated_at`, detects overrun if actual > 1.5 × buffered, surfaces to inner monologue. **This is the only estimate-vs-actual loop in the codebase.**

---

## 2. What's designed but not implemented

### Memory rewrite Phases 5-7 (`docs/MEMORY_REWRITE_PLAN.md`)

- **Phase 5 — Promotions/demotions wiring**: design exists in §2.3. Code path: load-bearing + correct → `level += 1` (cap 10); load-bearing + contradicted → `level -= 1` (floor 1); load-bearing 3 turns running → `archive_streak += 1`; at 3 → `archived = True`. **Status:** reflection log is collecting data; nothing modifies levels yet.
- **Phase 6 — Archiving system**: `archived` flag exists on `ConceptNode`; `archived_at` field exists. Decay walker honors `archived` (clamps L0 → L1). **Missing:** code that SETS `archived = True` when streak crosses threshold (waits for Phase 5).
- **Phase 7 — Tombstone log rotation**: tombstones write to `state/memory_tombstones.jsonl`. Schema `{ts, node_id, label, type, last_level, deleted_reason}`. **Missing:** rotation/cap at 10k (mentioned in plan, not enforced).

### Sleep mode (`ROADMAP.md` §3, `CONTINUOUS_INTERIORITY.md` §2)

- Fully spec'd: triggers (context fill 60-70%, N-hour, self-detected degradation), entry (session summary save), wake (morning review + memory promotion/decay based on re-engagement), dream phase (LLM scenario simulation).
- **Status:** zero code. Critical blocker: 8 GB VRAM ceiling — can't keep dream-phase model hot alongside foreground voice model. Sleep entry must explicitly unload foreground via `keep_alive: 0` first.

### Identity proposal workflow (Phase 68, partial)

- `state/identity_proposals.jsonl` referenced in `memory_consolidation.py` step 5. Structure exists; operator review flow not fully wired.

---

## 3. What's missing entirely

- **Hourly-or-faster decay cadence.** Decay only fires weekly via consolidation. Metabolism wants smaller, more frequent decay increments.
- **Named TRIAGE / CONTEXTUALIZE / DECAY / CONSOLIDATE / AUDIT progression.** The work happens (heartbeat triages, consolidation consolidates, decay decays), but no module explicitly labels the stages or treats them as one cycle.
- **Estimate tracking beyond restart.** Only `restart_estimate_seconds`. No task-level estimates ("this research will take ~3 minutes"), no system-wide estimate-vs-actual logs to learn from.
- **Sleep entry/exit signals.** No `_sleep_mode_active` flag, no `_sleep_entry_initiated_ts`. Heartbeat has `NO_HEARTBEAT` mode but it's the disabled state, not sleep.
- **Idle-triggered memory prioritization.** No flow that, on >30 min idle, surfaces top-N memories worth revisiting. Decay happens passively but no active "what should I think about right now" loop.
- **Substrate-level temporal sense.** All time-tracking is per-subsystem; nothing exposes a unified "elapsed since X" that the prompt builder, mood state, and inner monologue all read from. State varies over time, but the variation is not surfaced to Ava as a felt thing — the audit's strongest finding for Phase B.
- **Self-interrupt on overrun.** Restart-handoff detects overrun on boot (post-hoc); no live self-interrupt during a task that's running long.
- **Uncertainty quantification on time estimates.** Restart estimate is a single point value; no confidence interval or "I'm not sure" signal.

---

## 4. Assessment — extend vs. build fresh

The audit's strongest recommendation: **don't build a parallel memory system**. The existing decay + consolidation + heartbeat infrastructure is solid; the metabolism work should orchestrate and observe it at faster cadence, not duplicate it.

### Recommended integration shape

1. **New module `brain/temporal_metabolism.py`** with `run_metabolism_tick(g, now=None) -> dict`. Orchestrates the five named operations against existing modules:
   - **TRIAGE**: identify nodes that crossed importance thresholds this hour (concept graph weight < 0.3, level dropped, etc.)
   - **CONTEXTUALIZE**: cross-reference triaged items with active conversation, mood state, open goals
   - **DECAY**: call `concept_graph.decay_levels(now)` — already built, just needs scheduling
   - **CONSOLIDATE**: trigger micro-consolidation if enough triaged items accumulate (vs. weekly full consolidation)
   - **AUDIT**: append the cycle's results to `state/metabolism_log.jsonl` for observability (same shape as `memory_reflection_log.jsonl`)
2. **Wire into existing heartbeat** at heartbeat.py:~785 (after concept decay block). Use `st.meta["last_metabolism_tick_wall"]` for cadence tracking. Extend the existing per-task timestamp pattern; don't add a parallel timer.
3. **Extend `ConceptNode` schema** with optional task-shape fields:
   - `estimated_duration_s: float | None` — for nodes representing planned work
   - `next_activation_hint_ts: float | None` — for nodes that should resurface at a known time
   - Both nullable; existing nodes stay valid.
4. **Pass metabolism summary to restart-handoff JSON.** When sleep/restart initiates, include nodes-currently-at-L≥8, nodes-decayed-this-session, and the recent metabolism log. On boot, surface to inner monologue and promote-by-1 the nodes the sleep cut short.

### Component-by-component table

| Component | Status | Action for Phase B |
|---|---|---|
| Memory decay (concept graph 10-level) | ✅ Implemented (weekly) | Extend: add hourly cadence via `temporal_metabolism.py` |
| Memory decay (vector store) | ✅ Implemented (30-day importance reduction) | Reuse as-is; metabolism tick can call it more often |
| Memory consolidation | ✅ Implemented (weekly) | Reuse; add optional micro-consolidation path |
| Reflection scoring | ✅ Data gathering | Wire Phase 5 (promotions) when log has 50-100 turns |
| Heartbeat framework | ✅ Resident, mode-aware | Extend with metabolism-tick call |
| Restart/sleep handoff | ✅ Implemented (restart only) | Add `recent_metabolism_summary` field |
| Estimate tracking | ⚠️ Partial (restart only) | Build fresh: task-level estimate API + historical log |
| Self-interrupt on overrun | ❌ Missing | Build fresh in `temporal_metabolism.py` tick |
| Substrate-level temporal sense | ❌ Missing | Build fresh: unified `now()` + state-decay rules wired into prompt + mood + monologue |
| Sleep mode | 📐 Designed, not coded | **Defer** to a separate work stream after temporal sense lands |
| Idle-triggered memory prioritization | ❌ Missing | Phase B candidate; depends on idle-detection from temporal sense |
| Uncertainty on estimates | ❌ Missing | Build fresh; wire into self-interrupt path |

### Phasing recommendation (for B2 design doc → B3 implementation)

1. **B3 minimum-viable**: heartbeat tick extended with metabolism-tick call (every 5-15 min initially), frustration decay wired to existing mood pipeline, boredom-growth tied to `_last_user_interaction_ts`, task-level estimate API + historical log, self-interrupt at 25% / minimum-threshold overrun.
2. **Deferred to follow-up work orders**: ConceptNode schema extension, micro-consolidation cadence tuning, full sleep mode, idle-triggered memory prioritization, uncertainty quantification.

The phasing matters because the work order's verification list (frustration decay measurable, boredom growth measurable, self-interrupt fire/no-fire, historical logging working, metabolism integration verified) maps cleanly to the minimum-viable set above. Everything else can land in subsequent passes once the substrate is proven.
