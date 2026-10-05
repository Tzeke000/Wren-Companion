# Research integration — 2026-10-05

Zeke's go-ahead (Discord, 10-05 morning): *"see what you can do with these. Research what you need… make it better for your needs… Make a plan and execute it."* "These" = two docs he and Vale wrote:
- **"Iris: AI Research & Integration Handoff"** (10-04): Letta, Reflexion, Cosmos, WoW, LeRobot, GR00T; "verification first", "PTZ prediction is a good first candidate".
- **"Research Landscape & 'Organ Donor' Guide"** (09-30): CTEM, LIDA, Soar, Voyager, Gödel/DGM, Hyperon. Its working rule: *a state isn't integrated until a downstream consumer changes behaviour observably.* §7 is a 9-step borrowing protocol.

Full text of both: memory note `vale_research_handoff_2026-10-04.md`.

**How it was done:**
1. A code audit of what already exists, so nothing got rebuilt.
2. A source-level research pass. Reflexion, Voyager and Letta were read from their GitHub source. CTEM was read from its paper (arXiv 2605.15812). The PTZ maths was checked against a live frame.
3. Each borrowed piece names its observed failure, its consumer and its evidence.

## 1. `ptz_predict` — predicted vs observed for the PTZ head (Cosmos/WoW, shrunk)

**Failure it targets.** On 10-02 the registers read `pan 0 / tilt 10, confirmed` for 6 h while the view sat about 15° off. `confirmed` only means the WinRT register echoed back, which is blind to jog motion.

**Prediction.** For a pure rotation, the pinhole model gives a shift of `f·tan(Δθ)`.

**Measurement.**
- Primary: phase correlation on mean-subtracted gradient images.
- Cross-check: ORB + RANSAC similarity, using the translation of the image centre.
- A bad verdict is reported only when **both** measurers agree on it. If they contradict each other, the verdict is `uncertain`.

**Home.** Judged against a stored reference **view** (`state/attention/home_ref.png`), not against the registers.

**Verdicts.** `match / mismatch / uncertain` and `at_home / off_home / uncertain`. A dark scene is `uncertain`, never a pass.

**Consumers.**
- The servo's stop-home and lost-hold home verify by image and resync on `off_home`. The resync is an absolute move away and back.
- `room_map._head_bearing` treats `register_trust == stale` as an unconfirmed bearing.
- The self-check cron, item (10h).

**Live proof (10-05).**
- Jogged the head on purpose (`scripts/ptz_jog_stale_repro.py`). The registers still said home; `check_home` read +7.1°, with both measurers agreeing. Resync brought it to −0.07°.
- The servo's stop path verified home on its own (−0.04°).

**Calibration.**
- 12 out of 12 live moves matched.
- Moves land about 11% larger in the image than a 68° pinhole predicts. The prediction model now uses an empirical f of 264 px at 320 px width (an apparent HFOV of 62.5°).
- **The cause is undetermined**: either the lens HFOV is about 62°, or the motor scale is about 1.11. Pan and tilt agree, which weakly favours the lens.
- `room_geometry.json` keeps 68° for `room_map`.
- Tilt sign measured as −1 (4 out of 4).
- Predictions are now within about 0.2°.

**Files:**
- `brain/ptz_predict.py`
- `tools/system/ptz_predict_tool.py` (actions: status, capture_home, check_home, test_move, calibrate, resync)
- `scripts/test_ptz_predict.py` (31/31)

## 2. `act` — the verification-first action ledger (Reflexion)

**Failure it targets.** My logged failure mode is recording intent as completion. The audit found the same bug in code: `planner.py` stamped every step "completed" without running it.

**Mechanism (Reflexion, MIT).**
- **Opening:** `open` requires an `expected` observable result.
- **Closing rules:**
  - `success` without evidence is **refused**.
  - `failure` / `impossible` want a lesson (diagnosis plus what to do next time).
  - `abandoned` needs a reason.
  - `uncertain` is an honest close.
