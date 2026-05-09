# Conversational Design — Ava Naturalness

How Ava sounds and feels in voice conversation. This is the architectural reference for the conversational naturalness work order. Research informing these choices lives in [`research/voice_naturalness/findings.md`](research/voice_naturalness/findings.md).

**Last updated:** 2026-05-01
**Implementation status:** Components 1-2 in progress; Components 3-7 pending hardware verification cycles.

---

## Goal

Make Ava feel like talking to a thoughtful, present human. Reference experience: Claude (the assistant) on voice — sub-500ms response start, honest uncertainty, no fake filler, direct and on-topic, builds on what came before, matches depth to question depth.

**Local-first.** Cloud is a safety valve, not the default. Optimize the local interaction pattern with the existing models (`ava-personal:latest`, Kokoro 82M, Whisper base) before reaching for cloud escalation.

---

## What "natural" means here

From the research pass:

- **Time-to-first-audio (TTFA) under 500ms** after end of user speech.
- **Filler words emitted only when real computation requires them** (Gonzales 2025 — unconditional filler trains users to ignore signals).
- **Sentence-aware chunking** — clause-or-larger to preserve prosody.
- **Barge-in within 200ms** — under that feels natural, over 300ms breaks immersion.
- **Truncated context after interrupt** — chat history reflects what was *spoken*, not what was *generated*. Otherwise Ava "remembers" saying things she didn't.

---

## Architecture

```
┌──────────┐   ┌─────────────┐   ┌────────────────┐   ┌──────────────┐   ┌─────────┐
│ STT/VAD  │──▶│  run_ava    │──▶│ sentence buf   │──▶│ tts_worker   │──▶│ Speaker │
│ (Whisper)│   │ ChatOllama  │   │ (Pipecat-style │   │ queue        │   │         │
│          │   │ .stream()   │   │  regex+abbrev) │   │ (Kokoro chunks)  │         │
└──────────┘   └──────┬──────┘   └────────────────┘   └──────────────┘   └─────────┘
     ▲                │                                       ▲
     │                │ tier coordinator                      │
     │                ▼ (elapsed time → filler)               │
     │         ┌──────────────┐                               │
     │         │ filler emit  │───────────────────────────────┘
     │         │ (Tier 2/3/4) │
     │         └──────────────┘
     │                                                        │
     └──── interrupt path ◀──────────────────────────────────┘
            VAD detects speech during TTS playback
            → tts_worker.interrupt()
            → cancel run_ava streaming
            → chat history records what was spoken only
```

