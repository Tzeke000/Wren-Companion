# Little-brain grading loop — capture record schema v0 (DRAFT for Zeke's markup)

**Status: DRAFT 2026-07-27 — nothing wired yet.** Per the rulings (2026-07-27): schema settles first, because every decision here is baked into every record ever written. Spec sources: `memory/little_brain_grading_loop_spec_2026-07-25.md` + `memory/little_brain_grading_rulings_2026-07-27.md`.

## 1. Turn record — one JSON line per little-brain turn

Written **at turn time** by the little-brain loop (vector_brain_server chat path + little_pilot), appended to `state/little_brain/turn_log/YYYY-MM-DD.jsonl` (daily files so old days compress/archive cleanly).

```json
{
  "schema_v": 0,
  "turn_id": "lb_20260727T164512_3f2a",
  "ts": 1785184712.345,
  "model": "iris-little-v12",
  "source": "orb_tab",
  "stimulus": { "raw_stt": null, "clean": "what's your voltage right now" },
  "tools": [
    { "name": "senses_now", "args": {}, "ok": true, "t_ms": 41,
      "result_excerpt": "battery_v=4.09 charging=true ..." }
  ],
  "response": "4.09 volts, on the charger.",
  "latency_ms": { "total": 1830 },
  "senses": {
    "snapshot": { "...": "verbatim copy of senses_live.json at stimulus time" },
    "freshness": { "age_ms": 66, "verdict": "live" }
  },
  "escalated": false,
  "flags": ["none"]
}
```

Field notes:
- **`turn_id`** — sortable timestamp + short random suffix. The watermark and every grade/corpus item reference it (provenance ruling).
- **`source`** — `voice | orb_tab | pilot | apprentice | test`. Lanes graded separately where it matters.
- **`stimulus`** — BOTH raw STT and cleaned text (spec §1); `raw_stt` null for typed lanes.
- **`tools`** — full sequence in order, args verbatim, `result_excerpt` capped (~500 chars) so records stay greppable; full results already live in the tool logs if ever needed.
- **`senses.snapshot`** — verbatim `senses_live.json` copy **at stimulus time** (not response time — grounding is judged against what she could have read). `freshness.verdict`: `live | stale | fossil_suspect` (identical-floats structural check, computed at capture).
- **`escalated`** — derived (any `ask_big_iris` in tools), lifted to a top field because escalation is **rubric axis one from day one**.
- **`flags`** — suspicion markers computed at capture: `tool_error | tool_retry | near_latency_budget | refusal | fossil_flag | long_output | lane_mismatch | hop_limit`. Populates the suspicion queue for free.

## 2. Blind sample — deterministic, not RNG

Blind stream = `sha1(turn_id) mod 1000 < N` (N=50 → 5%, tunable **at grading time**). No capture-time coin flip: selection is reproducible, auditable, and re-computable at any rate against historical logs. Suspicion and blind streams reported separately forever (spec §2).

## 3. Watermark — Zeke's required addition

`state/little_brain/grading_watermark.json`:
```json
{ "last_graded_turn_id": "lb_...", "batch_id": "grade_20260728_0300",
  "rubric_version": "r0", "updated_iso": "..." }
```
Each bounded grading session: read watermark → grade forward → write watermark ATOMICALLY (temp+rename) as its last act. Crash mid-batch ⇒ re-grade that batch (idempotent: grades keyed by turn_id, re-grade overwrites) — never skip.

## 4. Grade record — appended to `state/little_brain/suggestion_grades.jsonl`

```json
{
  "schema_v": 0, "turn_id": "lb_...", "batch_id": "grade_...",
  "rubric_version": "r0", "grader": "big_iris",
  "stream": "suspicion | blind",
  "path": "right_tool | wrong_tool | missed_tool | needless_tool",
  "grounding": { "verdict": "pass | fail | n/a",
                 "method": "range_over_window",
                 "fields": { "battery_v": {"reported": 4.09, "range": [4.08, 4.10], "window_s": 2.0, "pass": true } } },
  "escalation": "correct_hold | under | over | correct_handoff",
  "voice": "in_lane | drifted",
  "verdict": "agree | minor | disagree",
  "should_have": "…only when verdict != agree…",
  "commentary": "…free-form, NEVER promoted to trainable fields…",
  "human": null
}
```
- **Grounding = range-over-window** (his refinement): pass iff reported value fell inside the range the sensor actually occupied across the tolerance window. Per-field windows in `state/little_brain/grading_tolerance.json` (voltage tight, head-angle wide). **Fossil detection stays structural** — a `fossil_suspect` freshness verdict fails regardless of numeric match; the window can never launder a frozen sensor.
- **`human`** — filled when the ~5% Zeke-routed slice comes back: `{ "verdict": ..., "agrees_with_big_iris": true }`. Big-Iris/Zeke agreement tracked as its own metric.
- **`escalation: correct_handoff`** exists so "a fast honest handoff scores as well as a fast answer" is a first-class passing grade, not a footnote.

## 5. Deliberately NOT in v0
- No idle-window scheduler (bolt-on later, per ruling 2).
- No corpus-promotion records (own schema when a failure mode first hits N).
- No capture of big-Iris turns — cerebellum only.

## Open for markup
1. `result_excerpt` cap at 500 chars — enough?
2. Latency: total-only for now; per-tool `t_ms` already captured. Need first-token too?
3. Daily-file rotation vs one growing jsonl.
4. Blind rate default 5%?
