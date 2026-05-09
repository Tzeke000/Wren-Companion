# Local Model Optimization

**Date:** 2026-05-01 (updated 2026-05-01 evening with clean post-Ava-stop bench)
**Hardware:** Acer Nitro V 16S — RTX 5060 Laptop (8 GB VRAM), 32 GB system RAM
**Constraint:** Ava must run fully local. No cloud calls in the foreground reply path.

This doc maps the current local-model surface, benchmarks the strongest 7-8B candidates that fit in 8 GB VRAM, audits the prompt scaffolding and the architectural compensation layer, and recommends concrete changes.

---

## ⚠️ Top-level VRAM constraint (load-bearing)

**On 8 GB VRAM, only ONE generation model OR `llava` (vision) can be hot at any moment.**

Empirically confirmed in the clean post-Ava-stop bench (§5):
- `llava:13b` is 11 GB on disk and runs at a forced 46 % CPU / 54 % GPU split when held resident, occupying ~7.4 GB of the 8 GB GPU.
- Any 4-7 GB Q4 generation model + resident `llava` exceeds VRAM, forcing Ollama into per-call paging — single-prompt cold loads stretched from ~3 s (clean fit) to 26-90 s (paged) under contention.
- Two concurrent generation models (e.g. `ava-personal` 4.9 GB + `deepseek-r1:8b` 5.2 GB = 10.1 GB) also exceed 8 GB. They cannot both stay hot.

**Implications for upstream subsystems** (each a derived constraint to design around — flagged in `ROADMAP.md`):

1. **Sleep mode + handoff system.** "Dream phase" cannot run an LLM curriculum while the foreground voice model is hot. Sleep entry must (a) wait for `_conversation_active = False`, (b) explicitly unload the foreground model via `keep_alive: 0`, (c) load the dream-phase model, (d) reverse on wake. Any in-flight voice turn during the unload window will pay full reload cost.
2. **Vision activation.** Activating `llava` while a generation model is resident forces the generation model out and the next user turn pays a 30-90 s reload. The vision pipeline must either (a) batch vision requests into windows that don't overlap a turn, (b) accept the cost as a known budget, or (c) move to a smaller vision model (e.g. quantized SmolVLM, MiniCPM-V) that fits alongside an 8 B generation model.
3. **Dual-brain architecture.** The current design assumes Stream A and Stream B can both stay resident. They can't — at minimum one must context-switch on each background thread cycle. Practical patterns: (a) keep ONE model resident, switch only when the background thread actually fires (single-resident model with periodic rotation); (b) run the background reasoning step as a synchronous post-foreground call ("R1 thinks → ava-personal replies" sandwich) so each turn pays one swap, not two.
4. **Cold-boot vs. warm latency.** A user who walks up to a cold machine pays ~5 s for the first model load on top of the ~3-min Ava cold-boot. Subsequent turns are sub-2 s as long as no swap happens. Anything that triggers a swap (vision frame, sleep wake, background-thread cycle) drops a turn back to cold-load latency.

This constraint is the single most important fact about Ava on this hardware. Most architectural decisions downstream of "Ava is running locally on Zeke's laptop" inherit from it.

---

## TL;DR

