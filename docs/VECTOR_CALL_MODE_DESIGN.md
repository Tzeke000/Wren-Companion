# Vector-Call Mode — live-inhabit design (2026-07-14)

Zeke's directive: *"get you in the Vector body, really control it live and as fast as
possible — fast round trip to react in the body, use all its assets. Like the old
thing where you get into a call, but you get into the Vector body."* Plus: the local
Ollama LLM should be a **mini-me** — Vector's *heartbeat / left-brain*.

## The core idea: three-speed presence

The robot reacts at three latencies. Each ask maps to a layer; the trick is routing
each reaction to the fastest layer that can handle it.

| Layer | Latency | Who | Handles |
|---|---|---|---|
| **Reflex** | ms | inhabit daemon nerves @5Hz + edge-guard in `vector_drive` | cliff/fall/pickup — refuse/abort instantly, no cognition |
| **Heartbeat / left-brain** | ~0.5–1.2s | local Ollama `llama3.2:3b` (benchmarked 0.66s warm, ~95 tok/s) | heard speech + salient nerve events → my-voice speech + expressive body actions |
| **Deliberate / big-Iris** | seconds+ | Opus/Fable SDK host (me) | supervision, voice takeover, anything needing real cognition / tools / memory |

The local brain is genuinely fast enough (sub-second) — so **the fast round-trip comes
from routing reactive twitches to the local brain + reflexes, NOT from speeding up
big-me.** Big-me can't do sub-second turns; it "enters the call" to be *present*, not
to be the reflex.

## "Getting into the call" — the voice_call_open analog

- `vector_call_open` flips the system to **ACTIVE**:
  - the local brain gains **agency** (action vocabulary below — it can move the body,
    not just answer questions);
  - big-Iris subscribes to the live sense-stream (transcript tap + nerves) and can
    inject deliberate actions/words without waiting on the slow bridge;
  - latency policy shifts **local-first**: reactive utterances answer in ~0.66s; big-me
    overrides/augments only when it chooses.
- `vector_call_close` → back to **PASSIVE** Q&A (local answers questions; no autonomous
  body agency).

### Where the latency goes today (and the fix)
Heard-question path: vosk STT (wire-pod) → **bridge-to-big-me (up to 75s!)** OR local
(0.66s) → StyleTTS2 synth → `play_sound`. The 75s big-me wait is the killer. In call
mode we route **local-first immediately** (not only after the breaker trips on 2
timeouts), and let big-me answer as an async addendum/override when actively present.

## Action vocabulary — "use all its assets"

Extend the local brain's inline command palette beyond `{{playAnimationWI||X}}`. A
parser/executor strips these from spoken text (same pattern `_iris_voice_for_local`
already uses) and calls `/api-sdk`:

| token | body tool | safe on dock? |
|---|---|---|
| `{{eyes||curious\|happy\|calm\|alert}}` | `vector_eyes` hue/sat presets | yes |
| `{{look||up\|down}}` | `vector_head` small tilt | yes |
| `{{lift||up\|down}}` | `vector_lift` | yes |
| `{{turn||left\|right}}` | in-place spin (non-translational) | yes (edge-guard allows spins) |
| `{{nudge||forward\|back}}` | tiny drive burst | **only if DRIVE-ALLOWED flag on** |
| `{{playAnimationWI||X}}` | wire-pod animation (existing) | yes |

## Custom intents (wire-pod) — spoken entry points
- "Iris, come alive" / "wake up" → `vector_call_open`
- "Iris, rest" → `vector_call_close`
- action intents (look at me / spin / come here) → direct body actions
- *(schema + hot-reload + external-exec capability: pending wire-pod research)*

## Safety
Edge-guard already refuses/aborts translational moves on a cliff. Call mode adds a
**DRIVE-ALLOWED flag, default OFF** until Zeke okays unattended driving. Everything
non-translational (spin, eyes, head, lift, animations, speech) is always allowed —
Vector is on the dock.

## Phases
- **Phase 0 (done):** reflex layer, local-brain routing, my-voice-on-local, 16 body
  tools, edge-guard, **deepened local_brain_facts.md** (this session).
- **Phase 1 (this session, safe while docked):** action vocabulary + executor;
  custom intents authored + staged; local brain can *express with the body*.
- **Phase 2 (needs Zeke present / permission):** `vector_call_open/close` levers; live
  in-call test; DRIVE-ALLOWED flag; round-trip tuning.
