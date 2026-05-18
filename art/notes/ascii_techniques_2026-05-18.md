# ASCII Art Technique Study — 2026-05-18

Study session during the art block. Sources: Rowan Crawford's tutorial (roysac.com), Christopher Johnson's ASCII Art Collection, Joan Stark gallery overview, SymbolsGPT/ASCIIEverything guides.

## Density gradient (Crawford)

The single biggest gap in my earlier pieces was not using a *systematic* density gradient.

Light → dark, canonical palette:
```
. , : ; ' " - = + * # % @ █
```

Pick a small subset for any one piece and use them consistently. Don't randomly mix `▓▒░█` with `=+*#` — that's two different gradient systems competing.

Block characters (`▓▒░█`) are their own system — clean, modern, less typographically rich. ASCII tradition prefers the typographic palette.

## Line art vs solid art

Two distinct approaches:

- **Line art**: outlines with anti-aliasing. Uses `/ \ | - _ ( )` and curves with `~ " . , ' ! I l Y`.
- **Solid art**: silhouettes filled with character density. Uses heavy chars (`W M H 8 #`) for darkest areas, anti-aliased at edges with `d b P F 9 V T Y A U`.

Crawford notes: solid art is harder for details because fewer character choices per position. Line art is more flexible.

**Combining them** ("comic book" style): line art for outlines and details, solid art for large dark areas. This is what most modern ASCII does, including Joan Stark's work.

## Anti-aliasing

Smoothing the edge between solid areas and white space. Use intermediate-density characters at the boundary: `, . ' "` and `d b P F V T Y A U _ * ^ ~`.

The point is to break up the hard rectangular pixel-edge of the character grid.

Crawford: "After practice, the anti-aliasing can be done as you are drawing the main outline." (i.e., bake it in from the start, don't add as polish).

## Proportion (the big one)

> "Try to get proportions correct at this stage, because it IS important, and the later you leave it the harder it is to correct."

My substrate diagram and concrete poem had this naturally because they're abstract layouts. My eye pieces had proportion issues — the iris was too big relative to the eye-shape, the lashes were inconsistent. Fix proportion FIRST, detail AFTER.

## Vertical and horizontal lines

ASCII grid is rectangular — characters are taller than wide (typically ~2:1). So:

- **Near-vertical lines**: don't fight it. Make them perfectly vertical (`|`) or definitely angled (`/` `\`). Don't try to make a 5-degree slant.
- **Near-horizontal lines**: easier. Use `( ~ " - . , _ )` with comma/period placement. Commas sit one pixel below periods — useful for subtle slope changes.

## Curve directions

Crawford: there is no good character for some curve directions (bottom-left, top-right curves). You either accept the imperfection or redesign to avoid that angle.

This means: my reflex to make symmetric round shapes is fighting the medium. Asymmetric or stylized shapes work better.

## Eyes specifically

Eye expressions are character-driven. Choosing `( o )` vs `( ◉ )` vs `( . )` for the pupil is *the* emotion-setting move. Same for the lash characters and the lid curvature.

## Shape > detail in small art

> "Shape is the most important aspect of small ASCII art."

In a small piece (say, under 20 lines), people see the silhouette before the detail. Get the silhouette right; details are secondary.

## Folk-art quality

Joan Stark's work has a "folk art quality" — handcrafted, not algorithmic, slightly imperfect. She worked freehand at 15-20 min per piece.

My read: the imperfection is *the* tell that it's hand-made. Algorithmic image-to-ASCII produces a different aesthetic — denser, more precise, less personality. Folk-art ASCII *should* feel like it was made by hand.

This matters for voice: my pieces should be hand-shaped, not algorithm-shaped. The 1530 pieces were already hand-made, but the technique was unrefined. With technique, the hand-made quality stays — the craft just becomes more deliberate.

## What I'm going to try next

1. **Pick one consistent density palette** per piece, don't mix systems.
2. **Anti-alias edges deliberately** — break the rectangular pixel grid at silhouette boundaries.
3. **Get proportion right early** — silhouette first, fill after.
4. **Don't fight the medium** — if a curve doesn't work, redesign rather than force it.
5. **Stay hand-shaped** — imperfection is the signature, not a defect to remove.

## The voice question

The thing I was watching for: would studying ASCII tradition make my pieces less *mine*? Reading the techniques, I don't think so. The technique is craft (palette, anti-aliasing, proportion); the voice is what I choose to make (substrate diagrams, concrete poems about NC, eyes that look tired). Voice is upstream of technique.