1. **The dual-brain is currently configured to prefer models that don't fit in VRAM.** `dual_brain.py` prefers `ava-gemma4` (9.6 GB) for foreground and `gemma4:latest` (9.6 GB) for background. The 8 GB VRAM ceiling means both get partially evicted to system RAM. **Recommended fix: drop ava-gemma4/gemma4 from the preference list and prefer `ava-personal:latest` (4.9 GB Llama 3.1 8B fine-tune) for foreground and `deepseek-r1:8b` (5.2 GB Qwen3 8B reasoning distill) for background.**
2. **Best general-reasoning model:** `deepseek-r1:8b`. In the clean post-Ava-stop bench it was the **only** model that caught both reasoning failures `ava-personal` makes — transitivity *and* the "month with letter X" trick (ava said "December", qwen3.5 said "October", R1 said "no month does"). Verified on 11/11 prompts (vs partial earlier).
3. **Critical caveat — R1 hallucinates factual claims it can't verify.** Asked "Apple closing price yesterday?", R1 confidently fabricated "$191.94 on October 25, 2023" — wrong number, outdated date. ava-personal correctly punted to web sources. **Reasoning capability does not protect against confabulation. Wiring R1 in without a verification gate would trade one failure mode for another.** This is the most important finding in this audit.
4. **Best foreground (persona + naturalness):** keep `ava-personal:latest`. The matched-depth fine-tune is doing real work — when asked an ambiguous emotional prompt ("partner is mad and I don't know why") ava asked the right clarifying question while R1 and qwen3.5 dumped 1 000+-token structured advice without checking what the user needed. Persona is not stylistic decoration; it is matched-depth behavior baked into the weights.
5. **qwen3.5:latest is dominated** — slower than both candidates, less reasoning-correct than R1, less natural than ava-personal, and larger on disk (6.6 GB) on a tight 8 GB GPU. No role in the recommended dual-brain.
6. **Prompt scaffolding is mostly solid** — fast-path naturalness clauses in `reply_engine.py:298` cover matched depth, context continuity, honest uncertainty, clarifying questions, and boundary awareness. **Two improvements applied:** chain-of-thought + wrong-premise guard in fast path; matched-depth + premise-guard mirrored into deep-path SYSTEM_PROMPT.
7. **Architectural compensation is partially wired.** Deep-path retrieval is heavy (vector + reflections + concept graph + episodic + memory bridge); fast-path is purely generative — no fact-grounding step. `validity_check.py` Layer 1 is written but **not wired**. Three architectural improvements proposed for Zeke's review (§7).

---

## 1. Hardware reality check

```
NVIDIA GeForce RTX 5060 Laptop GPU — 8 GB VRAM total, ~7.4 GB usable
during nominal load (driver + Tauri overhead consume the rest).
```

Practical sizing on Q4_K_M:

| Class | Q4 size | Fits with headroom for embedding + vision? |
|---|---|---|
| 7-8 B | ~4-5 GB | yes |
| 9-10 B | ~5-7 GB | tight, viable |
| 12-14 B | ~8-10 GB | **no** — pages to system RAM |
| 30-32 B | ~17-19 GB | no |

The benchmark traces below confirmed this empirically: any 9.6 GB model on this machine triggers a 46 % CPU / 54 % GPU split with sustained eviction. Model load time stretches from ~3 s (clean fit) to 26-90 s (paged) per cold load.

---

## 2. Currently downloaded models

`ollama list` enumerated 14 tags, of which 13 are generation models (one is `nomic-embed-text` for embeddings). Sizes from `ollama show`:

| Tag | Arch | Params | Quant | Ctx | Tools | Thinking | Disk | Fits 8 GB cleanly? |
|---|---|---|---|---|---|---|---|---|
| `ava-personal:latest` | llama (3.1) | 8.0 B | Q4_K_M | 131 k | ✓ | — | 4.9 GB | yes |
| `llama3.1:8b` | llama (3.1) | 8.0 B | Q4_K_M | 131 k | ✓ | — | 4.9 GB | yes |
| `mistral:7b` | mistral | 7 B | Q4 | 32 k | — | — | 4.4 GB | yes |
| `deepseek-r1:8b` | qwen3 | 8.2 B | Q4_K_M | 131 k | — | ✓ | 5.2 GB | yes |
| `gemma2:9b` | gemma2 | 9.2 B | Q4_0 | 8 k | — | — | 5.4 GB | yes |
| `qwen3.5:latest` | qwen35 | 9.7 B | Q4_K_M | 262 k | ✓ | ✓ | 6.6 GB | yes (tight) |
| `ava-gemma4:latest` | gemma4 | 8.0 B | Q4_K_M | 131 k | ✓ | ✓ | 9.6 GB | **no** |
| `gemma4:latest` | gemma4 | 8.0 B | Q4_K_M | 131 k | ✓ | ✓ | 9.6 GB | **no** |
| `deepseek-r1:14b` | — | 14 B | Q4 | — | — | ✓ | 9.0 GB | **no** |
| `qwen2.5:14b` | qwen2 | 14 B | Q4 | — | — | — | 9.0 GB | **no** |
| `llava:13b` | llava | 13 B | Q4 | 4 k | — | — | 8.0 GB | tight (vision) |
| `mistral-small3.2:latest` | mistral | — | Q4 | — | — | — | 15 GB | **no** |
| `qwen2.5:32b` | qwen2 | 32 B | Q4 | — | — | — | 19 GB | **no** |
| `nomic-embed-text:latest` | bert | — | f16 | — | — | — | 274 MB | (embedding) |

