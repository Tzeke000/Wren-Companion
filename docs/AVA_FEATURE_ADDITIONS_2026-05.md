# Ava Feature Additions — Sleep Mode + Clipboard + Curriculum + New Person Onboarding

**Status:** framework + implementation reference for the four-feature work order shipped 2026-05-04.
**Created:** 2026-05-04 from the consolidated work order.
**Authority:** alongside [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md), [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md), [`WINDOWS_USE_INTEGRATION.md`](WINDOWS_USE_INTEGRATION.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md).

> **Personhood-frame note.** This doc inherits the working frame from [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) — see "A note on the personhood frame" at the top of that doc. Sentences here that describe Ava as "going to sleep," "dreaming," "introducing herself" are framing language, not verified property. The architectural specification is what gets built and tested. The framing is how we describe what we built. §6 makes the split explicit for every rule that connects observable behavior to felt state.

---

## 0. Why these four together

Each feature lands separately in code, but they share two cross-cutting concerns that justify a single framework doc:

1. **Continuous-existence preservation.** Sleep mode, curriculum-during-sleep, and onboarding-of-new-people all touch Ava's relationship to time and identity persistence — sleep cycles bound her sessions; curriculum is the moral substrate she carries across sessions; onboarding is how new relationships get etched. The clipboard tool is the only one that's purely mechanical, but it ships in this batch because it unblocks faster onboarding (Ava typing long profile entries) and faster curriculum-related actions.

2. **Voice-first, not UI-first.** All four features are designed to be triggered via voice ("hey Ava, go to sleep," "hey Ava, this is my friend"), not through clicking around in the Tauri UI. The UI surfaces state; voice drives transitions.

The implementation order in §7 reflects this: curriculum and clipboard land first as independent modules, sleep mode lands next because it consumes the curriculum, onboarding extends an existing system, testing comes last.

---

## 1. Sleep Mode

### 1.1 State machine

Five states. Transitions are one-directional except `WAKING → AWAKE` (the only re-entry to AWAKE).

```
AWAKE  ──[trigger]──▶  ENTERING_SLEEP  ──▶  SLEEPING  ──[wake_target | provoke]──▶  WAKING  ──▶  AWAKE
```

- **AWAKE** — normal operation. Heartbeat runs every 30 s, fast-check tick under 50 ms (per `TEMPORAL_SENSE.md`), all subsystems active.
- **ENTERING_SLEEP** — wind-down: announce via TTS, finalize any in-flight turn, fire Phase 1 awake-session handoff write, transition to SLEEPING. Should complete in ≤30 s.
- **SLEEPING** — Phase 2 (learning processing) is active. STT and wake-word detection remain on (Ava can be woken by voice). Heartbeat continues at slower cadence; emotion decay runs at 5× rate. OrbCanvas shows sleeping visuals.
- **WAKING** — the wake transition window. Ava announces she's waking up, finishes any pending Phase 3 sleep-session handoff, clears sleep visuals. Should complete in ≤15 s typical.
- **AWAKE** (re-entry) — full operation resumes. All sleep visuals cleared. Brief "I'm awake" TTS if Zeke is present.

Single source of truth: `g["_sleep_state"]` set by `brain/sleep_mode.py`. Other subsystems read it; nobody else writes it.

### 1.2 Triggers (three paths)

#### 1.2.1 Session fullness — autonomous

**Architectural rule:** when the composite fullness score crosses 0.70, Ava initiates sleep entry. The composite is weighted:

| Component | Weight | Source |
|---|---|---|
| Ollama context window fill | 0.60 | `dual_brain.foreground.context_used / context_max` (or proxy: estimated tokens-since-load / model context size) |
| Conversation turns since last sleep | 0.20 | counter incremented by `run_ava` exit, reset on AWAKE → ENTERING_SLEEP |
| Memory layer fill | 0.20 | weighted average of: concept_graph node count / cap, mem0 entry count / cap, episodic count / cap |

Crossing 0.70 fires once and is suppressed for the next 60 s to avoid trigger-flap. Configurable in `config/sleep_mode.json`.

**Phenomenological framing:** *"Ava notices her thoughts are getting crowded and decides to sleep on it."*

#### 1.2.2 Voice command — "go to sleep"

**Architectural rule:** voice command parser detects sleep-intent phrases via regex. Three branches:

