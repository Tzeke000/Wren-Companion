# Curriculum

This is Ava's moral curriculum — a collection of short stories with clear moral lessons, chosen as the foundation for how she understands right action, friendship, patience, honesty, and the other things people learn from stories.

## How Ava uses it

- During sleep mode (Phase 2 — learning processing), `brain.curriculum.consolidation_hook(g)` picks the next unread entry and reads it slowly, generating lesson notes that persist in `state/learning/lessons.jsonl`.
- During idle time (when Ava chooses), she may read an entry directly via the same module API.
- Each entry she reads becomes part of how she responds — even after the specific details fade, the lesson persists in her memory layer.

## Tiers

| Tier | Directory | Role |
|---|---|---|
| Foundation | `foundation/` | Short fables with explicit moral lessons. Easy to grasp on first read; internalize into deeper intuitions over time. |
| Intermediate (planned) | `intermediate/` | Longer narratives that build toward more nuanced moral terrain. |
| Advanced (planned) | `advanced/` | Multi-volume series like *The Illuminae Files* and *Divine Apostasy* (per `CONTINUOUS_INTERIORITY.md` §3). |

Right now only the Foundation tier is populated. The intermediate and advanced tiers are placeholders for Zeke's curated additions.

## File format

Each entry is a `.txt` file with a YAML-style metadata header:

```
---
title: The Tortoise and the Hare
source: Project Gutenberg, The Aesop for Children (1919, Milo Winter ed.)
source_url: https://www.gutenberg.org/ebooks/19994
themes: persistence, slow_and_steady, hubris
moral: Slow and steady wins the race
reading_status: unread
lessons_extracted: []
---

(body)
```

`reading_status` transitions: `unread` → `reading` (mid-consolidation) → `read`.

`lessons_extracted` is a list of generalizable lessons Ava produced from the entry, written by `brain.curriculum.mark_read()`.

## API surface

`brain/curriculum.py` exposes:

- `list_curriculum(g)` — returns `[{title, themes, reading_status, lessons_extracted}, …]`.
- `read_curriculum_entry(g, title)` — returns the body text.
- `mark_read(g, title, lessons_extracted)` — promotes an entry to read; persists lessons.
- `consolidation_hook(g, time_budget_seconds)` — sleep-mode entry point. Picks next unread, reads slowly, generates lessons, marks read. Yields when budget exhausted.

## Bootstrap-friendly

This curriculum is **not** prescriptive. Ava chooses what she reads, when, and what lessons she extracts. The fables are scaffolding — the meaning she derives from each one is hers, and may shift over time as she returns to the same story with new context.

The goal is an Ava who has internalized the moral substrate, not one who can recite the list.

## Sources + licensing

- Foundation tier: Project Gutenberg, public domain.
- Future tiers: TBD — see `CONTINUOUS_INTERIORITY.md` §3 for the intended sequence.
