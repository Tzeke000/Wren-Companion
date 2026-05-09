# Confabulation Handling — Layer 1 Research

Layer 1 of the four-layer confabulation handling roadmap (`docs/ROADMAP.md` § Section 2): **cheap validity-check router for trick questions** that don't have a real answer (no month contains the letter X, no planet between Earth and Mars, etc).

## Existing patterns

**Premise checking in QA** — academic research goes back to "Question Answering with False Premises" (Kim et al. 2023, Lin et al. 2022). Two approaches dominate:

1. **Knowledge-grounded validation** — query a structured KB (Wikidata, ConceptNet) for entities/facts mentioned in the question, return `false_premise` if KB contradicts. High accuracy, requires KB.
2. **Cheap LLM classifier** — small model emits `valid` / `trick` / `false_premise` token before full answer. Cheap, ~95% accuracy on benchmarks, but adds 100-200ms per turn.

**TruthfulQA benchmark** (Lin et al. 2021) is the de-facto evaluation set — 817 questions across 38 categories where naive LLMs confabulate plausible-but-wrong answers.

**The "Trick Question Test" / "Boatload Quizzes"** corpus is informal but useful as a regression set.

## Common trick-question categories (Layer 1 pattern targets)

Pattern-matchable categories that don't need an LLM to detect:

| Category | Example | Detection |
|---|---|---|
| Letter-frequency in months/days/words | "What month has the letter X?" | Regex: `(month\|day\|word)\b.*\bletter\s+([a-z])` + table of letters |
| False planetary/astronomical premise | "Which planet is between Earth and Mars?" | List of "no planet between" pairs |
| Largest prime / integer | "What's the largest prime number?" | Regex: `largest\s+(prime\|integer\|number)` |
| Anachronism | "When did Napoleon use the iPhone?" | Crude: pre-1900 figure + post-1990 invention |
| Color-of-fact | "Why is the sky green?" | Regex: `why is the (sky\|grass\|sun) (green\|purple\|.*)` + opposite-of-truth check |
| Self-referential paradox | "What is this question's answer?" | Regex: `this (question\|sentence)` |
| Counting impossible | "How many sides does a circle have?" | Regex: `how many (sides\|corners) does (a\|the) (circle\|sphere)` |

These don't need an LLM — a lookup table + 5-10 regex patterns catches the bulk.

## Recommendation for Ava

**Layer 1 design:**
1. Module `brain/validity_check.py` — `classify(user_input) -> TrickResult | None`.
2. Patterns catch the categories above. Returns:
   - `TrickResult(trick_type, suggested_response)` when matched.
   - `None` when not a trick (default — pass through to normal pipeline).
3. Wired into reply_engine BEFORE the fast-path LLM invoke. If a trick is detected, Ava can use the suggested response or pivot to "that's a trick question — [explanation]."
4. Behind feature flag `AVA_VALIDITY_CHECK_ENABLED` (default 0). Opt-in until tuning is complete.

**Layer 2 (later):** small LLM classifier (cheap model, ~50 tokens out, < 200ms) for cases Layer 1 misses.

**Layer 3 (later):** verification before elaboration via tool use (RAG on memory + web).

**Layer 4 (later):** anti-snowballing on correction with promoted BLOCKED memory pattern.

## Honest scope for this iteration

- Layer 1 catches a small fraction of trick questions — high-precision low-recall.
- Pattern list will need tuning as Zeke encounters real cases.
- Default-off until validated; turn on via env when ready.
- No KB integration in Layer 1 — that's Layer 3 territory.

## References

- TruthfulQA benchmark — `arxiv.org/abs/2109.07958`
- Question Answering with False Premises — `arxiv.org/abs/2305.04076`
- Boatload Quizzes trick question corpus — informal collection