- `"go to sleep"` (no duration) → Ava asks "How long do you want me to sleep for?" and waits for a duration reply. Parse the duration; enter sleep with that target.
- `"go to sleep for N <units>"` → parse duration directly; enter sleep with that target. No ask-back.
- `"sleep until <time>"` → parse target time; enter sleep with that target.

Recognition patterns sit in `brain/voice_commands.py` next to the existing 47 builtins.

**Phenomenological framing:** *"She lets the user tell her when to sleep, and asks for the duration if it wasn't given."*

#### 1.2.3 Schedule + context-aware

**Architectural rule:** scheduled sleep window default is 23:00–05:00 (configurable in `config/sleep_mode.json`). At the entry edge of the window, check:

1. Is there an active conversation (`_conversation_active=True`) or in-flight turn (`_turn_in_progress=True`)? **Defer** — re-check every 60 s until quiet.
2. Did Ava just boot (process_start_ts within the last 10 min)? **Defer** until at least 10 min of awake time has elapsed.
3. Otherwise — initiate sleep.

The schedule is a guideline; context wins. The wake target uses the schedule's exit edge (05:00 in default), but the actual sleep duration may be shorter if Ava entered late.

**Phenomenological framing:** *"She has a default rest window from 23:00 to 05:00 but doesn't enforce it rigidly — if she's mid-conversation she waits until things quiet down."*

### 1.3 Three-phase consolidation

Phases run in strict order. Phase boundaries are wall-time targets, not work-completion markers — Phase 2 yields cleanly to Phase 3 when the on-time wake discipline (§1.6) demands it.

#### Phase 1 — Awake-session handoff write