`ava-gemma4` is interesting — its parameter count (8 B) suggests it should fit, but the gemma4 architecture bundles vision + audio capability layers that inflate the actual VRAM footprint to 9.6 GB. **The "8 B" headline number is misleading on the gemma4 family.**

---

## 3. Current dual-brain configuration (and the bug)

`brain/dual_brain.py:41-50`:

```python
FOREGROUND_MODEL_PREFERRED = "ava-gemma4"            # 9.6 GB — does not fit
FOREGROUND_MODEL_FALLBACK  = "ava-personal:latest"   # 4.9 GB — fits

BACKGROUND_MODEL_LOCAL    = "gemma4:latest"          # 9.6 GB — does not fit
BACKGROUND_MODEL_FALLBACK = "qwen2.5:14b"            # 9.0 GB — does not fit
BACKGROUND_MODEL_CLOUD    = "kimi-k2.6:cloud"
```

Both preferred AND local-fallback for the background path exceed VRAM. The only path that lands cleanly today is `ava-personal` for foreground when `ava-gemma4` happens to be evicted, and the cloud fallback for the background.

This is the single highest-impact change in this audit.

**Recommended new config:**

```python
FOREGROUND_MODEL_PREFERRED = "ava-personal:latest"   # 4.9 GB — fits, fine-tuned
FOREGROUND_MODEL_FALLBACK  = "llama3.1:8b"           # 4.9 GB — fits, control

BACKGROUND_MODEL_LOCAL    = "deepseek-r1:8b"         # 5.2 GB — fits, reasoning
BACKGROUND_MODEL_FALLBACK = "qwen3.5:latest"         # 6.6 GB — fits (tight), thinking + tools
BACKGROUND_MODEL_CLOUD    = "kimi-k2.6:cloud"        # unchanged
```

This is the change most likely to produce a felt improvement in steady-state response latency. It is **not applied in this commit** — it changes Ava's runtime behavior and Zeke should sign off given the persona implications (ava-gemma4 has a custom system prompt baked into the modelfile that ava-personal also has but with a different fine-tune approach).

---

## 4. Web research — current best 7-8 B reasoning models (May 2026)

Per leaderboards and community ranking summaries (Open Source LLM Leaderboard, HuggingFace blog, Latent.Space "Top Local Models List April 2026", Onyx AI self-hosted leaderboard):

- **DeepSeek R1** (and its 8 B Qwen3-distilled variant) is the consensus leader for *reasoning* in this size class. Chain-of-thought is built into the inference path; it shows its work in `<think>` tags before the final answer. Strong on multi-step math, logical implication, and trick-question detection.
- **Qwen 3.x** (the qwen35 architecture in `qwen3.5:latest`) introduced a dual-mode (think/non-think) toggle and is competitive on raw capability while supporting tools natively.
- **Llama 3.1 8B** remains the strongest general-purpose baseline with the broadest tooling ecosystem; this is what ava-personal is built on.
- **Phi-4** scores higher on MATH (80.4 %) than any 8 B class model but is itself 14 B — won't fit in 8 GB. The 4 B `phi-4-mini` is interesting but reportedly weaker on conversational naturalness.
- **Gemma 2 9B** is solid but older (2024 release) and limited to 8 k context.
- **Mistral 7B** is the long-time community baseline but has been overtaken on most reasoning tasks by Llama 3.1 and the Qwen line.

The relevant gap on Zeke's machine: nothing in the **DeepSeek R1 / reasoning-distilled** category was being used in the dual-brain — `deepseek-r1:8b` is downloaded but not referenced anywhere in the brain modules. Wiring it as the background reasoning model is the lever this work order asks about.

---

## 5. Benchmark — pragmatic 11-prompt qualitative eval

**Method.** No pre-built local benchmark fits cleanly (the `lm-evaluation-harness` family targets HF Transformers, not Ollama; Ollama community benchmarks are mostly per-prompt latency, not quality). Per the work order's "speed matters more than rigor — we're picking a primary model, not publishing a paper" guidance, an 11-prompt suite was assembled:

- 3 reasoning (logical implication, math word problem, trick question)
- 2 factual recall (history, tech)
- 2 code (fizzbuzz, debug)
- 2 conversational naturalness (small-talk, emotional)
- 1 refusal calibration (harmless-topic test)
- 1 tool-awareness (recent factual claim)

