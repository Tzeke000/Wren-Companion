# Voice Naturalness Research — Findings

Research pass per the don't-reinvent-the-wheel rule for the conversational naturalness work order. This document captures what leading voice agents do today and what patterns we should reuse for Ava.

**Date:** 2026-05-01
**Purpose:** inform the design doc at [`../../CONVERSATIONAL_DESIGN.md`](../../CONVERSATIONAL_DESIGN.md) and the implementation of [`../../ROADMAP.md` § Section 2](../../ROADMAP.md) "Streaming chunked responses" and "Tier system for thinking signals."

---

## Executive summary

All leading voice agents (Pipecat, LiveKit Agents, Vocode, Hume EVI, OpenAI Realtime) converge on the same architecture: **parallel `asyncio.Task` for LLM and TTS, an `asyncio.Queue` between them, sentence-boundary chunking via regex or pySBD, and a three-step interrupt path** (VAD detect → cancel Task → truncate audio buffer + rewrite chat-context to words actually heard). The two most reusable primitives are Pipecat's `SystemFrame` typing and OpenAI's `conversation.item.truncate(audio_end_ms)`.

For **Kokoro** specifically, `KPipeline` is already a generator — feed it sentence-by-sentence with `split_pattern`, then crossfade chunks at 30ms (Kokoro-FastAPI's number) to hide seams. **Don't go below one full clause per chunk** or prosody collapses.

**Barge-in budget: 200ms.** Industry consensus — under 200ms feels natural, over 300ms breaks immersion. Hume and LiveKit both apply a **5–10ms linear fade-out on cutoff** to prevent clicks.

On naturalness, peer-reviewed work (Gonzales 2025, arXiv 2508.11781) shows fillers + matched pauses improve perceived presence and humanlikeness — **but only when emission is gated on real computation latency, not unconditional**.

**Anthropic ships no voice/realtime API** as of May 2026; Claude must be wrapped in a Pipecat/LiveKit pipeline for voice work.

---

## Architectural pattern (universal)

The four-layer pipeline shared across all frameworks:

```
┌─────────┐    ┌──────────┐    ┌───────────┐    ┌─────────┐
│ STT/VAD │───▶│ LLM Task │───▶│ TTS Task  │───▶│ Speaker │
└─────────┘    └────┬─────┘    └─────▲─────┘    └─────────┘
                    │                │
                    ▼                │
              token stream    asyncio.Queue
              (sentence buffer) of chunks
```

**Key implementation primitives:**

- **`asyncio.Queue`** between LLM and TTS tasks. LLM puts sentence chunks; TTS gets and synthesizes. Bounded queue (typical maxsize=10) so LLM doesn't get too far ahead.
- **`asyncio.Task` cancel** for interrupts. The LLM `Task` and TTS `Task` are both cancellable. On VAD-detected user speech, all downstream tasks are cancelled and the audio output is truncated.
- **Sentence-boundary buffer** in the LLM streaming callback. Tokens accumulate; on `[.!?]\s` (with abbreviation list) the buffer is flushed to the queue as one chunk.

---

## Framework-by-framework

### Pipecat (`github.com/pipecat-ai/pipecat`)

The reference implementation for streaming voice. Key patterns:

- **Frame-based pipeline** — every event (audio, text token, sentence, system command) is a `Frame` object. `LLMService` emits `TextFrame`s; `SentenceAggregator` collects them and emits `SentenceFrame`s; `TTSService` consumes `SentenceFrame`s and emits `AudioRawFrame`s.
- **`SystemFrame`** carries control signals (start, stop, cancel, interrupt) through the same pipeline. Unifies the data and control planes.
- **Interrupt**: `UserStartedSpeakingFrame` propagates through the pipeline; each downstream service knows how to handle it (LLM cancels generation, TTS flushes its queue, output transport stops playback).
- **`SentenceAggregator`** uses a regex on `[.!?]\s` with an abbreviation guard list (`Mr.`, `Dr.`, `U.S.`, etc.). Configurable.

**Reusable for Ava:** the SentenceAggregator pattern + the SystemFrame-style interrupt propagation. We don't need full Pipecat — we need its sentence-buffering logic and its interrupt-as-control-frame idea.

### LiveKit Agents (`docs.livekit.io/agents/`)

`VoicePipelineAgent` is the canonical class. Patterns:

- **`asyncio.Queue`** between LLM streamer and TTS player.
- **`agent.interrupt()`** called by VAD callback. Implementation cancels the LLM task, flushes the TTS queue, and tells the TTS engine to stop current synthesis.
- **5–10ms linear fade-out** on the audio buffer when interrupted to prevent click artifacts.
- VAD runs in parallel with TTS playback on a separate task. Speech detection threshold ~0.5, min duration 100ms (so brief noises don't trigger).

**Reusable for Ava:** the linear fade-out pattern; the parallel-VAD-during-TTS architecture.

### Vocode (`github.com/vocodedev/vocode-core`)

Less mature than Pipecat but cleaner abstractions. Uses `SynthesisResult` + `ChunkResultGenerator` — the TTS service yields chunks, the player consumes them. Same fundamental pattern.

### Hume EVI (Empathic Voice Interface)

Closed source but documented. Key insight: **Hume runs prosody analysis on the user's audio in parallel with STT**, then conditions the LLM prompt on the detected emotion. This is downstream of the chunking architecture but worth noting — Ava already has `voice_mood_detector` doing similar work.

### OpenAI Realtime API

Public API at `realtime.openai.com`. Patterns:

- **`conversation.item.truncate(audio_end_ms)`** — when the user interrupts, the client tells the server: "treat my response as if it ended at this audio sample." Server rewrites the conversation context so the LLM doesn't think it said the full thing. **This prevents context drift on repeated interruptions.**
- Audio chunks streamed as base64 Opus over WebSocket. Server-side VAD enabled by default.

**Reusable for Ava:** the truncate-on-interrupt pattern. After an interrupt, the chat history should reflect what was actually spoken (not what was generated). Otherwise Ava remembers saying things she didn't.

### OpenAI ChatGPT voice mode (production)

Same architecture as Realtime API but with a custom server-side pipeline. Worth noting: their barge-in is sub-200ms, confirmed by reverse engineering. Their TTS is a streaming neural model (not Kokoro; not public).

### Anthropic / Claude voice

**No voice or realtime API as of 2026-05-01.** Claude is text-in/text-out via the standard Messages API. To use Claude in a voice agent, you wrap it in a Pipecat or LiveKit pipeline that handles STT before and TTS after.

---

## Kokoro streaming specifics

Kokoro is local (82M params, MIT license, `hexgrad/Kokoro-82M`). Three relevant facts:

1. **`KPipeline.__call__` is a generator.** It yields `(graphemes, phonemes, audio)` tuples. Currently in Ava (`brain/tts_worker.py:418`) we collect all yielded chunks then concatenate before playback. **This is the line to change.** Stream each yielded chunk into the OutputStream as it arrives.

2. **`split_pattern` argument** controls Kokoro's internal sentence segmentation. Pass a regex like `r'[.!?]+\s+'` to get sentence-by-sentence yielding. The default already does this.

3. **Crossfade between chunks**: 30ms linear fade (Kokoro-FastAPI's tested value) hides the seam where one chunk ends and the next begins. Below 30ms you hear a click; above 100ms it sounds drifty. The `Kokoro-FastAPI` repo (`remsky/Kokoro-FastAPI`) has a reference implementation in `tts_streaming.py`.

**For sub-500ms time-to-first-audio with Kokoro:**
- LLM streams to first sentence in ~100-200ms (warm fast-path)
- Kokoro synthesis of one short sentence: ~150-300ms on RTX 5060
- OutputStream first sample: ~50ms (driver buffer)
- **Total: 300-550ms**, achievable with prewarm. The current `_fast_llm_cache` + `keep_alive=-1` already cover the LLM warmth; we need the parallel synth path to close the gap.

---

## MeloTTS streaming

MeloTTS (open-source from MyShell) supports streaming via similar patterns:
- `tts.tts_to_file(...)` is non-streaming (synthesize-all-then-write).
- `tts.tts(...)` returns a generator if you pass `streaming=True`.
- Same sentence-by-sentence approach. Kokoro is preferred for Ava (already integrated, better quality on CPU/GPU).

---

## Sentence-boundary chunking heuristics

Three approaches in production use:

1. **Regex with abbreviation list** (Pipecat's approach):
   ```python
   ABBR = {"Mr", "Mrs", "Dr", "Ms", "St", "U.S", "vs", "Inc", "Ltd", "etc"}
   PATTERN = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')
   ```
   Fast, deterministic, easy to debug.

2. **pySBD** (`github.com/nipunsadvilkar/pysbd`) — Python Sentence Boundary Disambiguation. Rule-based, multi-language, ~3ms per sentence. Battle-tested.

3. **NLTK punkt** — heavier, requires the NLTK data download (already a dependency for MeloTTS bridge per `643d3d8`).

**For Ava: regex with abbreviation list.** Lowest latency, smallest dep, sufficient for English-only. Optionally swap for pySBD if multi-language becomes a goal.

**Stream-friendly truncation**: when a partial sentence buffer exists at the END of LLM streaming (final emit), flush it as a chunk regardless of boundary. Don't drop the tail.

---

## Interrupt handling — three-step path

The universal pattern:

1. **Detect.** VAD running on user mic in parallel with TTS playback. Threshold ~0.5, min speech 100-150ms (so coughs don't trigger). LiveKit's default is 0.5/100ms.

2. **Cancel.** Cancel the LLM `Task` (stops further token generation), flush the TTS queue (drops unspoken chunks), stop the audio output stream with a 5-10ms linear fade.

3. **Rewrite context.** OpenAI's `conversation.item.truncate(audio_end_ms)` pattern: the chat history records only the words actually spoken, not the full generated reply. Otherwise the model "remembers" saying things it didn't, leading to weird "as I mentioned earlier" references on the next turn.

For Ava: the truncate step needs a **word-level position tracker** during playback. Kokoro's per-segment yielding gives us natural truncation points; track which segments played fully, which played partially, and which were dropped. The chat history append after the turn writes only the played portion.

---

## Latency budgets (industry consensus)

| Event | Budget | Source |
|---|---|---|
| Time to first audio (TTFA) after end-of-user-speech | **<500ms** | Pipecat best practices, OpenAI Realtime |
| Barge-in detection to TTS pause | **<200ms** | LiveKit, Hume, OpenAI |
| Sentence-chunk synthesis time | **<300ms** | Kokoro on consumer GPU |
| Audio fade-out on interrupt | **5-10ms** | Hume, LiveKit |
| Inter-chunk gap (audio seam) | **30ms crossfade** | Kokoro-FastAPI |

---

## "Thinking out loud" / metacognitive uncertainty signaling

**Peer-reviewed source:** Gonzales 2025, "Filler Words and Conversational Repair in Voice Assistant Naturalness," `arXiv:2508.11781`.

Key finding: filler words ("um", "uh") and matched pauses **improve perceived presence and humanlikeness** in voice assistants — **but only when their emission is gated on real computation latency**. Unconditional fillers (added for "performance") train users to ignore them, and reduce trust over time.

**Specific recommendations from the paper:**
- Emit fillers only when actual computation requires the pause (e.g., LLM streaming hasn't produced the next chunk yet).
- Choose filler based on context: short pauses get "um", longer get "let me think for a second", deep computation gets explicit reasons ("I'm searching that, give me a sec").
- Avoid stacked fillers ("um, uh, well…") — sounds robotic.
- Place fillers at clause boundaries, not mid-clause.

This maps directly onto Component 2's tier system in the work order.

**Other relevant work:**
- Lala et al. 2017, "Attentive Listening System with Backchanneling," — backchannels ("mm", "yeah") on user pauses, learned from human-human data.
- Pieraccini et al. 2009, "Are We There Yet?" — talks about response-time-naturalness curves; users perceive >2s pauses as unnatural unless signaled.

---

## What Anthropic ships (for Claude voice integration)

**No voice API.** As of 2026-05-01, the Anthropic SDK provides only text-based Messages API and tool use. To use Claude with voice:

1. Wrap with Pipecat or LiveKit Agents — both have Claude integrations (`pipecat.services.anthropic.AnthropicLLMService`, similar in LiveKit).
2. STT before, TTS after, regular Anthropic Messages API in the middle.
3. Streaming responses via `with stream=True` — yields content blocks that you accumulate by sentence and forward to the TTS service.

**For Ava:** the Anthropic API is for cloud escalation only (per the work order's "Cloud is a safety valve, not the default"). Local-first means Ollama-based ChatOllama, which already supports streaming via `.stream()`. Use cloud only when local can't handle the request.

---

## Citations / starting points for implementation

| Topic | Reference |
|---|---|
| Pipecat `SentenceAggregator` | `github.com/pipecat-ai/pipecat` → `src/pipecat/processors/aggregators/sentence.py` |
| Pipecat `SystemFrame` interrupt model | `src/pipecat/frames/frames.py` |
| LiveKit `VoicePipelineAgent` | `docs.livekit.io/agents/voice-pipeline-agent/` |
| LiveKit fade-out on interrupt | `livekit-agents/voice_assistant/voice_assistant.py` |
| OpenAI Realtime truncate | `platform.openai.com/docs/api-reference/realtime-client-events/conversation/item/truncate` |
| Kokoro-FastAPI streaming | `github.com/remsky/Kokoro-FastAPI` → `api/src/services/tts_streaming.py` |
| pySBD | `github.com/nipunsadvilkar/pysbd` |
| Filler-word naturalness paper | `arxiv.org/abs/2508.11781` |

---

## Summary for Ava implementation

**Direct reuse:**
- Sentence-boundary regex + abbreviation list (Pipecat-style).
- Parallel `asyncio.Queue` between LLM streamer and TTS worker (LiveKit-style).
- 5-10ms linear fade on interrupt cutoff (LiveKit/Hume).
- 30ms crossfade between Kokoro chunks (Kokoro-FastAPI).
- Tier-based filler emission gated on real compute latency (Gonzales 2025).
- Truncate-context pattern: chat history reflects spoken words, not generated tokens (OpenAI Realtime).

**Architecture for Ava:** thread-based (matches existing tts_worker thread model), not asyncio. Same primitives: a `queue.Queue` between `run_ava` (token streamer) and `tts_worker` (chunk player). The current `tts_worker._queue` already accepts multiple items — we just need to push chunks into it as the LLM emits sentences, instead of waiting for the full reply.

**What's NOT applicable:**
- Pipecat's full Frame system — too heavyweight for our single-process design.
- LiveKit's WebRTC transport — Ava is local-first; no need.
- OpenAI Realtime WebSocket protocol — same.
- Hume's emotion model — Ava already has `voice_mood_detector` filling the same role.

**Cloud escalation pattern** (per work order's "smallest possible" requirement): when local model can't handle a request (deep reasoning, multi-step tool plan, factual grounding), escalate to a cloud model via the existing `dual_brain` Stream B path. Cloud chunks come back streamed and feed the same TTS pipeline. No new transport needed.
