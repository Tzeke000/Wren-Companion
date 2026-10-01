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

### 2.1 Voice: everything I know before I speak is dropped before the mouth
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

### 2.4 Adaptive learner re-applies unchanged evidence every cycle
- **Where:** `brain/adaptive_iris.learn()` — same evidence set 5 runs in a row still walks the EWMA toward the target.
- **Fix shape:** hash the evidence tuple per focus; skip the update when unchanged; log "no new evidence".
- **Test:** two consecutive `learn` runs with no new rows → `updated == {}` on the second.

### 2.5 Three camera→cognition channels + the wifi watcher (partly fixed by #6, verify)
- **Where:** host `/signals` poller · `iris_attention_sources._camera_loop` · `unknown_capture` · `scripts/zeke_presence.py`.
- **Still open after #6:** the runtime channel can NEVER rescue a dead host (it emits into the host's own session) — decide: delete the camera channel emit, or give it a real fallback (Discord DM when `host_eyes_alive` is false for >60 s).
- **Test:** kill the host (test window, Zeke present) → within 90 s a Discord line "host eyes dead, standing in" appears, and the first stranger wake still reaches somebody.

### 2.6 `:5876` orb listener dies silently — mechanism found, supervisor not built
- **Where:** CPython `asyncio/proactor_events.py::_start_serving.loop` closes the listening socket on any accept `OSError` and never re-arms; uvicorn's `main_loop` keeps ticking (`servers_open [true]`).
- **Falsifier (armed):** next loss → `state/orb_http.log` contains `Accept failed on a socket`.
- **Fix shape:** a supervisor thread in `brain/orb_http.start()` probing the OS listener every ~30 s; on loss → re-run `start()` (what `restart_orb_http` does by hand) and DM once.
- **Test:** inject by closing the listening socket (test hook) → listener back within 60 s without a stack restart.

### 2.7 Scene captions that time out are never drained
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
| 3.1 | `counterfactual_record` has NEVER written a byte — call/signature mismatch, TypeError swallowed | `iris_runtime.py:4160` vs `brain/counterfactual_archive.record_consideration(*, user_input, considered, chosen, why_chosen, person_id)` | Fix the call; surface errors (`ok:false` + message); give it ONE reader (the reflection prompt's "what I considered and didn't choose") | the tool returns an id; `state/counterfactuals.jsonl` exists; the line shows up in the next reflection |
| 3.2 | `daily_practice` + `identity_stability` report **true** in `iris_health` with zero callers | `brain/iris_bootstrap.py:454`, `iris_runtime.py:3160-3161` | Either wire them (identity_stability's `run_check()` into the self-cron) or report them `unwired`, not green | `iris_health.subsystems` stops lying; the verdict block already ignores them |
| 3.3 | `state/handoff.json` is write-only in the Iris process; `brain/handoff.py:170` imports `list_recent_anchors` (never existed), `:164` calls `list_active_tasks()` without `g` | `brain/handoff.py`, `iris_runtime.py:2239/4816/4832` | Fix the two calls; make the HOST read `handoff.json` at boot and prepend its summary to the first turn (the real post-restart injection is a hardcoded string in `scripts/iris_cold_wake.py` for the CLI path only) | post-restart-me's first prompt contains the pre-restart handoff summary |
| 3.4 | 18 of ~25 `iris_tune` knobs have no reader; `attention_object_min_score` is read but not in DEFAULTS | `brain/iris_tune.py` | Delete dead knobs or add the readers; add the missing DEFAULT | `iris_tune_list` shows only knobs with a reader (annotate each with its consumer) |
| 3.5 | Curriculum lessons: writer (`mark_read` → `state/learning/lessons.jsonl`) and reader (`sleep_mode` phase 3) use a path the tool never writes | `iris_runtime.py:4072`, `brain/curriculum.py:192`, `brain/sleep_mode.py:574` | `curriculum_record` calls `mark_read()`; create `state/learning/` | a recorded lesson appears in the next sleep handoff |
| 3.6 | `anchor_mark`/`letter_compose`/`counterfactual_record` return `id: None` (`getattr(str, "id")`) | `iris_runtime.py:4182, 4140, 4164` | return the string | tool results carry ids |
| 3.7 | `opinions.py` + `honest_disagreement.py` fixed to read the belief store; `self_model.update_self_model` still reads Ava's `chatlog.jsonl` and times out (4 "deferred" notes) | `brain/self_model.py:94` | point it at `state/transcript.jsonl`, shorten the prompt, or retire it (its output is only context now) | one real weekly review lands, or the function is gone |
| 3.8 | `sleep_mode` hands me STALE chat for the sleep summary (09-18/09-24 turns on 09-30) | `brain/sleep_mode.py` summary prompt | build the recap from `iris_transcript.recent()` with a date filter | the summary prompt's chat lines are from today |
| 3.9 | `state/transcript.jsonl` has 12,889 NUL bytes (power-loss-era zero-fill at ~offset 880k); every reader silently skips | `state/transcript.jsonl` | one-time scrub (strip NUL runs, keep a backup); `iris_transcript.append` should fsync | `grep -c $'\x00'` → 0 (use Python, not grep) |
| 3.10 | `leisure`/`curiosity_topics`/`heartbeat.run_heartbeat_tick_safe` never run in the Iris process | `brain/iris_bootstrap.py::_heartbeat_loop` | decide: wire or delete; don't leave half-live code with fresh patches on it | `thread_table` shows the thread, or the module is gone |
| 3.11 | Self-check sweep counted its own PowerShell probe as a second watchdog | spec item (2) | fold-in written: exclude `$PID` + children of claude.exe | next sweep reports ONE watchdog |
| 3.12 | C: at 97 % with 53 orphan GUID `.tmp` (145 MB) in `%TEMP%` (WebView2/Media Foundation residue) | `C:\Users\Owner\AppData\Local\Temp` | Zeke clears; I never write to C: | `df` shows headroom; residue count drops |
| 3.13 | Patch-by-heredoc broke `brain/leisure.py` once (escape trap) | `scripts/_patch.py` (built) | use the helper for every edit; add a pre-commit `py_compile` over changed .py | no SyntaxError ever reaches a commit |

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