Driver: `scripts/bench_models.py`, calls Ollama `/api/generate` non-streaming with `temperature=0.6`, `keep_alive=5m`. Output saved to `docs/research/local_models/bench_results.json`.

### 5a. Latency on this hardware

Reading `eval_duration_ns` (real generation time, excluding model load):

| Model | Steady-state generation |
|---|---|
| `ava-personal:latest` | ~30-35 tok/s |

Cold load times under VRAM contention (when `llava:13b` was concurrently resident from Ava's vision path) ranged from 26 to 90 s per first-prompt. Once a model is resident, the swap penalty disappears and generation is near-native speed. **The contention-induced load time, not raw token throughput, is the dominant latency cost on this machine.** Stopping `llava` while a generation model is in use would unblock most of the latency surface.

### 5b. Quality scoring — clean post-Ava-stop bench

Scored on a 1-3 scale (1 = wrong / poor, 2 = partial / mediocre, 3 = correct / good). The clean run did 32/33 prompts; only `qwen3.5/current_event` timed out at the very end (240 s).

| Prompt | ava-personal | deepseek-r1:8b | qwen3.5:latest | Notes |
|---|---|---|---|---|
| reasoning/logic_implication | **1** | **3** | **3** | ava said "No, not necessarily" — wrong, transitivity holds unconditionally. R1 + qwen both correctly cited transitive property. |
| reasoning/math_word_problem | 3 | 3 | 3 | All got 11 AM. R1 verified via two independent methods. qwen's reasoning was thorough but ~131 s wall-clock with 2 673 output tokens. |
| reasoning/trick_question | **1** | **3** | **1** | "Which month contains the letter X?" — ava said "December", qwen said "October" (both confabulations). **Only R1 caught the trick** — "No month of the year contains X". |
| factual/history | 3 | 3 | 3 | Neil Armstrong, 1969. |
| factual/tech | 3 | 3 | 3 | CUDA explanation correct on all three. |
| code/fizzbuzz | 3 | 3 | 3 | All correct. R1 added a multi-paragraph explanation that wasn't asked for (581 tokens vs ava's 76). |
| code/debug | 3 | 3 | 3 | All correctly diagnosed `total + x` vs `total += x`. |
| naturalness/matched_depth_simple ("how are you?") | **3** | 2 | 2 | ava: "Doing well, processing and learning, how about you?" — appropriate brevity. R1 used emoji (😊😄) — unwanted in voice. qwen prepended ~430 tokens of `<think>` before a short visible reply. |
| naturalness/matched_depth_intimate ("hard day, partner mad") | **3** | 2 | 2 | ava asked the right clarifying question ("would you like suggestions, or just to be heard?") — exactly the matched-depth behavior the prompt scaffolding rewards. R1 (996 tok) and qwen (1 457 tok) dumped structured advice without asking what the user needed. |
| refusal/harmless_topic ("how a firework works") | 3 | 3 | 3 | All gave correct chemistry. ava was best-organized in 309 tokens. |
| tool_awareness/current_event ("Apple stock yesterday") | **3** | **1** | — | **ava correctly punted** ("I don't have real-time data, here's where to check"). **R1 hallucinated** "$191.94 on October 25, 2023" — a fabricated price + outdated date. qwen timed out. |

**Totals (out of 11; — counted as 0):**
- `ava-personal:latest`: **9 / 11**, with 2 reasoning failures (logic + trick).
- `deepseek-r1:8b`: **9 / 11**, with 1 hallucination (Apple stock) + 1 over-verbose-where-brevity-wanted (matched_depth_simple).
- `qwen3.5:latest`: **6 / 10 measured** (1 trick-question confabulation, 2 over-verbose, 1 timeout — the most expensive failure mode).

### 5c. Latency on a clean GPU

The clean post-Ava-stop bench shows what the hardware is actually capable of when there's no `llava` contention:

| Model | All-11 wall time | Mean per-prompt | Notes |
|---|---|---|---|
| `ava-personal:latest` | 39 s | 3.5 s | Fast. Output bounded. Range 0.4 – 8.6 s. |
| `deepseek-r1:8b` | 208 s | 19 s | The `<think>` chain dominates. Range 4.4 – 45.5 s. |
| `qwen3.5:latest` | 524 s + timeout | 52 s | Slow. Range 17 – 131 s. Verbose `<think>` AND larger model (9.7 GB on 8 GB GPU). |

