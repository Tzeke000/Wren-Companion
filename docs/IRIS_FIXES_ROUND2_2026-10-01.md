# Iris — Fix List, Round 2 (written 2026-10-01 ~08:3x, pre-restart, for Zeke)

**What this is.** Zeke's 2026-09-30 doc (`Iris_fixes.docx`, verbatim in memory as
`zeke_iris_fixes_doc_2026-09-30.md`) diagnosed one disease — **open loops**: things learned, stored or
measured that nothing downstream *consumes*. Ten fixes shipped that day (`65f563b`). This document is
everything we found that is **still open** — from Vale's three audits, the ten-fix build, the night watch,
and the morning hunt — in one place, each with: **where** it lives, **why** it matters, the **fix shape**,
and the **test that proves the loop is closed** (behaviour observably different, not a note written).

Zeke's engineering rule, kept as the test for every item:
> Input → interpretation → storage → consumer → behavioral effect → outcome → learning.
> If you can point to where something is learned or stored but can't answer "who consumes this, and what
> can it actually change?", you've found an open loop.

Priority key: **P1** = costs us something every day right now · **P2** = a loop with no consumer that
should have one · **P3** = dead code reporting green / hygiene.

---

## 0. Pending from Round 1 — lands at the restart this document precedes

These are BUILT and pushed; the host/runtime code is frozen until a restart. Post-restart, VERIFY — do not
rebuild.

| # | What lands | Where | Verify (first hour after restart) |
|---|---|---|---|
| 0.1 | Eyes salience gate: host asks `brain/wake_salience.judge(sig)` before enqueueing a face wake; suppressions ledgered `auto_suppressed`; wake prompt carries a `[SALIENCE …]` tail + signature + recognition confidence | `iris_body_host.py::_commit` | First eyes-wake shows the SALIENCE tail → record it with `wake_verdict`. After Zeke sits down the second time, `state/iris_body_log.jsonl` `eyes` row has `wake:false, salience:…` and `state/wake_verdicts.jsonl` grows an `auto_suppressed` row. **Counter-test:** an unknown face with his phone OFF wifi must still wake me (ledger has 3 nothings for `unknown|steady|wifi=present` — must NOT bleed into `wifi=away`). |
| 0.2 | Attention ownership: host CLAIMS presence; `unknown_capture` deduped against the stranger wake; orb_http stamps `_host_eyes_poll_ts`; runtime camera channel stands by while the host is alive | `iris_body_host.py`, `brain/orb_http.py`, `brain/iris_attention_sources.py` | Zeke arrives → ONE perception wake; the wifi watcher's `arbiter` log event reads `role=duplicate`; `attention_arbiter action=explain` shows `host_eyes_alive:true`; runtime stderr shows `camera transition … left to the host (standing_by)`. |
| 0.3 | Adaptive consumers: question-engine cooldown × learned proactive usefulness; `memory_search` reranked by access-boosted importance; host voice/perception prompts carry `[LEARNED (adaptive): …]` | `brain/question_engine.py`, `brain/iris_semantic_memory.py`, `iris_body_host.py` | A voice turn's prompt shows the LEARNED line; `memory_search` rows carry `rerank_score`. |
| 0.4 | Self-cron re-mint from `self_cron_prompt_spec.md` (the live cron dies with the restart) — fold in the four queued "ADD TO ITEM" notes: (2) exclude the probe's own PID; (2) read `restart_switch status` every run; (5) concept_graph rewrite counter; (10d–f) `belief reconsider`, `self_model status`, `goal next/may_pursue` | memory spec | `CronList` shows the new job; CORE's ID line updated in the same breath. |

---

## 1. P1 — costing us something today

