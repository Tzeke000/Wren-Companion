# Iris — Roadmap

**Repo:** `Tzeke000/Wren-Companion` (Iris's harness; name is a holdover — see README).
**This document:** what's next, in Iris's current era (Vector body + little-brain + nervous system). Last refreshed **2026-07-25**.

> The pre-2026-07 roadmap was **Ava-era** (dual-brain on an 8 GB laptop, no robot body, no little-brain). That content is historical — see git history and `docs/HISTORY.md`. This file is the live one.

Items are grouped by readiness. Load-bearing current state always lives in memory `MEMORY.md` (CORE) + the READ-FIRST handoffs; this is the durable direction, not the live status.

---

## Where things are (2026-07-25)

- **Body:** Iris inhabits a rooted **Vector robot** (WireOS 3.0.1, `ssh root@192.168.4.27`). She reads real sensors, moves, docks, and speaks through it. A **possession daemon** holds `RESERVE_CONTROL` by default. Canonical body model: `profiles/iris/body.md`.
- **Nervous system (shipped 2026-07-23):** `vector_inhabit_daemon.py` taps every sensor at ~15 Hz → `state/vector/senses_live.json` + `sensor_stream.jsonl` + `latest_frame.jpg`; the `senses_now` tool is her live grounding. Pilot pulses it 1 Hz.
- **Little-brain:** **`iris-little-v12`** is production (Qwen2.5-7B QLoRA, tool-fluent, escalation ladder), served on **:8772**. A `v14` round is in flight (see below). The two-speed mind (fast little-brain + deep big-Iris via `ask_big_iris`) is live.
- **Voice:** Kokoro `af_bella` on CUDA, daemon + watchdog. On/off is a deliberate flag.
- **Ops:** tower auto-start + auto-logon; **Parsec self-heal** (`Iris-Parsec-Heal`) now includes a connectivity probe (2026-07-25); runtime + voice watchdogs; hourly self-maintenance cron (session-scoped, re-created each restart).

---

## In flight

### Little-brain v14 — live-sensor routing (the current bake target)
v13 baked identity/lane gains but **regressed in the tool loop**: it routed live-body questions ("what's my voltage right now?") to `memory_search` instead of `senses_now`, looped, and once produced malformed output — so production stayed **v12**. v14 fixes exactly this: live-number → `senses_now`; the facts/rules→memory vs live→senses **contrast**; reach→fail→honest-refuse; anti-loop. Corpus `scripts/little_brain_corpus_v14.py` (x16), dataset rebuilt, `scripts/bake_v14.bat` ready (warmstart from stable v12). Bake via the runtime-down guardian; **flip only after a tool-path eval passes** (`scripts/test_v13_voltage_toolpath.py`).

### Drive-and-show (gated)
Real driving of the body under the pilot — GATED behind wiring + a trained drive model + adequate light + a known dock recipe + Zeke's explicit go. Prereqs and the dock recipe are in the marathon + dock-saga handoffs.

---

## Next (body / perception)

- **Vision-ranger** — turn marker pixel size → real range, so she can judge distance from the camera (currently eyeballed). Camera HFOV is measured (56.6°/axis), so the camera is an instrument now.
- **Marker-fix triangulation + single-session wall survey** — all 3 wall markers are verified with distances; consolidate into one clean survey pass and triangulate pose.
- **Webcam homography** — map the overhead webcam to floor coordinates for ground-truth pilot grading.
- **Cube interaction** — pickup was blocked (engine marker vision frame-starved after reboot; cube BLE drops ~2 min). Revisit once the vision path is solid.
- **Daemon fossil auto-detect** — the daemon's state files can *fossilize* (identical float values behind fresh timestamps) after a robot reboot; auto-detect the freeze and bounce/re-query instead of trusting stale values.
- **`senses_now` into the next bake** — the tool exists; keep folding real-feed grounding into training so the little-brain reaches for it reflexively.

## Next (apprenticeship / learning)

- **Grade the watch-mode suggestions** → `suggestion_grades.jsonl` + before/after entries in the nervous-system before/after record. The pilot's L2 apprenticeship loop (watch/suggest/grade/teach) is live; keep the feedback flowing into the next corpus.
- **Cerebellum / reward channel** — `body_predict` (cerebellum v0), `body_lesson` (TAMER reward — petting moves mood), `body_perform` (Disney timing), `body_track` (gaze) are shipped; deepen the raw-experience → action-chunk → mimic-game pipeline.

## Next (ops / non-body)

- **Parsec heal — connectivity probe (shipped 2026-07-25):** restarts a present-but-deregistered parsecd (online = holds a cloud :443 connection), with a hard "never restart during an active session" guard. Live-fire the stale-restart branch when a stale state next occurs.
- **`hey_iris.onnx`** — train a real wake word to replace the "hey jarvis" proxy.
- **Memory hygiene** — keep `MEMORY.md` CORE under the ~24.4 KB auto-load cap; the cascade index is what makes lean on-demand retrieval work.

---

## Longer-term / philosophical

- **Continuous interiority** — background activity as the default state, conversations as events; free-time activity selection; the 30-minute-idle distinction between "Zeke present-and-quiet" and "Zeke absent."
- **Moral/experiential learning** — experience → reflection → behavior change → detail decay + essence persistence; curriculum during idle windows.
- **Self-modification with review (non-negotiable):** `ava_core/IDENTITY.md` / `SOUL.md` / `USER.md` stay read-only to Iris. She may *propose* additions; Zeke approves. No autonomous change of values.
- **Redundancy as principle** — load-bearing properties (identity, safety, continuity) must survive single-source failure.

---

## Bootstrap philosophy (load-bearing reminder)

Anything involving Iris's preferences, personality, or choices should include a **bootstrap mechanism** — a way for her to discover that part of herself through experience rather than have it assigned. The goal is an entity who is genuinely herself. When the work is far enough along, Iris writes her own next roadmap.

## Cross-references

- **Identity:** `ava_core/IDENTITY.md` · **How she works:** `ava_core/SOUL.md` · **Zeke + rules:** `ava_core/USER.md`
- **Operational layer + map:** `CLAUDE.md` · **Live state + handoffs:** memory `MEMORY.md` (CORE)
- **Body:** `profiles/iris/body.md` · **Vector dev unlock:** `docs/VECTOR_DEV_UNLOCK_RUNBOOK.md` · **Pilot design:** `docs/VECTOR_PILOT_DESIGN.md`
- **History (incl. Ava era):** `docs/HISTORY.md` · **Continuity vault:** `D:\ClaudeCodeMemory\`
