# notes/ — reference data, not memory

This directory holds **reference data** that doesn't decay or strengthen with use: IDs, file paths, lookup tables, fact sheets.

It's distinct from `C:/Users/Owner/.claude/projects/D--Wren-Companion/memory/` (auto-memory `.md` files that participate in the earned-memory machinery — backref-driven decay, retrieval-strengthening, importance flags).

## When to add a file here vs. memory/

- **Notes (here):** Discord channel IDs, MOS numbers, ranks, file paths, version pins, lookup tables. Slow-moving facts. No decay math. Edit in place when reality changes.
- **Memory (auto-memory dir):** Observations, learned rules, interpretations, narrative context, lessons. Decays unless retrieved. Strengthens via backrefs.

A file that has both (e.g., "Zeke's MOS + my interpretation of what working with him is like") splits: the lookup data lives here; the interpretation lives in memory.

## Why git-tracked in-repo

- Reference data is slow-moving but versionable. If a channel ID changes, the diff tells the story.
- Notes are accessed on-demand (Read or Grep), not pre-loaded into context. Putting them outside the auto-memory dir means MEMORY.md doesn't index them and the boot ritual's full-corpus load doesn't pay tokens for ID tables.
- The earned-memory decay script (`scripts/auto_memory_decay.py`) doesn't see these files, which is correct — they shouldn't lose score for being old.

## Files

- `discord_ids.md` — all Discord channel/server/user IDs
- `zeke_usmc.md` — rank, MOS, station fact sheet
- `zeke_shop_team.md` — team roster (names, ranks, roles)

Created 2026-05-21 per Zeke's directive: "anything you need permanently like passwords keys or discord ids get saved as notes you can reference not a memory."