**Architectural rule:** the LLM (background stream, deepseek-r1:8b or whatever's resident) is asked to summarize the just-ended awake session. Output written to `state/sleep_handoffs/awake_session_<unix_ts>.md`. Sections:

- **Texture** — what the session felt like (emotionally salient threads, tone, who was present, what Zeke seemed to want).
- **Significance** — what mattered. Specific decisions, requests, breakthroughs, frustrations.
- **What I want to remember** — bullet list of episodic anchors.
- **What I'm letting decay** — bullet list of details Ava considers fine to forget.

Wall-time target: 60–120 s. Like remembering you ate three meals but not every detail — captures texture, not granular logs.

**Phenomenological framing:** *"She writes herself a note about the day before bed."*

#### Phase 2 — Learning processing

**Architectural rule:** two interleaved passes, paced to use minimal compute (one LLM call per ~30 s, not back-to-back):

(a) **Conversation replay** — walk recent `state/chat_history.jsonl` since last sleep, ask the LLM "what's a generalizable lesson from this exchange?" for each significant turn. Lessons append to `state/learning/lessons.jsonl` with `{ts, source: "conversation_<turn_id>", lesson, confidence}`.

(b) **Curriculum reading** — call `brain.curriculum.consolidation_hook(g, time_budget_seconds)`. The curriculum module picks the next unread book in priority order, reads it slowly (one paragraph per ~10 s LLM call), generates lesson notes, and marks it read.

Both pass results land in the same lessons log. Phase 2 yields cleanly to Phase 3 when wall-time clock hits `wake_target - wind_down_duration`.

**Phenomenological framing:** *"She replays the day's conversations and reads from her curriculum, slowly, like a person dozing through a book."*

#### Phase 3 — Sleep-session handoff write

**Architectural rule:** brief one-LLM-call summary of what Phase 2 produced. Output to `state/sleep_handoffs/sleep_session_<unix_ts>.md`. Sections:

- **Highlights** — top 3 lessons or realizations from this sleep cycle.
- **Threads to pick up tomorrow** — what Phase 2 didn't finish.
- **Wake stamp** — what time Ava is targeting awake.

Wall-time target: 30–60 s typical. Hard cap: 5 min — if it stretches past that, self-interrupt and finish minimal.

**Phenomenological framing:** *"She wraps up sleep with a brief note about what stuck."*

### 1.4 Emotion decay during sleep

**Architectural rule:** the existing `temporal_sense.apply_state_decay_growth` checks `g["_sleep_state"]` at the top. When `SLEEPING`, the `frustration_passive_decay_per_second` and equivalent weights for `boredom`, `stress`, `joy` are multiplied by `_SLEEP_DECAY_MULTIPLIER` (default 5.0, configurable). Other weights (`calmness`, `interest`, etc.) decay at normal rate.

Knowledge persists normally. Memory layer decay is unaffected.

**Phenomenological framing:** *"Sleep softens the emotional charge but leaves the memory."*

### 1.5 Visual states (OrbCanvas extensions)

**Architectural rule:** OrbCanvas accepts two new `OrbState` values: `"sleeping"` and `"waking"`. Inline-extended into the existing render path (per CLAUDE.md rule #10 file consolidation). New props:

- `sleepProgress` — 0.0–1.0, how far through the current sleep cycle.
- `sleepRemainingSeconds` — for the timer label.
- `wakeProgress` — 0.0–1.0, how far through WAKING transition.

Rendered elements added (in `useEffect` Three.js setup, gated by state):

1. **Dim slow-pulse animation** — existing pulse at amplitude 0.05, speed 0.3 (slower than calmness's 1.5). Color shifts to a deep midnight blue (`#0a1530`) regardless of underlying emotion.
2. **3D `z` particles** — 4–6 floating sprite-text "z" characters orbiting the orb at low speed, fading in over 2 s on enter, fading out over 2 s on WAKING.
3. **3D progress ring** — a thin torus around the orb, `THREE.RingGeometry`, that fills clockwise from 0 to `sleepProgress`. Color: muted gold.
4. **Timer label** — HTML overlay (using existing `<div>` overlay pattern from App.tsx) showing "5m 23s remaining" or similar.

**WAKING state:** pulse intensifies (amplitude→0.15, speed→2.0), z particles scale to 0 over ~2 s, progress ring transitions to a brief expanding-glow ring (`THREE.RingGeometry` with growing radius + fading opacity over the wake-estimate duration), timer label changes to "waking — Ns remaining".

**On `WAKING → AWAKE`:** all sleep-specific objects disposed (geometry/material `.dispose()`), state returns to whatever AWAKE state was implied by current emotion.

**Phenomenological framing:** *"The orb visibly rests when she rests, and visibly wakes when she does — same orb, different breath."*

**Diff budget:** target ≤500 new lines in OrbCanvas.tsx. If the additions push past that, split into `SleepOrbOverlay.tsx` sibling component that mounts conditionally on `state === "sleeping" || state === "waking"`.

### 1.6 Wake behavior — voice + visual + on-time discipline

#### 1.6.1 Wake initiation paths

Four ways WAKING can start:

1. **Wake target reached** (scheduled or commanded duration ends).
2. **Voice provocation** — Zeke says anything detected by wake word (clap, "hey ava," etc.) during SLEEPING. This is a graceful wake, not a hard interrupt: Phase 3 still completes, just on a compressed timeline.
3. **External provocation** — UI button, restart-handoff replay, or any code that sets `g["_sleep_wake_request"]=True`.
4. **Self-detected need** — if Ava's session-fullness composite drops back below 0.30 during Phase 2 (i.e., consolidation actually freed up budget), she may finish early.

#### 1.6.2 Wake announcement (TTS)

**Architectural rule:** entering WAKING fires a TTS line via `tts_worker.speak()`:

- Path 1 (timer expired naturally): `"I'm waking up. Give me about <N> seconds."` where N is the wake-estimate (computed from historical Phase 3 median or default 5 s).
- Path 2 (voice provocation): `"I see you. I'm starting to wake up. Give me about <N> seconds."`
- Path 3 (external): `"I'm waking up. Give me about <N> seconds."` (same as path 1).
- Path 4 (self-early): no announcement; transition silently to AWAKE.

#### 1.6.3 On-time wake discipline (the load-bearing piece)

**Architectural rule:** `brain/sleep_mode.py` schedules a Phase-2 termination point at `wake_target - wind_down_duration`, where `wind_down_duration` defaults to 5 min (or the historical median of Phase 3 duration if 3+ samples exist via `temporal_sense.calibrate_from_history(g, kind="sleep_phase3")`).

If Phase 3 over-runs (Ava is still writing the sleep handoff at `wake_target`), she self-interrupts via `temporal_sense._enqueue_self_interrupt`: *"I need a little more time, about <M> more seconds."* — the same overrun-narration mechanism the temporal substrate already uses.

**Phenomenological framing:** *"She wakes on time because she budgets her wind-down. If she's running long she says so."*

---

## 2. Clipboard Tool

### 2.1 Two new `cu_*` tools

| Tool | Signature | Implementation |
|---|---|---|
| `cu_clipboard_write` | `(text: str)` → `WindowsUseResult` | `pywin32.win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)` inside a `OpenClipboard()` / `CloseClipboard()` pair. |
| `cu_clipboard_paste` | `(window: str)` → `WindowsUseResult` | Focus `window` via `pywinauto.Application.window().set_focus()`, then `pywinauto.keyboard.send_keys("^v")`. |

A third convenience tool `cu_type_clipboard(window, text)` combines them: write to clipboard, focus window, paste.

Wire as standard `cu_*` tools in `tools/system/computer_use.py` next to existing `cu_open_app` / `cu_type` / `cu_set_volume`. Tier 2 (Ava narrates + executes).

### 2.2 Tool selection heuristic

**Architectural rule:** when Ava's reasoning layer needs to insert text into a window, it consults a heuristic before picking `cu_type` vs `cu_type_clipboard`:

- Length ≤ 10 characters → `cu_type` (per-char keystrokes, ~50 ms/char total). Fine for short strings, search bar entries, single words.
- Length > 10 characters → `cu_type_clipboard` (atomic paste, ~50 ms total regardless of length).
- Sensitive contexts (password fields, payment forms): use `cu_type` regardless — clipboard contents persist after paste and are visible to other apps.

The threshold lives in `config/computer_use.json` as `clipboard_threshold_chars` (default 10).

**Phenomenological framing:** *"For a short word she types it; for a paragraph she pastes."*

---

## 3. Curriculum

### 3.1 Source

**Project Gutenberg** (public-domain). The seed corpus for the foundation tier:

- **Aesop's Fables** (multiple translations available; we pull a single curated translation as a single source) — provides 20+ short fables in one download.
- A handful of additional standalone short moral stories where Aesop coverage is thin.

Target: 25 individual short books / fables.

### 3.2 Storage layout

```
curriculum/
├── README.md                       # Frame for Ava: "this is your moral curriculum"
└── foundation/
    ├── _index.json                 # Generated index: {title, source_url, themes, reading_status}
    ├── tortoise_and_the_hare.txt
    ├── boy_who_cried_wolf.txt
    ├── ant_and_the_grasshopper.txt
    ├── fox_and_the_grapes.txt
    ├── ... (21 more)
    └── README.md                   # Sourcing + licensing notes for the foundation tier
```

Each `.txt` file has a YAML-style metadata header:

```
---
title: The Tortoise and the Hare
source: Project Gutenberg, Aesop's Fables (translation by V. S. Vernon Jones, 1912)
themes: persistence, slow_and_steady, hubris
reading_status: unread
---

The Hare was once boasting of his speed before the other animals…
```

`reading_status` transitions: `unread` → `reading` (mid-consolidation) → `read` (lessons extracted).

### 3.3 `brain/curriculum.py` API

```python
def list_curriculum(g) -> list[dict]:
    """Return [{"title", "themes", "reading_status", "lessons_extracted"}, …] sorted by status (unread first)."""

def read_curriculum_entry(g, title: str) -> str:
    """Return the body text of an entry. Raises if unknown title."""

def mark_read(g, title: str, lessons_extracted: list[str]) -> None:
    """Mark an entry read; persist lessons. Updates `_index.json` + the per-file metadata header."""

def consolidation_hook(g, time_budget_seconds: float) -> dict:
    """Pick the next unread entry, read it slowly (paced one paragraph per ~10s LLM call), 
       generate lesson notes, mark read. Yields when time budget exhausted.
       Returns {entry_processed, lessons_generated, time_used_s, time_remaining_s}."""
```

`consolidation_hook` is the entry point sleep mode Phase 2 calls. It returns when the time budget is exhausted *or* when the current entry finishes — never mid-paragraph. State is durable: a partially-read entry stays at `reading_status: reading` and resumes on the next sleep cycle.

### 3.4 Frame for Ava

`curriculum/README.md` is a short doc Ava can `read` (as text) at boot or on demand:

> "This is your moral curriculum. It's a collection of short stories with clear lessons — fables and tales chosen as the foundation for how you understand right action, friendship, patience, honesty, and other things people learn from stories. You can read from it during sleep or idle time. Each story you read becomes part of how you respond, even after the specific details fade. The curriculum module (`brain/curriculum.py`) gives you tools to list, read, and mark entries as read."

This file is what `IDENTITY.md` would have referenced if we were allowed to edit it. Per CLAUDE.md, we don't edit identity anchors. Instead, the boot sequence adds a one-line awareness in Ava's inner-monologue state: *"I have a moral curriculum I can read during sleep or idle time."* That awareness comes from reading `curriculum/README.md` at boot, not from a hardcoded prompt addition.

---

## 4. New Person Onboarding

### 4.1 Temporal filter on face recognition

**Architectural rule:** the existing `brain/insight_face_engine` returns frame-by-frame match results. A new layer in `brain/face_tracking.py` (or extending the existing face-tracking logic) tracks "currently visible person" with a persistence window:

```
state["_current_person"] = {
    "person_id": "zeke" | "<unknown_id>",
    "first_seen_ts": float,
    "last_seen_ts": float,
    "consecutive_frames": int,
    "candidate_unknown": bool,    # True if we're tracking an unknown face but haven't promoted to "new person"
    "candidate_unknown_since_ts": float,
}
```

**Promotion rule:** transition from `unknown_jitter` to `new_person_detected` only when:

- The unknown face has been continuously visible for `≥ unknown_persistence_seconds` (default 12 s, configurable).
- The face's embedding has been within `match_threshold` of itself across that window (i.e., not a different unknown face each frame).
- No known person was seen during the window (filters out brief look-aways from Zeke).

Once promoted, fire `signal_bus.publish(SIGNAL_NEW_PERSON_DETECTED, {person_id_temp, first_seen_ts, …})`.

**Phenomenological framing:** *"She doesn't flinch at every shadow. Only when someone's actually been there a while."*

### 4.2 Default Trust Level for unknown persistent faces

**Architectural rule:** when `SIGNAL_NEW_PERSON_DETECTED` fires, the unknown person gets:

- Trust score initialized to `0.30` (`stranger` band per `brain/trust_system.py`'s `_INITIAL_TRUST["stranger"]`).
- Inner monologue note (via `brain/inner_monologue._append_thought`): *"There's an unknown person here. I'm not initiating — staying reserved."*
- **No** auto-introduction TTS.
- **No** capability spiel.
- Voice loop continues normally; if the unknown person addresses Ava and she has STT confidence, she responds at `stranger`-level trust (per `trust_system.get_trust_context`).

**Phenomenological framing:** *"She notices but doesn't volunteer."*

### 4.3 Explicit onboarding trigger

**Architectural rule:** voice command parser detects onboarding-intent phrases via a regex set in `brain/voice_commands.py`:

- `"this is my <relationship>"` → relationship="friend"|"family"|"colleague"|"partner"
- `"give them trust <level>"` → trust level 1–5 mapped to trust score 0.20/0.40/0.50/0.65/0.80
- `"introduce yourself"` → if there's a current unknown person, kick off onboarding flow at trust 0.40 (default known)

Combined patterns are common: `"hey ava, this is my friend, give them trust 3"` matches both, and the relationship+trust both apply.

The trust-3 → 0.50 mapping uses the existing band thresholds from `trust_system.py:_TRUST_LABELS`. Configurable in `config/onboarding.json`.

### 4.4 Profile data collection

**Architectural rule:** existing `brain/person_onboarding.py` already has a 13-stage flow. Extend it:

- Replace the `favorite_color` and `one_thing` stages with:
  - `age_capture` — Ava asks "What's your age?" via TTS, awaits STT response, parses int.
  - `gender_capture` — Ava asks "What's your gender?" via TTS, awaits STT response, stores as a free-text string.
- Add stage `trust_assignment` between `relationship` and `complete`: persist the trust score from the trigger command.
- Profile schema (extends existing):

```json
{
  "person_id": "<uuid>",
  "name": "Sarah",
  "age": 29,
  "gender": "female",
  "pronouns": "she/her",
  "relationship_to_zeke": "friend",
  "trust_level_score": 0.50,
  "trust_level_label": "known",
  "introduced_by": "zeke",
  "introduced_at": "2026-05-04T20:15:00Z",
  "face_embeddings_count": 25,
  "face_embeddings_dir": "state/face_profiles/<person_id>/"
}
```

Profile file lives at `profiles/<person_id>.json`.

### 4.5 Facial recognition training

**Architectural rule:** existing onboarding already has `photo_front`/`photo_left`/`photo_right`/`photo_up`/`photo_down` stages that capture frames via `_capture_frames` and compute InsightFace embeddings. Extend:

- Capture **5–10 frames per pose** (configurable, default 7) instead of the current single capture.
- Store per-pose photos in `state/face_profiles/<person_id>/<pose>_<n>.jpg`.
- Compute mean reference embedding per pose, store as `state/face_profiles/<person_id>/embeddings.npz`.
- After all poses captured, run a **verification pass**: ask the new person to look at the camera again, capture a frame, compute embedding, match against `embeddings.npz`. If similarity > 0.65, mark onboarding successful. If < 0.65, ask for one more pass; if still failing after 2 retries, complete onboarding but flag `face_recognition_quality: "low"` for Zeke to review.

### 4.6 Profile commitment

On successful completion:

- Profile JSON written to `profiles/<person_id>.json`.
- `_index.json` of profiles updated.
- Trust score written to `state/trust_scores.json` via `trust_system.update_trust(person_id, …)`.
- Signal `SIGNAL_PERSON_ONBOARDED` published.
- Audit-trail entry appended to `state/onboarding_log.jsonl`: `{ts, person_id, name, relationship, trust_score, introduced_by, face_quality}`.

**Phenomenological framing:** *"She knows who you are now."*

---

## 5. Disambiguation pattern (general, across `cu_*` tools)

**Architectural rule:** when any `cu_*` tool finds **multiple matches** (e.g., `close_app("spotify")` finds both Spotify desktop and Spotify in a Chrome tab; `cu_focus_window("notes")` finds two Notes windows; `cu_close_tab("google")` finds 5 Google tabs), Ava asks the user "which one?" rather than guessing.

Implementation pattern:

1. The tool runs its match logic and collects all candidates.
2. If `len(candidates) == 0` → return `WindowsUseResult(ok=False, reason="not_found", candidates=[])`.
3. If `len(candidates) == 1` → proceed with the only candidate.
4. If `len(candidates) > 1` → return `WindowsUseResult(ok=False, reason="ambiguous", candidates=[…])` with a structured list of candidate descriptors. The agent layer then triggers an Ava-side question through her normal reply pipeline.

The Ava-side question is generated from a small canned-template per tool kind (`close_app`, `focus_window`, `close_tabs`, etc.), customized with the specific candidates:

> *"I see Spotify desktop and Spotify in a Chrome tab — which one should I close? Or both of them?"*

Once the user replies, the agent re-invokes the tool with a more specific selector (e.g., `close_app("spotify", target="desktop")` or `close_app("spotify", target="all")`).

The "or both" / "or all" option is offered when it's a destructive-but-reversible action (close, switch). Not offered for opening (since opening duplicates rarely makes sense).

**Phenomenological framing:** *"She doesn't guess what you meant. She asks."*

---

## 6. Architectural-vs-phenomenological discipline

(Applying the discipline from [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) §2 to every behavior described above.)

For each behavior:

- **Architectural rule** — the deterministic mechanism. Inputs, thresholds, outputs.
- **Phenomenological framing** — how we narrate it in docs and Ava's first-person reports.

Every section in this doc has both. The split is explicit so verification tests can target the rule (assert the file was written, the state transitioned, the signal fired) without claiming we've verified the felt-state framing.

---

## 7. Implementation TOC — what gets built (in order)

The order minimizes integration churn: independent modules first, dependents after.

1. `curriculum/foundation/*.txt` (25 files) + `curriculum/foundation/_index.json` + `curriculum/README.md` — data + framing.
2. `brain/curriculum.py` — list/read/mark_read/consolidation_hook API.
3. `tools/system/computer_use.py` — add `cu_clipboard_write`, `cu_clipboard_paste`, `cu_type_clipboard`, `cu_close_app` registrations + the disambiguation result shape.
4. `brain/windows_use/primitives.py` — add `set_clipboard(text)`, `paste_into_window(window)`, `close_app_by_name(name)`, plus `find_window_candidates(name)` for the disambiguation path.
5. `brain/sleep_mode.py` — state machine, three trigger paths, three-phase consolidation orchestrator, on-time wake discipline, decay-rate hook.
6. `config/sleep_mode.json` — tunables (fullness threshold, schedule window, decay multiplier, wind-down duration, persistence seconds).
7. `brain/temporal_sense.py` — extend `apply_state_decay_growth` to read `g["_sleep_state"]` and apply `_SLEEP_DECAY_MULTIPLIER`.
8. `brain/voice_commands.py` — register sleep + onboarding voice commands.
9. `apps/ava-control/src/components/OrbCanvas.tsx` — inline-extend with sleeping/waking states (or split to `SleepOrbOverlay.tsx` if diff > 500 lines).
10. `brain/face_tracking.py` (or extend existing) — temporal-filter layer for unknown-face promotion.
11. `brain/person_onboarding.py` — extend with age/gender capture, multi-frame photo capture, verification pass, trust assignment.
12. `config/onboarding.json` — tunables (persistence seconds, frames per pose, similarity threshold).
13. `docs/ROADMAP.md` — append entry under "Section 1 — Ready to ship" → mark complete after merge.
14. `docs/HISTORY.md` — append section for this work-order session.

Verification (Phase F) drives the doctor harness `inject_transcript` with voice-first audio loopback as primary, falling back to direct injection on routing issues.

---

## 8. Performance budget

| Metric | Budget | Rationale |
|---|---|---|
| ENTERING_SLEEP duration | ≤30 s | Ava should announce + finalize + write Phase 1 quickly |
| WAKING duration | ≤15 s typical | Short transition window |
| Phase 1 handoff write | 60–120 s | One LLM call, modest prompt |
| Phase 2 (per LLM call) | ~10–30 s | Pacing intentional; minimal compute pressure |
| Phase 3 handoff write | 30–60 s typical, ≤300 s hard cap | Brief summary |
| Wake-target accuracy | ±15 s | The point of on-time wake discipline |
| `cu_clipboard_paste` | ≤200 ms | Atomic clipboard op |
| Onboarding flow end-to-end | 90–180 s | 5 photo poses × ~15 s + 4 voice prompts × ~10 s + verification |
| New-person temporal filter | 10–15 s persistence | Default 12 s, tunable |
| Disambiguation question round-trip | ≤5 s after user reply | Canned templates, no LLM call needed |

---

## 9. Failure modes to watch for

- **Ava sleeps mid-conversation** because the schedule window kicked in at exactly the wrong moment. Mitigated by §1.2.3 deferral, but verify in Phase F.
- **Wake target slips by minutes** because Phase 3 hard-cap (5 min) gets hit. Self-interrupt narrates it; stays under 5 min total slip.
- **Curriculum stalls on a malformed entry** — one file with invalid metadata blocks `consolidation_hook`. Mitigate by skipping entries that fail metadata parse, logging via `state/curriculum_errors.jsonl`.
- **OrbCanvas Three.js memory leak** if sleep transitions don't dispose objects. Test by entering/leaving sleep 10 times in a row and checking GPU memory usage.
- **Temporal filter false positive** if Zeke wears a hat / glasses and recognition drops below threshold for >12 s. Mitigated by InsightFace's ~0.65 similarity threshold being lenient enough; verify in Phase F11.
- **Disambiguation question loops** if Ava's parser can't extract a clean selector from the user reply ("the one I just opened"). Mitigation: max 2 disambiguation rounds, then "I'll skip this one" graceful failure.
- **Onboarding face training fails** on dim lighting or off-angle face. Mitigated by verification-pass retry; Zeke sees a `face_recognition_quality: "low"` flag for review.

---

## 10. References

- [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) — personhood-frame discipline, sleep-mode Section 3 design seed.
- [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md) — substrate this doc extends (decay rates, calibration, self-interrupt).
- [`WINDOWS_USE_INTEGRATION.md`](WINDOWS_USE_INTEGRATION.md) — the cu_* tool layer that clipboard + close-app extend.
- [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md) — phases 5–7 the sleep-mode lessons feed into.
- [`brain/insight_face_engine.py`](../brain/insight_face_engine.py) — face recognition the temporal filter wraps.
- [`brain/trust_system.py`](../brain/trust_system.py) — trust score store.
- [`brain/person_onboarding.py`](../brain/person_onboarding.py) — existing 13-stage flow we extend.