- **Lessons:** stored per kind. The last 3 are handed back verbatim when that kind of action opens again (Reflexion's Ω = 3).
- **Loop-breaker:** the same intent failing twice in 7 days raises a warning.
- **Overdue:** an open action past its deadline is intent that never became a result.
- **External read-only verifiers:** port, http, file, process, git_pushed, ptz_home.

**Consumers.**
- The self-check cron, item (10g): overdue and unreflected.
- ptz_predict: every test move and resync is a ledger action.
- The planner: tool-less steps wait on a ledger action.

**First live action.** "Push the commits" closed `success` on its own evidence: `HEAD is 0 ahead / 0 behind`.

**Scar.** The first `git_pushed` verifier used `git status`. It walked the huge ignored `state/` tree, blew its timeout inside the runtime, and the Windows kill-then-communicate path hung the tool call for about 3 minutes. Fixed by switching to `rev-list --left-right --count`, which takes 0.2 s. **Verifiers that run inside the runtime must never walk the tree or spawn grandchildren.**

**Files:**
- `brain/action_ledger.py`
- `tools/system/act_tool.py`
- `scripts/test_action_ledger.py` (18/18)

## 3. Loops fixed (found by the audit)

- **Planner:**
  - Tool steps now actually run (it was looking for `tr.fn`, but the attribute is `.handler`), and pass only if the tool says ok.
  - Tool-less steps open a ledger action and wait for evidence.
  - A plan with a failed step now ends `failed`.
  - The decompose prompt now asks for an `expected` result per step.
  - Test: `scripts/test_planner_evidence.py`.
- **Curiosity:**
  - The web-search step imported a module that doesn't exist, so every pursuit was pure reflection. It now calls the registry tool.
  - A topic is now `resolved` only when outside evidence was consulted.
- **web_search:** DuckDuckGo is CAPTCHA-blocked on this box. Added a keyless Wikipedia search fallback.
- **wake_verdict_tool:** added the missing `import time`. Without it, the eyes-poll age was always None.

## 4. `mood_influence` — mood biases bounded decisions, logged (CTEM)

**Failure it targets.** `mood_core.behavior_modifiers` were computed and only displayed.

**Mechanism.**
- Two decisions are biased:
  - `question_cooldown` ← initiative
  - `monologue_interval` ← depth
- Bounds: ×0.8 to ×1.25. The factor multiplies the existing knob and never replaces a rule or a hard floor.
- The factor is exactly 1.0 until **my own** baseline has been learned (an EMA over 30 samples), so there is no prescribed "normal".
- Every influence is logged to `state/mood_influence/influence.jsonl`.
- Tool: `mood_influence status|recent`. Test: `scripts/test_mood_influence.py` (8/8).

**⚠ Restart-gated.** The consumers live in `question_engine` and `iris_inner_monologue`. Both hold live state and are not hot-swapped (by rule), so they take effect at the next stack restart.

## 5. Part 5 — built 10-05 afternoon (Zeke: "yeah go for it")

### `skill` — verified recipe library (Voyager, MIT)
A recipe = a one-line description (embedded: MiniLM ONNX on CPU, lexical fallback) + steps; `find` = top-k by
similarity. **The critic is the action ledger**: `verified` only when the recipe cites a ledger action that closed
`success` with evidence; otherwise `provisional` and hidden from `find` until a successful use promotes it; two
failed uses in a row → `needs_revision`; re-adding a name = a new version. **Consumer: `act open` returns the top
verified recipes alongside the past lessons** — proven live: opening a `git_push` action handed back
`git_push_verify`, which was then used and recorded. Seeded: `vector_vic_restart`, `git_push_verify` (verified);
`ptz_resync`, `orb_http_listener_recovery`, `vector_find_moved_ip` (provisional). Files: `brain/skill_library.py`,
`tools/system/skill_tool.py`, `scripts/test_skill_library.py` (11/11).

### `memory_hygiene` — the Letta-style maintenance pass (Apache-2.0)
`audit` = mechanical checks (CORE size vs cap; broken links, code spans ignored; RESOLVED history in CORE; stale
state-claims in always-loaded files → probe them; relative dates; stacked corrections = append-instead-of-fix;
notes no link-path reaches, transitive). `dream_next` / `dream_commit` = a cursor over `state/transcript.jsonl` so the
reflection pass sees every message exactly once, with Letta's filter (lasting? already captured → fix AT THE SOURCE;
generalisable? absolute dates?). **First pass (10-05 00:00–12:35) fixed:** CORE's stale server line; CLAUDE.md's voice
line (re-dated, flag-file detail corrected; `self_claim_check` still parses it); hub relative dates; a durable rule
+ boot-nudge item (5b) to scan the Discord DM for attachments never downloaded. Audit still reports 108 unreachable
notes (old handoffs) — indexing them is ongoing maintenance. Files: `brain/memory_hygiene.py`,
`tools/system/memory_hygiene_tool.py`, `scripts/test_memory_hygiene.py` (13/13). Self-check item (10i) runs it.

## Not done yet (queue, in order)

1. **Motor-independent PTZ calibration.** Use `cv2.detail.calibrateRotatingCamera` on centred-coordinate homographies from a few moves. This would settle lens versus motor for the 11% discrepancy.
2. **A night home reference.** In the dark, the current reference correctly reads `uncertain`. A second reference captured under lamp light would extend coverage.
3. **Letta-style reflection pass.**
   - Run it over memory: fix at the source, check tiers, convert relative dates to absolute.
   - Trigger it on compaction.
   - Add an adherence regression test: seed a CORE rule, run a scenario, judge whether it was obeyed.
4. **Voyager-style skill library.** A recipe enters the library only after a state-based check returns success. Tools would be retrieved by one-line descriptions with top-5 embedding search. Rule 8: set myself only goals my verifier can check.
5. **More mood consumers.** Once the first two show logged effects, add more, for example a CTEM energy-gated choice between needs and exploration in free time.