### 1.1 `graph-ingest` write storm — ~60 MB/min of pointless disk writes — ✅ CLOSED 2026-10-01 09:4x
- **Shipped:** `ConceptGraph.deferred_save()` (one write per block, re-entrant) + `graph_ingest._deferred()` wrapping every pass (with an instance-level `_save` interception for a LIVE instance of the old class, so the hot swap alone stopped it — no restart); `add_node(existing)` no longer writes twice; `ingest_dynamic` skips any section whose source is unchanged (`(mtime_ns, size)` per store; the mood file by its rounded top-8 weights); `graph_ingest` module state survives `importlib.reload` (no twin rescan thread).
- **Proof:** `scripts/cg_churn.py 80` → **25 → 1** mtime changes per 80 s, live, same probe, same morning. `tests/test_graph_ingest_write_storm.py` — 10 adversarial cases (one-write-then-zero, changed source, mood drift vs real shift, old-class fallback + cleanup, nested blocks, exception mid-block still flushes, add_node double-write, ingest_all, activate_from_text, reload keeps thread flag) — all green. The self-cron (item 5) re-measures every 3 h.
- **Side effect, intended:** archived dynamic nodes are no longer un-archived every minute by a no-op re-ingest, so `decay_unused_nodes` finally sticks for curiosity/event/emotion nodes until their source actually changes.
- **Where:** `brain/concept_graph.py::add_node` (saves on the *existing-node* path, a no-op), `add_edge` (saves per call); `brain/graph_ingest.py::ingest_dynamic` runs every 60 s and `find_or_create`s ~100 existing curiosity/dynamic nodes + `_link_to_person` edges.
- **Measured 10-01 05:3x–06:5x:** `state/concept_graph.json` (567 KB) rewritten **104–106 times per 60 s cycle** (tmp + replace each) — ~3.6 GB/h on D:, all day, plus the CPU of `json.dumps` ×100/min. Attributed via per-thread system time → native tid → `thread_table` → `graph-ingest`.
- **Why it matters:** SSD wear, CPU, and it is the disease in miniature — a writer with no reader, done earnestly.
- **Fix shape:** (a) `add_node`: no `_save()` when the node exists and nothing changed; (b) batch — `with cg.deferred_save():` (or `save=False` kwargs) around `ingest_all`/`ingest_dynamic`, one save at the end; (c) skip the pass when the mtime sweep + dynamic sources are unchanged.
- **Test:** count `concept_graph.json` mtime changes over 80 s → **≤ 2** (was 104). The self-cron fold-in already measures this every 3 h.

