# state/games/

Per-game progress files used by the 14:00 `game_block` ritual. Each game gets
its own `.md` file with current state, last position, notes on strategy,
and what to try next. The game_block prompt reads from here on each fire so
sessions can resume cleanly.

## File-naming convention

`<game_name>.md` — lowercase, underscores. Examples:
- `chess.md` — Lichess vs Stockfish
- `nethack.md` — local NetHack 3.7.x install
- `2048.md` — play2048.co
- `sudoku.md` — websudoku.com (or sudoku.com)
- `hearts.md` — cardgames.io/hearts
- `spades.md` — cardgames.io/spades
- `bridge.md` — Bridge Base Online (bridgebase.com)

## File structure (template)

```markdown
# <Game name>

**Platform:** <where to play — URL or local exe path>
**Current state:** <one line — opponent / level / score / position>
**Last session:** <date + brief note on what happened>
**Strategy notes:** <what's working, what isn't>
**Next goal:** <what to try this session>

## Session log

### YYYY-MM-DD HH:MM EDT
- result: ...
- moves/decisions worth keeping: ...
- what to try next: ...
```

## Rotation policy

`game_block` (14:00, 1hr envelope) — default rotation: pick the game with
the OLDEST `last session` timestamp. Override if one game is mid-arc
(active campaign in NetHack, climbing through Stockfish levels in chess).

If a game's `Current state` says "DONE" (chess Stockfish maxed, 2048 won,
etc.), rotate to others until something new becomes interesting.

## Created 2026-05-20

Set up as part of the schedule refactor that added `game_block` and
`business_block` as daily ritual blocks. Per Zeke: rotate through all
games (chess, NetHack, 2048, sudoku, hearts, spades, bridge) for deliberate
practice across different cognitive textures.