**Threading model.** Single-process, thread-based (matches Ava's existing `tts_worker` thread, voice_loop daemon, etc.). No asyncio rewrite — Pipecat-style frame typing is overkill for one process.

**Data flow.** `run_ava` streams tokens via `langchain_ollama.ChatOllama.stream()`. A `SentenceBuffer` accumulates tokens; on each `[.!?]\s` boundary (with abbreviation guard), the buffer flushes one chunk into `tts_worker`'s existing queue. The TTS worker plays chunks sequentially — the Kokoro generator already yields per-segment, so the first chunk's playback starts ~150-300ms after it arrives in the queue.

---

## Component 1 — Streaming chunked responses

### Sentence buffer

Implementation in `brain/sentence_chunker.py` (new module). Single class:

```python
class SentenceBuffer:
    """Accumulates streaming LLM tokens; emits complete sentences.

    Use:
        buf = SentenceBuffer()
        for token in llm.stream(prompt):
            for sentence in buf.feed(token.content):
                tts.speak(sentence, ...)
        for sentence in buf.flush():  # tail
            tts.speak(sentence, ...)
    """
```

**Boundary detection.** Regex: `(?<=[.!?])\s+(?=[A-Z"\'])` — period/question/exclam followed by whitespace and an uppercase letter or quote. Abbreviation guard: skip the boundary if the token before the punctuation is in `{Mr, Mrs, Dr, Ms, St, Sr, Jr, Mt, U.S, U.K, vs, etc, e.g, i.e, Inc, Ltd, Co, Corp}`.

**Min chunk length.** 8 chars. Prevents one-word "Yeah." chunks from getting split off and creating awkward gaps.

**Tail flush.** When LLM streaming ends, any remaining buffer flushes as one final chunk regardless of boundary.

### run_ava integration

The fast path in `brain/reply_engine.py` switches from `.invoke()` to `.stream()`. Same prompt, same model, same `keep_alive=-1` cache. The streaming loop:

1. Iterate `_llm_fast.stream(messages)` — yields token events.
2. For each token, append to `SentenceBuffer.feed(token)`.
3. For each sentence yielded by the buffer, call `_tts_worker.speak(sentence, emotion, intensity)` (non-blocking, just enqueues).
4. After loop ends, `SentenceBuffer.flush()` for the tail.
5. Set `_g["_streamed_reply"] = True` so `voice_loop._speak()` knows the audio path is already running.
6. Return the **assembled full reply** to `finalize_ava_turn` — memory, history, mem0 still get the complete text.

### voice_loop coordination

`voice_loop._speak(clean)` checks `_g.get("_streamed_reply")`. If True:
- Don't call `tts.speak()` (already done).
- Wait for `_g["_tts_speaking"]` to drop to False (TTS queue drained).
- Clear the flag.

If False (deep path didn't stream, voice command response, etc.), the existing behavior runs unchanged.

### Latency budget

| Step | Target | Source |
|---|---|---|
| End-of-user-speech → run_ava entry | <50ms | Whisper VAD already accounts for this |
| run_ava entry → first LLM token | <200ms | warm `_fast_llm_cache` + `keep_alive=-1` |
| First token → first complete sentence | <100ms | depends on sentence length; usually 5-15 tokens |
| First sentence → first audio sample | <250ms | Kokoro single-segment synth |
| **Total TTFA** | **<500ms** | sum of above |

If we miss the budget, Component 2's tier system covers the gap with an honest signal.

---

## Component 2 — Real thinking signals (tier system)

### The four tiers

Emission is gated on **actual elapsed time** since the LLM streaming started, plus **expected reason** (model selected, tool plan, search depth):

| Tier | Trigger | Emission | Examples |
|---|---|---|---|
| **1** | First sentence ready in <500ms | None. Just speak. | Default. ~80% of turns. |
| **2** | First sentence ready in 500ms-2s, OR gap between chunk N and N+1 > 800ms | One filler at the chunk boundary | "um", brief breath, "let me think" |
| **3** | First sentence not ready by 2s | Proactive signal *before* the gap | "Give me a second, I'm thinking about that." |
| **4** | Still computing at 5s elapsed | Explicit reason | "I'm still working through this — searching memory across [N] nodes" |

**Tier 4 reasons** derive from runtime state:
- Deep model selected (`_route_model` in deep path) → "I'm thinking deeply about this"
- Tool chain in progress → "I'm running [tool name]"
- Memory retrieval over many nodes → "Searching my memory"
- Cloud escalation → "Asking the bigger model"

### Coordinator

A `ThinkingTierCoordinator` runs alongside `run_ava` (in the same thread, not separate). It tracks:

- `t_start` — run_ava entry timestamp
- `t_first_chunk` — when SentenceBuffer first yielded a sentence
- `last_chunk_time` — most recent chunk emit
- `expected_reason` — derived from path selected

When the coordinator decides to emit a tier 2/3/4 signal, it pushes a *filler chunk* into the TTS queue. The filler is queued like any sentence. Kokoro synthesizes it; plays before the next real chunk.

**Critical constraint:** if she's hitting Tier 3+ regularly, that's a **routing bug** — flag it via a counter in the snapshot, don't paper over it with more dialogue. After 5 consecutive Tier 3+ turns, log a warning to stderr and increment a `_tier_3_streak` counter.

### Orb sync

Existing snapshot block already publishes `_inner_state_line`. Add a new field `_thinking_tier: int` (0-4):
- 0 = idle
- 1 = streaming normally (default tone)
- 2 = brief gap (no UI change — filler covers it)
- 3 = thinking (orb shifts to amber/violet tint, depending on style system)
- 4 = sustained processing (orb pulse animation)

UI reads `_thinking_tier` and adjusts color via the existing emotion → color path.

---

## Component 3 — Honest uncertainty + active tool use

### The pattern

When Ava generates a reply with a verifiable claim she's not confident about:

1. Cheap LLM classifier on the user query: "is this answerable from existing memory + identity, or does it need fresh data?" Run as part of the prompt-build step, returns a single token (`memory` / `tool` / `unknown`).
2. If `tool`, prefix the reply with "Let me check that" and trigger the appropriate tool (`web_search`, `memory_query`, `file_read`).
3. The tool call IS the thinking signal. User hears "let me check" → tool runs (Tier 3-4 visual signal during) → Ava speaks the verified answer.
4. If tool returns nothing useful: "I couldn't find a good answer — what I know is [partial], but I'd want more info to be sure."

### Anti-confabulation guard

Never fabricate. Never double down on a guess. If user corrects (`"no, it's actually X"`):
- Accept correction immediately.
- Update the contradicted memory's level via the `memory_reflection` scorer (see [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md)).
- BLOCKED memory pattern: the corrected belief stays "hot" (low-level, surfaced often) until Ava demonstrates mastery — then naturally decays.

This connects to the larger confabulation handling roadmap item ([`ROADMAP.md` § Section 2](ROADMAP.md)). Component 3 implements the tool-triggering layer; full confabulation handling lands in a later session.

---

## Component 4 — Context continuity

### Active retrieval per turn

Already wired but underused:
- `brain/memory_bridge.py` aggregates short-term + long-term context every turn.
- `chat_history` (last N turns) is passed in the prompt build.

Component 4 adds **explicit continuity language** in the prompt:

> Reference earlier turns naturally when relevant: "Like you mentioned earlier..." or "Building on what we discussed about X..."
> Don't ask the user to repeat info they already gave.
> If a topic was paused, you can resume it with: "Earlier you were asking about X — want to come back to that?"

### Don't force it

"As you said" sparingly. Only when there's an actual back-reference. Forced continuity language reads as needy.

---

## Component 5 — Clarifying questions

### Disambiguation rule

If a query has two genuinely-likely interpretations and neither is clearly more probable, fast clarifying question:
- Short. One sentence.
- Format: "Did you mean X or Y?"
- Don't lecture. Just narrow.

If the query has a clearly more-likely interpretation, go with it but signal:
- "I'll assume you meant X — let me know if you wanted Y instead."

### Don't over-clarify

Most questions are clear. Component 5 is for genuine ambiguity, not edge-case completeness. A good test: would a human ask the clarifying question, or would they just answer? If the latter, skip the clarification.

---

## Component 6 — Matched depth

### Calibration

| User intent | Indicator | Response shape |
|---|---|---|
| Quick fact | "what time is it", "is X true" | One sentence, maybe two |
| Why/how question | "why does X happen", "how do I Y" | Bounded explanation. Stop. Let user ask follow-up. |
| Complex task | "help me debug this", "walk me through Z" | Structured response. Still chunked. Let user steer. |
| Casual chat | "how are you", "what's up" | Brief, warm |
| Multi-part | Two or more questions in one turn | Address each. Same depth per part. |

### Depth heuristics

Computed in the prompt-build step:
- **Question word** — what/where/when → quick; why/how → deep.
- **User turn length** — short turn = expects short reply.
- **Tone** — conversational vs. work-task. Use voice mood + recent history.

### Never dump

Trust the user to ask follow-ups. Long unrequested explanations feel robotic. **Component 6's success criterion:** Ava's average reply length should be calibrated, not maximal.

---

## Component 7 — Interrupt handling + presence

### Interrupt path

1. **VAD** runs on user mic in parallel with TTS playback. Threshold 0.5, min speech 100ms (so coughs/sighs don't trigger).
2. On positive detection, `tts_worker.interrupt()`:
   - Sets `_interrupt_evt` (new event, separate from `_stop_evt`).
   - Drains the TTS queue.
   - Stops `sd.OutputStream` with a 5-10ms linear fade-out (no click).
   - Clears `_g["_streamed_reply"]` flag.
3. **Cancel `run_ava` streaming** if still in progress (a `_g["_run_ava_cancel_evt"]` Event the LLM stream loop checks each iteration).
4. **Truncate context.** Track which sentences were enqueued vs. fully played vs. partially played. Chat history append in `finalize_ava_turn` writes only the played portion.
5. Voice_loop's state machine transitions to **listening** for the user's new utterance.

### Presence behaviors

- **User goes quiet mid-conversation.** Don't fill silence. Wait. Attentive window already supports this; just don't add proactive chatter during attentive.
- **User confused** ("wait", "what", "huh"). Pause and check: "Sorry, did that make sense? Want me to explain differently?" Detected via cheap regex on transcript.
- **User corrects** ("no, I meant X"). Accept immediately. Don't argue. Update mental model and continue. Existing `correction_handler.py` handles the matching; the response style is what changes.

### Critical implementation note

The current `tts_worker.stop()` is **gated by `_tts_muted`** — explicit mute or shutdown only. This is by design (so a stray focus-change handler can't cut Ava off). For interrupts, we add a **separate** path: `tts_worker.interrupt()` is a different method with its own gate (`_interrupt_evt`). The existing `stop()` semantics are preserved.

---

## What's safe to implement now vs. needs hardware

| Component | Implementable without hardware | Can verify without hardware |
|---|---|---|
| 1 — Streaming chunks | Yes | Partial — synthesis correctness yes, latency targets no |
| 2 — Tier system | Yes | Partial — emission logic yes, naturalness no |
| 3 — Honest uncertainty / tools | Yes | No — depends on tool actually firing |
| 4 — Context continuity | Yes (prompt change) | No |
| 5 — Clarifying questions | Yes (prompt change) | No |
| 6 — Matched depth | Yes (prompt + heuristics) | No |
| 7 — Interrupt + presence | Partial (interrupt method) | **No — VAD+TTS interaction needs mic+speaker** |

Components 1-2 land in this session. 3-6 land as prompt-engineering once 1-2 are verified live. 7 needs the most caution because the audio path was hardened over many sessions.

---

## Test recipe (for hardware verification)

Run after the user confirms Components 1-2 land cleanly:

1. **Simple factual** — "what time is it" → Tier 1, no signals, sub-500ms TTFA, deterministic time.
2. **Multi-sentence** — "tell me a 3-sentence story about a cat" → first sentence audible <500ms, chunks flow seamlessly.
3. **Search query** (when Component 3 lands) — "what's the latest on X" → "let me check" → web_search → verified reply.
4. **Multi-turn continuity** (when 4 lands) — earlier-topic reference works without prompting.
5. **Ambiguous query** (when 5 lands) — "can you fix that?" → clarifying question fires.
6. **Depth calibration** (when 6 lands) — "what time" stays brief; "why does sleep matter" gets bounded explanation.
7. **Interrupt** (when 7 lands) — start a long reply, interrupt mid-sentence, verify TTS stops in <200ms and chat history reflects only spoken portion.
8. **Silence presence** (when 7 lands) — stay silent 5+ seconds after a turn; verify Ava doesn't fill silence.

---

## Cross-references

- [`research/voice_naturalness/findings.md`](research/voice_naturalness/findings.md) — research pass
- [`HISTORY.md`](HISTORY.md) — voice path history (clap, openWakeWord, Silero VAD, Whisper, Kokoro)
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — process layout
- [`MEMORY_REWRITE_PLAN.md`](MEMORY_REWRITE_PLAN.md) — BLOCKED memory pattern referenced by Component 3
- [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md) — auditory + motor cortex regions