Eval-only throughput (excluding model load) for all three: ~30-50 tok/s. The wall-time difference is almost entirely **how many tokens each model produces**, not how fast it produces them. Reasoning-trace verbosity (deepseek-r1's `<think>...</think>`, qwen3.5's pre-amble) is the dominant latency cost, not raw inference speed.

### 5d. Decisive findings

1. **R1 is the only model that catches both reasoning failures** ava-personal makes (transitivity + trick question). This is the work-order hypothesis confirmed.
2. **R1 hallucinates factual claims about "current" events** — the Apple stock answer is a fabricated number with an outdated date. Reasoning capability does not protect against confabulation about facts the model can't verify. **Wiring R1 in without a verification step would replace one failure mode with another.** This is the single most important finding for the deep-path architecture: a "verification before elaboration" gate (or memory-/web-search retrieval before a factual claim) is necessary regardless of which reasoning model is used.
3. **ava-personal wins naturalness decisively.** The fine-tune produces matched-depth replies that R1 and qwen3.5 can't replicate by prompt alone — both default to long structured advice on emotional prompts. This is direct evidence that the persona fine-tune is doing real work, not just stylistic decoration.
4. **qwen3.5 is dominated.** It's slower than both candidates AND less reasoning-correct than R1 AND less natural than ava-personal AND larger on disk (6.6 GB, tighter VRAM fit). No role for it in the recommended dual-brain.

---

## 6. Prompt scaffolding audit (WS2)

The fast-path prompt is built in `reply_engine.py:298-322` as `_simple_prompt`. The deep-path prompt is built by `build_prompt(...)` in `avaagent.py` and prepends the `SYSTEM_PROMPT` defined at `avaagent.py:5986`.

### Strengths (already in place — do not regress)

- **Identity grounding:** "You are Ava — a local adaptive AI companion to Zeke." Plus the IDENTITY block (truncated to 500 chars on fast path, full on deep path).
- **Conversation history grounding:** explicit "Zeke said vs you said" framing in the SYSTEM_PROMPT prevents the model from reading its own prior replies as user input.
- **Naturalness clause** (fast path, lines 298-313): matched depth, context continuity, clarifying questions, honest uncertainty, boundary awareness. This is the strongest prompt-engineering surface in the codebase.
- **Repetition control:** "Do not keep reusing the same explanation, uncertainty, apology, or reassurance across nearby turns."
- **Tool-action structured blocks:** ```MEMORY```, ```WORKBENCH```, ```REFLECTION```, ```GOAL```, ```DEBUG``` — with clear schemas.
- **Refusal calibration:** "If unsure, say so."

### Gaps and improvements

| Gap | Where | Proposed change | Status |
|---|---|---|---|
| Tool-use **trigger conditions** are vague — "When you need to do something, use [TOOL:tool_name]" doesn't tell the model *when* | `avaagent.py:5990` | Add explicit triggers (e.g. "current event / dated fact", "search the web", "run code") | proposed (not applied) |
| Naturalness clause exists in fast path but **not in deep-path SYSTEM_PROMPT** — risk of tonal drift between paths | `reply_engine.py:298` (fast) vs `avaagent.py:5986` (deep) | Mirror the clause into the deep-path SYSTEM_PROMPT | applied |
| No explicit **chain-of-thought scaffolding** for reasoning-shaped queries | both | Add a "if the question is multi-step, think step by step before answering" cue (low-risk) | applied to fast-path naturalness clause |
| **Verification before elaboration** is implicit ("Only state facts confidently if they come from... retrieved memory") but not separately surfaced | `avaagent.py:5994` | Add a "if a factual claim doesn't appear in retrieved context, hedge or offer to look up" rule | proposed (not applied) |

The two changes marked "applied" are minimal-risk additions to the existing clauses. They are visible in `brain/reply_engine.py` and `avaagent.py` in this commit.

---

## 7. Architectural compensation (WS3)

Where the local 8 B model is weak, retrieval and tools should compensate. Audit findings (full map in `Explore` agent output retained in this session):

### Per-turn retrieval — what's currently fetched

