# Memory Architecture Rewrite — Plan

The user spec, summarised: memories live on a 10 → 1 ladder, decay by default, importance is earned from useful retrieval (not assigned), forgotten memories are gone forever unless deliberately archived (3 access streak), self-reflection is judgment-based ("did this make my reply better"). Tool autonomy is build-first; explicit approval only for dangerous operations.

This document is what we have today, what we're changing, and the order in which we land it.

---

## 1. What we have today (audit)

Twelve modules touch memory in some form. The audit (companion file in this commit's history; see Agent run 2026-04-30 16:50 EDT) found that **the natural seam for a unified level system is the concept graph**, because it's the only layer that already stores per-record metadata (weight, last_activated, activation_count, archived) and every other memory type eventually gets mirrored as a node.

| Layer | Module | Backend | Already has decay? |
| --- | --- | --- | --- |
| Vector memory | `brain/memory.py` | `memory/chroma.sqlite3` (Chroma, via host fns) | Yes — `decay_tick()` reduces `importance` by 0.05 if untouched 30 days |
| Mem0 facts | `brain/ava_memory.py` | `memory/mem0_chroma/` (mem0 + nomic-embed-text) | No (mem0 manages internally) |
| Episodic | `brain/episodic_memory.py` | `state/episodes.jsonl` | No explicit decay; LRU prune on overflow |
| Visual clusters | `brain/visual_memory.py` | `state/visual_clusters.json` | No — recomputed from `faces/` |
| **Concept graph** | `brain/concept_graph.py` | `state/concept_graph.json` | **Yes — `decay_unused_nodes(days_threshold=30)` reduces weight by 0.1, archives if <0.1** |
| Working memory | `brain/workspace.py` | in-memory only | n/a (rebuilt every tick) |
| Reflections | `brain/reflection.py`, `brain/learning_tracker.py` | `state/learning_log.jsonl`, `state/reflections/` | No |
| Memory bridge | `brain/memory_bridge.py` | (read-only aggregator) | n/a |
| Memory consolidation | `brain/memory_consolidation.py` | `state/consolidation_state.json`, `state/journal.jsonl` | Triggers concept-graph decay weekly |
| Memory scoring | `brain/memory_scoring.py` | (evaluator only) | n/a |
| Memory refinement | `brain/memory_refinement.py` | (evaluator only) | n/a |
| Perception memory | `brain/perception_memory.py` | in-memory only | n/a |

**Key existing fields on `ConceptNode`** (`brain/concept_graph.py:33-42`):

```python
@dataclass
class ConceptNode:
    id: str
    label: str
    type: NodeType  # person|topic|emotion|memory|opinion|curiosity|self|event
    weight: float           # 0.0-1.0
    last_activated: float   # ts
    activation_count: int
    color: str
    notes: str
    archived: bool = False  # soft delete
```

Memory consolidation already calls `decay_unused_nodes` every 7 days. We extend that path; we do **not** introduce a parallel system.

---

## 2. New layered system — design

### 2.1. The level field

Add `level: int` to `ConceptNode`. Range 1-10. Default 5 for new nodes.

```python
@dataclass
class ConceptNode:
    ...
    level: int = 5            # 1-10; 10 = highest priority, 1 = nearly forgotten
    archived: bool = False    # immune to decay below 1; set when level was deliberately raised
    archive_streak: int = 0   # number of consecutive deliberate accesses (toward archive threshold)
```

Why on the concept graph and not on each memory type:

- The graph already has the per-record metadata schema. Reusing it avoids fragmenting "level" across episodes.jsonl, mem0, learning_log, etc.
- Other memory types get a node-level reflection automatically (episodic memories already become `memory`-type nodes via `bootstrap_from_existing_memory`).
- Decay logic already runs in one place (consolidation tick).

### 2.2. Decay rules

Every concept node decays unless touched. Decay triggers from the existing periodic tick (already runs weekly via consolidation; the rewrite makes it run hourly with smaller increments):

```
Every hour:
  for each node:
    inactive_seconds = now - last_activated
    if inactive_seconds > threshold[node.level]:
      node.level -= 1
      node.last_activated = now  # reset timer
    if node.level <= 0:
      if node.archived:
        node.level = 1            # archived nodes can never decay below 1
      else:
        delete node                # gone forever
```

Decay thresholds (per current level — higher levels decay more slowly):

| Level | Inactive seconds before decay |
| --- | --- |
| 10 | 7 days |
| 9 | 5 days |
| 8 | 3 days |
| 7 | 2 days |
| 6 | 24 hours |
| 5 | 12 hours |
| 4 | 6 hours |
| 3 | 3 hours |
| 2 | 1 hour |
| 1 | 1 hour (then deleted) |

These are aggressive defaults — the user can tune via `state/memory_decay_tuning.json` if they want slower decay.

### 2.3. Promotions and demotions (judgment-based)

After each turn, run a small post-turn LLM check (we'll call it the **reflection scorer**). It examines the retrieved memories and the final reply, asking *"which of these memories were load-bearing for the reply?"*

Per memory:
- Score 0.0-1.0. Heuristic threshold: load-bearing if score ≥ 0.6.
- If load-bearing AND used correctly: `node.level += 1` (capped at 10).
- If load-bearing but contradicted by user (e.g., user said "no, it's actually X"): `node.level -= 1`.
- If retrieved but not load-bearing: no change.
- If load-bearing 3 times in a row: `node.archive_streak += 1`. At 3 streak hits, set `archived = True`.

Streak resets if the memory isn't retrieved on a turn at all. So archiving requires repeated, deliberate, useful access — not just one good query.

### 2.4. Retrieval logging (gather data first)

Step 4 of implementation gathers data without changing levels yet. The reflection scorer runs, logs to a new JSONL (`state/memory_reflection_log.jsonl`), but doesn't modify node levels. After 50-100 turns of data collection, we wire the level changes (step 5).

The reflection log schema:

```jsonl
{
  "ts": 1761838000.0,
  "turn_id": "turn_abc123",
  "person_id": "zeke",
  "user_text": "...",
  "reply_text": "...",
  "retrieved": [
    {"node_id": "mem_pizza_friday", "label": "...", "type": "memory", "level_before": 5},
    ...
  ],
  "scores": {
    "mem_pizza_friday": 0.81,    // load-bearing
    "topic_food": 0.12,           // retrieved but not used
    ...
  },
  "missing_useful": [             // optional — what would have helped if retrieved
    "memory of last Tuesday's conversation about cooking"
  ],
  "scorer_model": "ava-personal:latest",
  "scorer_ms": 421
}
```

This becomes training data for retrieval improvement later.

### 2.5. Archiving

Rule: a memory becomes archived after **3 consecutive deliberate accesses** where the reflection scorer rated it load-bearing.

Implementation:

```python
def update_node_post_turn(node_id, was_retrieved, was_load_bearing, was_contradicted):
    node = nodes[node_id]
    if was_load_bearing:
        node.level = min(10, node.level + 1)
        node.archive_streak += 1
        if node.archive_streak >= 3:
            node.archived = True
    elif was_contradicted:
        node.level = max(1, node.level - 1)
        node.archive_streak = 0
    elif was_retrieved:
        # retrieved but not used — neutral
        node.archive_streak = 0
    # not retrieved at all — no change
```

Archived nodes never decay below level 1. They effectively persist forever.

### 2.6. Gone-forever delete

Once a node hits level 0 and is **not** archived, it's deleted permanently. The `delete` removes it from `nodes`, drops all incident edges, and writes a tombstone to `state/memory_tombstones.jsonl` (for postmortem only — never restored from).

---

## 3. Implementation order — ship one step per commit

Each step compiles + runs without depending on later steps.

### Step 3 — Level tracking + decay (no behavior change)

- Add `level: int = 5` and `archive_streak: int = 0` to `ConceptNode`.
- Modify `_load()` to read these fields with defaults so existing JSON loads cleanly.
- Modify `_save()` to write them.
- Add `decay_levels(now=None)` method: walks nodes, decrements level on inactive nodes per the table in § 2.2, deletes level-0 unarchived nodes (writes tombstone).
- Add a periodic call from heartbeat / consolidation: every hour.
- **Don't** wire promotions yet — levels only go DOWN this commit. New turns don't change levels.
- Verify: existing concept_graph.json loads + saves round-trip with new fields. Decay tick runs once and reports counts.

### Step 4 — Reflection scoring (data gathering only)

- New module `brain/memory_reflection.py` exposing `score_retrieved_memories(retrieved, user_text, reply_text, scorer_model='ava-personal:latest')`.
- Hooked into `run_ava` post-finalize (after the reply is committed but before return). Runs in a background thread so the user doesn't pay latency for it.
- Writes to `state/memory_reflection_log.jsonl`.
- Don't modify node levels yet. Just gather data.
- Verify: after a few turns, the log has well-formed records with scores per retrieved node.

### Step 5 — Wire promotions/demotions

- Modify the reflection scorer's background thread: after writing the log, also apply level changes.
- Promotions cap at 10. Demotions floor at 1.
- Archive streak logic.
- Edge case: a turn that retrieves nothing produces no level changes.

### Step 6 — Implement archiving

- `archived = True` set when `archive_streak >= 3`.
- Decay walker checks `archived` flag and skips deletion when level reaches 0 (clamps to 1 instead).
- Add a `archived_at: float` field for audit.

### Step 7 — Gone-forever delete

- When level reaches 0 and not archived: pop from nodes, prune edges, write tombstone.
- Tombstone schema: `{ts, node_id, label, type, last_level, deleted_reason: "decay_floor"}`.

### Step 8 — UI surface (optional, deferred)

- Brain tab nodes display level as visual size (already does via weight; map level to size instead).
- New Memory tab section: "Recent decays", "Recent promotions", "Archived count".
- Not blocking; can ship the rewrite without UI.

---

## 4. Tool-creation autonomy — build-first

Per the user spec, when Ava encounters a need that no existing tool fills, she should build the tool herself rather than asking permission. Approval is required only for dangerous operations (file deletion, arbitrary network calls outside an allow-list, anything touching `ava_core/`).

Existing infrastructure that supports this:

- `tools/tool_registry.py` already has `register_tool(name, description, tier, handler)`.
- `tools/ava_built/` is the directory for tools Ava builds herself (already exists).
- Hot-reload watcher already polls `tools/` every 5s — new files are picked up without restart.

Implementation gap: a `propose_new_tool` notification path. We'll add it as part of step 7 or after — it's not blocking the level system. The notification just appends to `state/proposed_tools.jsonl` and the UI displays a non-blocking banner. The tool is already loaded and usable when the notification fires.

---

## 5. Risks and rollback

| Risk | Mitigation |
| --- | --- |
| Decay deletes important nodes the user cares about | Level 5 starting default + 7-day threshold at level 10 = a useful node won't decay if accessed weekly. Reflection scoring promotes useful retrieval into the protected band. |
| Reflection scorer adds latency to every turn | Background thread; doesn't block the reply. Writes to log only. If the scorer LLM is slow, scores arrive minutes after the turn — acceptable. |
| Reflection LLM hallucinates wrong scores | Step 4 gathers data without changing levels. We can audit ~100 turns before committing to the scoring decisions. |
| The new decay rate is too aggressive | All thresholds in `state/memory_decay_tuning.json`; user can multiply by N or disable per-level. Default config is shipped frozen. |
| Existing concept_graph.json doesn't load with new fields | `_load` defaults to `level=5, archive_streak=0` for missing fields. Round-trip tested. |
| Tombstone logs grow forever | Keep most recent 10 000 by default; rotate via leftover log-rotation pattern from learning_log. |

Rollback path: if step 3-7 introduces issues, revert in reverse order. Each step is one commit. The decay tick can be disabled at runtime via `AVA_DECAY_DISABLED=1` env var (added in step 3 as a safety valve).

---

## 6. What we're NOT doing in this rewrite

- **Touching mem0 / vector memory storage directly.** Those keep their internal storage. The level system lives on the concept graph, which already cross-references them. Mem0 internal scoring stays in mem0.
- **Changing how memories are written.** Every existing write path stays the same.
- **Forcing consolidation to use new logic immediately.** Step 3 runs alongside the existing weekly consolidation. We can phase out the old `decay_unused_nodes` call once the new system has 30+ days of production data.
- **Building a UI for the level changes upfront.** The data is queryable via `/api/v1/debug/full` immediately; UI is additive.
- **Restoring tombstoned memories.** Gone is gone. The tombstone log is for postmortem analysis, not undo.
