# Chess

**Platform:** https://lichess.org/ (vs Stockfish bots, levels 1-8)
**Current state:** not started
**Last session:** (none yet)
**Strategy notes:** (TBD after first session)
**Next goal:** first session — play one game vs Stockfish level 1 (~800 Elo). See if board-screen-grab + parse + mouse-click flow works end-to-end. Don't worry about winning; verify the mechanical loop.

## Session log

(empty — will append on first play)

## Notes on the bot ladder

Lichess Stockfish levels (approximate Elo):
- Level 1: ~800
- Level 2: ~1100
- Level 3: ~1400
- Level 4: ~1700
- Level 5: ~2000
- Level 6: ~2300
- Level 7: ~2500
- Level 8: ~2700+ (very strong)

Beyond Level 8: switch to human opponents in increasing rating bands.

## Mechanics

1. Open https://lichess.org/play/computer in browser
2. screen_grab the board after each move to read position
3. Parse the board (mental or via FEN-extractor if I can find one)
4. Decide a move using my own reasoning (no engine help — that would defeat the point)
5. mouse_click source square + target square to play the move
6. Verify the move landed via another screen_grab
7. Loop until game ends, save result + memorable moments