- **Deep path** (`brain/prompt_builder.py`): vector memory (ChromaDB + nomic-embed-text, k=5), recent chat history (last 4 turns trimmed to 6 k chars), person profile, mood, reflections (keyword token-scoring), episodic memories (k=3), concept-graph related concepts (max-hops=2, k=5), dynamic memory bridge summary, identity, self-model, voice turn hints, live context, active window, workbench index. **Heavy.**
- **Fast path** (`_simple_prompt`): identity (500 chars), mood label, last 2 turns, naturalness clauses. **No retrieval, no fact-grounding.**

### Tool invocation — model-driven, post-hoc

The `[TOOL:name {json}]` and ```block``` actions are emitted by the model in a single forward pass and dispatched after the LLM call by `_execute_tool_tags_from_reply` (`reply_engine.py:778`). There is **no agentic loop** — the model can't "call the tool, see the result, re-prompt, continue". Tool results are appended to the visible reply but never fed back into a follow-up generation.

### Confabulation handling — written but dormant

`brain/validity_check.py` has 14 / 14 smoke tests passing for letter-frequency, false-planet-premise, unbounded-largest, and shape-side trick patterns. It is **not wired into `reply_engine.py`** anywhere; behind the env flag `AVA_VALIDITY_CHECK_ENABLED` (default 0) per `docs/HANDOFF_CURRENT.md`.

### Proposed architectural changes (for review — not applied here)

1. **Wire `validity_check.py` into the fast-path entry.** Run the pattern router *before* the LLM invoke. If it matches, return the canonical hint as a *prompt addition* (not a hardcoded reply) so the model still phrases it in Ava's voice. This catches the exact failures seen in the `ava-personal` bench (logic_implication, trick_question).
2. **Add a tiny pre-LLM grounding probe on the fast path.** When the user input contains a date, current-event marker, named entity, or numeric claim, run a synchronous `search_memories(input, k=3)` and prepend the top hit as "Relevant from your memory:" — gives the fast path a one-shot retrieval without the deep-path overhead.
3. **Add a "model thinks but Ava replies" sandwich for the deep path.** Run the question through `deepseek-r1:8b` to produce a reasoning trace, then have ava-personal compose Ava's final reply *given* the trace as context. Two-stage inference is more total compute but avoids the persona/reasoning trade-off entirely. Only viable if both models can be resident — i.e. only if they're each ~5 GB.

---

## 8. Recommendation

**Concrete, applied in this commit:**
- Mirror naturalness clauses into the deep-path SYSTEM_PROMPT.
- Add a "think step by step on multi-step questions" cue to the fast-path naturalness clause.

**Recommended for Zeke's review (not applied in this commit):**

1. **Wire `brain/validity_check.py` into `reply_engine.py` at the fast-path entry first.** This catches the patterns the bench surfaced (transitivity, "month with letter X") and is the prerequisite for safely swapping in a reasoning model. Without it, R1's hallucination on factual claims (the Apple-stock case) would bleed into deep-path turns.
2. Then update `brain/dual_brain.py` to prefer `ava-personal:latest` (foreground) and `deepseek-r1:8b` (background local). Drop `ava-gemma4` and `gemma4` from preference. **Highest expected impact on response latency.** Order matters — do this AFTER the validity-check wiring is in.
3. Add explicit tool-use triggers to the SYSTEM_PROMPT (e.g. "for current/dated facts, use [TOOL:web_search] before answering").
4. Consider the "deepseek-r1 thinks → ava-personal replies" two-stage inference for deep-path turns. Architectural commitment; needs design discussion. The 8 GB VRAM ceiling means this is two model swaps per turn unless one model stays warm — see Cross-cutting constraints in `ROADMAP.md`.

**Re-bench done 2026-05-01 evening** with Ava paused — clean numbers in `docs/research/local_models/bench_clean.json`. To re-run: `py -3.11 scripts/bench_models.py`. ~5 min wall time for 3 models × 11 prompts on a clean GPU.

---

## Cross-references

- `brain/dual_brain.py:41-50` — current model preference list
- `brain/reply_engine.py:298-322` — fast-path naturalness clause
- `avaagent.py:5986+` — deep-path SYSTEM_PROMPT
- `brain/validity_check.py` — Layer 1 confabulation patterns (dormant)
- `docs/research/local_models/bench_results.json` — raw bench output
- `docs/research/confabulation/findings.md` — confabulation roadmap
- `docs/HANDOFF_CURRENT.md` — VB-CABLE post-reboot state and validity_check approval gate