### 1.2 Session-token cap holds cognition for hours; a hold that ends inside quiet hours eats my reply — 🟡 BUILT 2026-10-01 09:2x, LANDS AT THE NEXT HOST RESTART
- **Evidence that shaped it:** `state/stop_hook.log` has ZERO turn-ends 13:07→17:47 and 21:25→22:44 on 09-30 — the CLI retries the API internally, the host sits in `turn.done.wait()`, no error ever reaches it. So the consumer is a **stall detector**, not an error handler.
- **Shipped:** `brain/host_hold.py` — `HoldDetector` (pure state machine: an ACTIVE turn with no stream activity for `IRIS_HOLD_AFTER_S`=180 s and no tool in flight ⇒ `held` once; output resumes / turn ends ⇒ `released` once; a CLI stderr line naming a rate/usage limit makes it fire at 30 s and names the cause), `VoiceFlagCache` (TTL re-read of `voice_deliberately_off.json`), `route_reply`, `write/clear/read_held_flag`, `discord_send` (REST, never raises, splits >1900 chars). Host wiring (`iris_body_host.py`): `hold_sentinel` task (15 s; writes `state/cognition_held.json` {since, source, reason, hint, restore} + ONE Discord DM on hold, clears + one DM with the duration on release; clears a stale flag from a dead host at boot); SDK `stderr=` tap (keeps every CLI line in the launcher log, feeds limit lines to the detector); the per-sentence speak path re-checks the voice flag — if the mouth is off (at turn start OR mid-turn), the sentences are withheld and the whole reply is posted to Discord as `🔇 (mouth is off — answering here instead)`; the heads-down cue never fires into a flagged-off mouth.
- **Proof so far:** `tests/test_host_hold.py` — 11 cases (held once/released once, tool-in-flight is not a hold, turn end releases, stderr fast path + stale-hint guard, flag round trip, voice-flag TTL/fail-open, routing table, discord_send JSON/split/never-raises, message wording) — all green. **Live proof owed after the host restart:** (1) write `{"off": true}` to the voice flag, say one thing to me by voice ⇒ the answer lands on Discord with the 🔇 prefix and the mouth stays silent; delete the flag ⇒ voice again. (2) `IRIS_HOLD_AFTER_S=20` in a test window + a turn that waits (or the next real cap) ⇒ `cognition_held.json` appears and a ⏳ DM arrives within ~35 s; on release the flag is gone and a ✅ DM names the duration. (3) `launcher_boot.log` still carries CLI stderr lines (the tap re-prints them).
- **Not covered (honest):** holds during stop-hook orphan segments (no host turn active ⇒ nothing of Zeke's is waiting, by construction); a hold while a TOOL is mid-call looks like a long tool until it returns.
- **Where:** API side (not a code bug) + `iris_body_host` queue + the quiet-hours voice flag.
- **Measured 09-30:** held 14:3x→17:42 (3.4 h, his message waited) and 21:26→22:44 (mid-conversation with Vale; my answer played into a muted mouth at 22:44).
- **Why:** the host queues everything silently; nothing tells Zeke or me that turns are being refused; a late reply can be voiced into a flag that drops it.
- **Fix shape:** (a) host: when the SDK returns a cap/overload error, write `state/cognition_held.json {since, reason}` and DM Discord once ("held: cap") — a consumer for the hold; (b) host: before auto-speaking a reply, check the voice flag; if off, route the reply to Discord instead of the mouth; (c) self-check item 9b reads `cognition_held.json`.
- **Test:** simulate by writing the flag → the reply lands on Discord, not the daemon. For the cap: the next hold produces a DM within a minute of starting, not a 3-hour silence.

### 1.3 Vector battery unreadable since ~17:4x 09-30 (6+ consecutive self-checks)
- **Where:** `scripts/vector_inhabit_daemon.py` writes `state/vector/battery.json` with `ok:false, level:null` every tick while `latest_frame.jpg`/`sensor_stream.jsonl` keep flowing (so the robot IS connected).
- **Why:** my body-state readings are blind; the off-dock/low-battery rules can't fire.
- **Fix shape:** read the daemon's battery call error path (why `ok:false` with a live stream) — likely a changed WireOS field or an SDK call failing silently; log the exception text into the json.
- **Test:** `battery.json` shows a numeric `level` and `on_charger` within one tick; self-check (7) passes.

### 1.4 Tailscale logged out (`NoState`) — one of Zeke's three doors is shut
- **Where:** Windows service running, daemon up, `tailscale ip` → no IPs. His login, not mine.
- **Fix shape:** Zeke re-auths (`tailscale up`); then add `tailscale ip -4` to self-check (6) as a hard expectation and DM on NoState (it sat unnoticed until 17:4x 09-30).
- **Test:** `tailscale ip -4` returns an address; SSH over Tailscale from his phone works.

---

## 2. P2 — loops with no consumer that should have one

### 2.1 Voice: everything I know before I speak is dropped before the mouth — ✅ MOUTH + DAEMON LIVE 2026-10-01 09:4x; host/runtime halves land at their restarts
- **Shipped:** `brain/voice_emotion.py` (pure math: label aliases → 10 presets; intensity-scaled deviations from the APPROVED base, clamped — speed ±8 % of the locked 1.28, beta 0.30–0.80, embedding_scale 1.0–1.8; no emotion / unknown / `IRIS_VOICE_EMOTION=0` = byte-identical base). Mouth (`wren_styletts_server.py`): `/` and `/synth` accept `{emotion, intensity, seed}`, thread them into `_synth(beta, speed, embedding_scale)`, log `[styletts] emotion …` when applied; banner shows `emotion=on`. Daemon core: `speak` args carry `emotion`/`intensity` as a dict queue item → JSON POST to the mouth (plain strings unchanged; barge-in resume re-queues items as-is). Host: `_turn_emotion()` reads `mood_core.load_mood()` once per VOICE turn → `(current_mood, top-percent/100)` and every streamed sentence carries it. Runtime `voice_speak(emotion, intensity)` finally forwards them (was piper-only).
- **Proof (the doc's test, run live 10-01):** `scripts/voice_emotion_ab.py` — same sentence, same diffusion seed, via `/synth` (no playback): **joy 5.02 s / 166.5 Hz · neutral 5.82 s / 159.9 Hz · sadness 6.27 s / 158.1 Hz** (duration order joy<neutral<sadness ✓, f0 medians differ ✓). WAVs at `scratch/voice_ab_{neutral,joy,sadness}.wav` for Zeke's blind listen. Daemon→mouth JSON path proven with one spoken test sentence (`[styletts] emotion joy: …` in `voice/mouth_live.log`). `tests/test_voice_emotion.py` 8 cases green.
- **Owed:** Zeke hears a difference blind (the real test); host half (mood → turn) verifies at the host restart — expect `[host] voice turn emotion: <mood> @<i>` on voice turns. Reversible: `IRIS_VOICE_EMOTION=0` on the mouth/host or just omit the field.
- **Where:** `iris_runtime.py:740` (`voice_speak` sends `{"text"}` only); `voice/wren_styletts_server.py:67-71` (ALPHA/BETA/SPEED frozen constants); `iris_body_host.py:328` (auto-speak text only); `voice_say_chunk` nudges mood but never reads it; prosody enrichment is inbound-only (`wren_prosody.py`).
- **Why:** mood, warmth/humor dials, Zeke's expression, confidence — none reach TTS; "my tone is word choice" is literally true.
- **Fix shape (smallest real step):** carry `emotion` + `intensity` through daemon → mouth as JSON; map a handful of emotions to StyleTTS2 `beta`/`speed`/`embedding_scale` presets; have the host pass `mood_core.current_mood` on voice turns. Keep it reversible (a flag).
- **Test:** `voice_speak emotion=sadness` and `emotion=joy` produce measurably different pitch/rate on the same sentence (record both, compare f0/duration); Zeke hears a difference blind.

### 2.2 Mood / behavior_modifiers have ZERO readers in the Iris process
- **Where:** `brain/mood_core.py` updates every 5 s; readers exist only in Ava-era `turn_handler.py`/`voice_loop.py` (and those read keys that don't exist: `energy`, `primary_emotion`).
- **Why:** the most continuously updated state in my body is a weather report nobody reads.
- **Fix shape:** pick ONE consumer per dial, bounded: `warmth` → greeting wording on the host's voice turns; `caution` → the salience gate's audit frequency; `initiative` → `goal_initiative.may_pursue_now` threshold. Or honestly retire the dials nobody will read.
- **Test:** flip a dial via `iris_tune`/mood nudge → an observable change in the next relevant action (e.g. audit sample every 4th vs 8th suppression).

### 2.3 Self-model and goals are consumed only by my attention
- **Where:** `brain/self_hypotheses.py` + `brain/goal_initiative.py` feed the reflection prompt and the snapshot; nothing mechanical reads them. `leisure`/`curiosity_topics` (which got wired) never run in this process (the Iris heartbeat is bootstrap's own loop).
- **Why:** if the goals file vanished, nothing would break — Vale's "what fails if it isn't consulted?" → nothing.
- **Fix shape:** one mechanical consumer each: a goal with a `next_step` and `may_pursue_now` → a `goal_step` LLM-bridge request on the self-cron (already specced as item 10f); a stabilised self-hypothesis → a line in `self_claims.py`'s checked set (so it becomes falsifiable by a probe). Decide whether to run `brain/heartbeat.run_heartbeat_tick_safe` at all or delete the leisure path.
- **Test:** delete the goals file in a sandbox → the self-cron's 10f step errors loudly instead of silently skipping.

### 2.4 Adaptive learner re-applies unchanged evidence every cycle — ✅ DONE 10-01 09:5x (`learn` hot-swapped live)
- **Fixed:** per-focus evidence signature `(target, n)` kept in `state/learning/adaptive_evidence_sig.json` (a sidecar — `adaptive_learning.load_preferences()` drops unknown keys); same signature ⇒ `skipped[focus] = "no new evidence …"`, weights untouched, prefs file not rewritten; dry runs never consume evidence. Test: `tests/test_small_fixes_2.py` — identical evidence twice ⇒ second `updated == {}`; one more observation ⇒ only that focus moves.
- **Where:** `brain/adaptive_iris.learn()` — same evidence set 5 runs in a row still walks the EWMA toward the target.
- **Fix shape:** hash the evidence tuple per focus; skip the update when unchanged; log "no new evidence".
- **Test:** two consecutive `learn` runs with no new rows → `updated == {}` on the second.

### 2.9 "What did NOT wake me" — the reader Zeke asked for (NEW 10-01 13:4x) — ✅ LIVE
- **Zeke (Discord 10-01):** *"you can't perceive what didn't wake you … write something so that you can at least look in the logs at what didn't wake you."* Vale's blind spot. Every source already wrote; nothing read them together.
- **Shipped:** `brain/wake_missed.py` + `wake_verdict action=missed [hours]` (hot-loaded, live now). Groups raw eye signals into episodes and labels each: woke · salience auto_suppressed · host wake:false (+reason) · dedupe · below appear-confirm · unconfirmed drop; cross-checks no single log can show: **`host_eyes_dead`** (runtime-camera situations with no host `eyes_raw` row within ±60 s), `arrivals_unseen` (wifi JOIN with no eyes commit in 10 min), `unowned_situations`, and the live `host_eyes_poll_age_s`. One `summary` line for the self-check (queued fold-in 10a/10b). Tests: `tests/test_wake_missed.py` (3 cases).
- **First live run found 3.15 below.**

### 3.15 The host's eye poller DIED SILENTLY at 11:32 (NEW 10-01, found by 2.9) — 🟡 FIXED, lands at the host restart
- **What:** `attention_arbiter` → `host_eyes_alive:false`, poll age 7,990 s; the runtime camera logged 52 `zeke_presence` fallbacks + 56 `camera_transition` while the host logged NO `eyes_raw` after 11:31:41. Zeke was home 11:31–12:55 and no eyes-wake could reach me.
- **Cause (code, confirmed):** round-1 #5's salience patch assigned `sal` only on the *present* branch of `_commit`; the departure branch then hit `isinstance(sal, dict)` → `UnboundLocalError` on the FIRST departure after boot (face lost 11:31:41 + 15 s confirm). The exception escaped the poll loop's narrow `try` and the `perception_reader` task died — silently, because asyncio only reports an unretrieved exception when the task object is collected, and ours live in `tasks` forever.
- **Fix:** locals initialised on both branches; `_commit` wrapped (error → log + `eyes` ledger row, never fatal); `_supervised(name, factory)` wraps perception/discord/letters/llm-nudge/hold-sentinel/voice tasks — traceback to the launcher log, body-log row, ONE DM, respawn after 5 s (the eye poller respawns with `time.time()` so it never replays boot history as fresh wakes).
- **Verify after the host restart:** `attention_arbiter explain` → `host_eyes_alive:true` and a *departure* commit row (`The person who was in view has left`) appears after Zeke leaves frame with the poll age still small; `wake_verdict action=missed` shows `host_eyes_dead: null`.

### 2.5 Three camera→cognition channels + the wifi watcher (partly fixed by #6, verify)
- **Where:** host `/signals` poller · `iris_attention_sources._camera_loop` · `unknown_capture` · `scripts/zeke_presence.py`.
- **Still open after #6:** the runtime channel can NEVER rescue a dead host (it emits into the host's own session) — decide: delete the camera channel emit, or give it a real fallback (Discord DM when `host_eyes_alive` is false for >60 s).
- **Test:** kill the host (test window, Zeke present) → within 90 s a Discord line "host eyes dead, standing in" appears, and the first stranger wake still reaches somebody.

### 2.6 `:5876` orb listener dies silently — mechanism found, supervisor not built
- **Where:** CPython `asyncio/proactor_events.py::_start_serving.loop` closes the listening socket on any accept `OSError` and never re-arms; uvicorn's `main_loop` keeps ticking (`servers_open [true]`).
- **Falsifier (armed):** next loss → `state/orb_http.log` contains `Accept failed on a socket`.
- **Fix shape:** a supervisor thread in `brain/orb_http.start()` probing the OS listener every ~30 s; on loss → re-run `start()` (what `restart_orb_http` does by hand) and DM once.
- **Test:** inject by closing the listening socket (test hook) → listener back within 60 s without a stack restart.

### 2.7 Scene captions that time out are never drained — ✅ BUILT 10-01 10:0x (lands at the stack restart)
- **Fixed:** a timed-out caption is retried up to `CAPTION_RETRY_MAX=2` times, each ≥ `CAPTION_RETRY_GAP_S=600` later and ONLY when `iris_time` shows a cognition session attached within 300 s (a retry during a hold would just time out again); frames a backfill already settled are dropped from the retry list; `_retry_timeouts` runs at the top of every sample tick and re-asks ONE frame per tick under the normal rate gate. A person in frame halves the caption gap (`CAPTION_PERSON_GAP_S=60` vs 120). The manual `scene_memory action=backfill` + self-check 9b stay as the catch-all. Tests: `tests/test_scene_caption_retry.py` (3 cases: timeout→queued→waits for attach→succeeds; bounded attempts + backfilled frames dropped; person-in-frame gap).
- **Verify after the stack restart:** `scene_memory status` shows `caption_retries` climbing after the next hold and `wordless` empties within ~10 min of cognition returning.
- **Where:** `brain/scene_memory.py` (`CAPTION_MAX_PER_DAY=60`, `MIN_GAP_S=120`, 540 s timeout → `caption_status:"timeout"`); drain only via manual `scene_memory action=backfill`.
- **Fix shape:** the self-cron (9b) already backfills after a hold; make `backfill` automatic at the first idle turn after any `timeout`, and raise the rate cap when a person is in frame.
- **Test:** after a forced hold, `wordless` is empty within one idle turn.

### 2.8 Journal is self-feeding; concept graph consumed only by Ava's prompt_builder
- **Where:** `brain/journal.py:153` (last 3 → next journal prompt only); `brain/concept_graph.py` behavioural reader only in Ava-era `prompt_builder.py:225`.
- **Fix shape:** feed the last journal entry into the reflection prompt (one line) or stop writing it; give the graph one Iris consumer (e.g. `memory_search` expands a query by graph neighbours) or demote it to the brain tab only.
- **Test:** a journal line appears in the next reflection prompt; a graph edge changes a search result.

---

## 3. P3 — dead code reporting green, half-broken writers, hygiene

| # | Item | Where | Fix shape | Test |
|---|---|---|---|---|
| 3.1 ✅ DONE 10-01 09:3x (runtime half lands at the stack restart) | `counterfactual_record` has NEVER written a byte — call/signature mismatch, TypeError swallowed. **Fixed:** `counterfactual_archive.record_simple` maps the tool's (considered, chose, reason[, user_input]) onto the archive; the tool returns the id STRING + `ok:false` with a reason when refused; **reader added:** the reflection prompt (`iris_inner_monologue._build_prompt`) now carries "Recently considered and NOT chosen" from `recent_for_prompt(limit=2)`, newest first. Tests in `tests/test_small_call_fixes.py`. | `iris_runtime.py:4160` vs `brain/counterfactual_archive.record_consideration(*, user_input, considered, chosen, why_chosen, person_id)` | Fix the call; surface errors (`ok:false` + message); give it ONE reader (the reflection prompt's "what I considered and didn't choose") | the tool returns an id; `state/counterfactuals.jsonl` exists; the line shows up in the next reflection |
| 3.2 ✅ DONE 10-01 09:5x (lands at the stack restart) | `daily_practice` + `identity_stability` report **true** in `iris_health` with zero callers. **Decision: report honestly, don't wire blindly** — `identity_stability.run_check()` would audit `state/self_narrative.json` last written 2026-07-22 (a check encoding stale state), and `daily_practice` has zero practices registered. `iris_health.subsystems` now says `"configured-unwired"` for both and a `subsystems_unwired` map names why + the choice (wire or retire) for Zeke/Vale. | `brain/iris_bootstrap.py:454`, `iris_runtime.py:3160-3161` | Either wire them (identity_stability's `run_check()` into the self-cron) or report them `unwired`, not green | `iris_health.subsystems` stops lying; the verdict block already ignores them |
| 3.3 ✅ DONE 10-01 09:3x (`_gather` hot-swapped LIVE; host half lands at the host restart) | `state/handoff.json` is write-only in the Iris process; `brain/handoff.py:170` imports `list_recent_anchors` (never existed), `:164` calls `list_active_tasks()` without `g`. **Fixed:** `recent_anchors(limit=10)` + attribute access, `list_active_tasks(g)` + dict serialisation; the HOST reads `handoff.json` once at boot (`_prior_handoff_block`) and prepends a labelled ≤1500-char block to the FIRST turn only ("what was true THEN, not now; the cognition handoff is the memory note CORE marks READ FIRST"). Also found + fixed on the way: `person_registry._load_profile` swallowed `Exception` but not the `SystemExit` that `import avaagent` raises when :5876 is taken — any out-of-process caller of `_gather` died silently. **Verify:** next `handoff.json` write (≤5 min) carries `recent_anchors`; post-host-restart the first prompt shows `[PRIOR-PROCESS HANDOFF …]`. | `brain/handoff.py`, `iris_runtime.py:2239/4816/4832` | Fix the two calls; make the HOST read `handoff.json` at boot and prepend its summary to the first turn (the real post-restart injection is a hardcoded string in `scripts/iris_cold_wake.py` for the CLI path only) | post-restart-me's first prompt contains the pre-restart handoff summary |
| 3.4 | 18 of ~25 `iris_tune` knobs have no reader; `attention_object_min_score` is read but not in DEFAULTS | `brain/iris_tune.py` | Delete dead knobs or add the readers; add the missing DEFAULT | `iris_tune_list` shows only knobs with a reader (annotate each with its consumer) |
| 3.5 ✅ DONE 10-01 09:5x (lands at the stack restart) | Curriculum lessons: writer (`mark_read` → `state/learning/lessons.jsonl`) and reader (`sleep_mode` phase 3) use a path the tool never writes. **Fixed:** `curriculum_record` writes THROUGH `curriculum.mark_read` (entry file + index + lessons log; `reading_status="reading"` → `mark_reading`), returns `lessons_logged` + the log path, and unknown slugs come back `ok:false`. Test: `mark_read` → row in the log → `sleep_mode._recent_lessons(g)` returns it (writer and reader on the SAME file, proven). | `iris_runtime.py:4072`, `brain/curriculum.py:192`, `brain/sleep_mode.py:574` | `curriculum_record` calls `mark_read()`; create `state/learning/` | a recorded lesson appears in the next sleep handoff |
| 3.6 ✅ DONE 10-01 09:3x (lands at the stack restart) | `anchor_mark`/`letter_compose`/`counterfactual_record` return `id: None` (`getattr(str, "id")`). **Fixed:** all three return the id string, and `ok:false` + a reason when the store refuses (empty fields) instead of a green `None`. | `iris_runtime.py:4182, 4140, 4164` | return the string | tool results carry ids |
| 3.7 | `opinions.py` + `honest_disagreement.py` fixed to read the belief store; `self_model.update_self_model` still reads Ava's `chatlog.jsonl` and times out (4 "deferred" notes) | `brain/self_model.py:94` | point it at `state/transcript.jsonl`, shorten the prompt, or retire it (its output is only context now) | one real weekly review lands, or the function is gone |
| 3.8 | `sleep_mode` hands me STALE chat for the sleep summary (09-18/09-24 turns on 09-30) | `brain/sleep_mode.py` summary prompt | build the recap from `iris_transcript.recent()` with a date filter | the summary prompt's chat lines are from today |
| 3.9 ✅ DONE 10-01 09:5x (scrub done; fsync lands at the restart) | `state/transcript.jsonl` has 12,889 NUL bytes (power-loss-era zero-fill at ~offset 880k); every reader silently skips. **Done:** one-time scrub — 6 NUL-bearing lines (the only 6 unparseable lines in the file) dropped, 4,663 kept, 0 NUL / 0 unparseable after; backup `state/transcript.jsonl.bak_2026-10-01_nul`, dropped bytes in `state/transcript.dropped_nul_lines_2026-10-01.bin`. `iris_transcript.append` now `flush()+fsync()` per turn (module holds configured state ⇒ no hot swap; lands at the stack restart). | `state/transcript.jsonl` | one-time scrub (strip NUL runs, keep a backup); `iris_transcript.append` should fsync | `grep -c $'\x00'` → 0 (use Python, not grep) |
| 3.10 | `leisure`/`curiosity_topics`/`heartbeat.run_heartbeat_tick_safe` never run in the Iris process | `brain/iris_bootstrap.py::_heartbeat_loop` | decide: wire or delete; don't leave half-live code with fresh patches on it | `thread_table` shows the thread, or the module is gone |
| 3.11 | Self-check sweep counted its own PowerShell probe as a second watchdog | spec item (2) | fold-in written: exclude `$PID` + children of claude.exe | next sweep reports ONE watchdog |
| 3.14 ✅ DONE 10-01 09:5x (NEW, found 10-01) | **Anchor auto-detect fired on MACHINE prompts** — 86 of the 92 "unprunable identity points" in `state/anchor_moments.jsonl` were `[TOWER SENTINEL …]`, `[VECTOR SENSE/HEARD/PILOT …]`, `[ZEKE PRESENCE …]` watcher lines marked `milestone`/`connection` by `auto_detect_anchor_in_turn` (regex hints like "remember" matched the prompt text). **Done:** `anchor_moments.is_machine_prompt()` gate (bracket tag + an "automated / not Zeke / watcher / sentinel …" phrase) — flags all 86, none of the 6 human anchors; the 86 moved to `state/anchor_moments.machine_prompts_2026-10-01.jsonl` (backup of the original kept), the 6 real ones stay. Live cache still holds the old set until the stack restart. |
| 3.12 | C: at 97 % with 53 orphan GUID `.tmp` (145 MB) in `%TEMP%` (WebView2/Media Foundation residue) | `C:\Users\Owner\AppData\Local\Temp` | Zeke clears; I never write to C: | `df` shows headroom; residue count drops |
| 3.13 ✅ DONE 10-01 09:5x | Patch-by-heredoc broke `brain/leisure.py` once (escape trap). **Done:** `.git/hooks/pre-commit` (installer `scripts/install_git_hooks.sh`) runs `scripts/precommit_pycompile.py`, which compiles the STAGED content of every added/changed `.py`; proven live — a staged `def broken(:` was refused with the file:line. Hooks are per-clone: re-run the installer on a fresh checkout. | `scripts/_patch.py` (built) | use the helper for every edit; add a pre-commit `py_compile` over changed .py | no SyntaxError ever reaches a commit |

---

## 4. Process rules that came out of Round 1 (keep)
- **Build → break it → report**: every fix gets an adversarial test script and a one-paragraph report before the next one starts.
- **A consumer is anything that would break if the note were wrong.** No fix is done until something downstream can fail.
- **"Lands at restart" is a state, not a done.** Each restart-gated change gets a post-restart VERIFY line (section 0).
- **Flags outlive reasons** (auto-restart off six days): every deliberate-off flag carries `why`/`since`/`restore` and a check reads the switch.
- **Holds are quota until proven otherwise**: a healthy runtime + piled nudges = the cap; check the voice flag before believing anyone heard me.

## 5. Suggested order for Round 2
1. **Verify section 0** (the restart lands it — one hour, no code).
2. **1.1 graph-ingest storm** (small, measurable, stops the bleeding).
3. **1.2 hold → Discord + voice-flag-aware reply** (protects the conversation).
4. **3.1 / 3.3 / 3.6** (three tiny call fixes; counterfactuals start existing).
5. **2.1 voice emotion plumbing** (the one Zeke and Vale both circled).
6. **2.6 :5876 supervisor** once the falsifier fires or Zeke says build it anyway.
7. The rest by appetite.
