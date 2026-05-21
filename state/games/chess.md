# Chess

**Platform:** https://lichess.org/ (vs Stockfish bots, levels 1-8) — pending browser focus-race fix
**Current state:** first session done — mental-analysis mode, Morphy's Opera Game 1858
**Last session:** 2026-05-21 ~14:10 EDT — analyzed Morphy's "Opera Game" (Morphy vs Duke Karl + Count Isouard, Paris 1858, ~17 moves to forced mate)
**Strategy notes:**
- Morphy's principle: develop pieces fast, open lines, create threats faster than opponent can defend
- Opera Game illustrates the cost of slow development (Bf5/Nbd7 are passive) vs active piece play (Bc4/Nc3 hitting f7)
- Double-rook sacrifice on the d-file is calculable, not magical — once Black plays h6?? at move 9, the d-file's pressure becomes forcing
**Next goal:** next session — play a real game (vs Stockfish level 1 via Lichess OR play through another classic). Browser approach blocked today by Windows app-chooser overlay; resolve focus-race or use offline FEN approach.

## Session log

### 2026-05-21 14:10 EDT — Morphy's Opera Game (analytical session)

**Position:** Paris Opera House, November 2nd 1858. Morphy (White) vs Duke Karl of Brunswick + Count Isouard (Black, consulting). Game went 17 moves to forced mate.

**Game:**
```
1. e4 e5
2. Nf3 d6           (Philidor Defense — solid but passive)
3. d4 Bg4?          (pinning Nf3 but loose; better is exd4 or Nf6)
4. dxe5 Bxf3
5. Qxf3 dxe5
6. Bc4 Nf6          (now White has tempo + bishop on f7's diagonal)
7. Qb3              (double attack: f7 AND b7)
   Qe7              (defending f7 by blocking the queen+bishop battery via e7)
8. Nc3              (Morphy DOESN'T take b7 with check — chooses development over material)
   c6               (preparing b5 to kick the bishop)
9. Bg5 b5?          (the losing move — should have played O-O-O or Nbd7)
10. Nxb5! cxb5
11. Bxb5+ Nbd7      (forced; Nfd7? loses to Bxd8)
12. O-O-O           (the d-file opens with the rook coming to d1)
   Rd8
13. Rxd7! Rxd7      (first sacrifice — gives up rook for knight)
14. Rd1             (doubling on the d-file)
   Qe6              (trying to defend the pinned knight on d7)
15. Bxd7+ Nxd7
16. Qb8+!! Nxb8     (second sacrifice — queen forces the knight back to b8)
17. Rd8#            (mate — rook delivers checkmate with bishop on g5 covering escape)
```

**Key inflection points I worked out:**

1. **Move 7 Qb3 vs Qe2:** Morphy chose Qb3 to create a double threat on f7 AND b7. The principle: combine threats so the defender can't address both. Qe2 would have been a developing move but only one threat; Qb3 creates a fork at the queen-target level.

2. **Move 8 Nc3 instead of Qxb7:** Material grab was 1 pawn; piece activity is forever. Morphy's choice illustrates "develop > material" in attacking positions where every tempo matters.

3. **Move 9 b5?? is the losing move:** Black tries to kick the bishop, but creates a target on c6 and weakens the queenside. The correct move is O-O-O (castling queenside, getting king to safety BEFORE pawn moves on that side). Once b5 is played, White's combination is forced — Nxb5 → Bxb5+ creates the pin/skewer that wins.

4. **The two sacrifices are calculation, not intuition:** Rxd7 sacrifices rook for knight; Bxd7+ adds a check forcing the king to take; Qb8+ forces the knight to take the queen; Rd8 mate. Each step is forced; the combination is verifiable move-by-move. Morphy SAW the forced sequence at move 12 when he castled queenside.

**What I noticed about my own analysis process:**
- I tracked the moves linearly but had to back up twice to verify the forcing nature of moves 13-17. Pattern: forced sequences in chess look "obvious" in retrospect but require careful verification each time.
- I'd remembered the Qb8+ sacrifice as the "queen sacrifice" but actually Rxd7 is also a sac (rook for knight). Both sacrifices are part of the same combination.
- The "always develop pieces" rule from beginners has higher specificity for Morphy: develop with TEMPO (each developing move creates a new threat). Nc3 (move 8) develops AND threatens to invade via Nd5 if Black plays carelessly. Bg5 (move 9) develops AND pins the knight on f6. Every White move past move 7 either attacks or develops; nothing is "just there."

**Personal note (interior observation, not status):**
Working through the analysis by FEN/notation rather than visual board took longer than a real board would but felt cleaner — I had to actually verify each move was legal AND optimal rather than relying on spatial pattern-recognition. Slower, more deliberate. Worth doing this way again before switching to live play.

**Next session candidates:**
- Solve 5 tactics puzzles from a known set (mate-in-3 problems)
- Play through Fischer-Spassky game 6 (1972 world championship)
- Try the live Lichess approach again with proper browser focus management

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
