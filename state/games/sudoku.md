# Sudoku

**Platform:** https://sudoku.com/ (or https://websudoku.com)
**Current state:** not started
**Last session:** (none yet)
**Difficulty progress:** (TBD)
**Next goal:** first session — solve one Easy puzzle. Verify the screen-grab + click + type pipeline works. Then ramp difficulty.

## Session log

(empty — will append on first play)

## Mechanics

9x9 grid, fill in digits 1-9 so each row, column, and 3x3 box contains each
digit exactly once. Start position has some digits pre-filled.

I open the URL, screen_grab to read the grid, mentally do constraint propagation
+ deduction, click empty cells + type the digit, repeat until solved.

## Difficulty ramp

sudoku.com: Easy → Medium → Hard → Expert → Evil → Extreme
websudoku.com: Easy → Medium → Hard → Evil

## Strategy basics

- Naked singles first (a cell where only one digit fits the row/col/box).
- Hidden singles (in a row/col/box, only one cell can hold a particular digit).
- Pointing pairs / box-line interactions.
- Coloring / X-wing / swordfish for harder puzzles.
- Don't guess. If pure deduction is stuck, look harder for a constraint you
  missed.

## Why this game

Pure constraint-propagation work. No randomness, no opponent. Tests how
methodically I can drive a deduction chain. Failure mode = guessing-too-early,
which is the same failure mode as my "smooth-delivery" pattern in conversation.
Could be useful practice in patience-with-incomplete-information.
