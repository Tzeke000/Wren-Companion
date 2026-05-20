# Hearts

**Platform:** https://cardgames.io/hearts/
**Current state:** not started
**Last session:** (none yet)
**Win/loss record:** (TBD)
**Next goal:** first session — play one full round against the cardgames.io bots. Verify card-recognition + click-to-play loop works.

## Session log

(empty — will append on first play)

## Mechanics

Trick-taking card game for 4 players. Goal: AVOID taking hearts (worth 1pt
each) and the queen of spades (worth 13pt). Game ends when someone hits 100pt;
LOWEST score wins.

Pass 3 cards at start of each hand: left, right, across, hold (rotating).

I open the URL, screen_grab to read my hand + the table, decide which card to
play, click it. Some interactive subtlety with the bots' play patterns.

## Strategy basics

- Pass high spades (especially the queen if not protected) — but don't pass
  ALL high spades or you can't shoot the moon.
- "Shooting the moon" (taking ALL hearts + queen of spades) gives 26pt to
  everyone else. Only attempt if hand is overwhelmingly strong.
- Lead low cards early to draw out high cards.
- Don't break hearts (lead a heart) unless you have to or it's strategic.
- Count cards — know which hearts have been played.

## Why this game

Incomplete-information game with adversaries. Different from chess (perfect
information) and 2048 (no opponent). Tests probability + opponent-modeling.
Different cognitive shape entirely.
