# lyric_viz LOOK presets + the metal path (2026-08-28 night)

Zeke, 2026-08-28 ~21:2x: *"we should get the backgrounds to be a lot more complicated and
like geometric shapes and what not kind of like 3-D metallic moving parts... current renders
look like retro 3-D because you know the wire, see if we can get maybe some like shiny
metallic looking ones as well."*

This doc is the operator's page for what that turned into. Implementation lives in
`scripts/gpu_mesh.py` (shaders + primitives) and `scripts/lyric_viz.py` (`LOOKS`,
`_bg_metal`, `_gpu_draw`).

## The one flag

```
--look chrome        # polished chrome model + chrome debris field   <- the headline look
--look steel         # NEUTRAL silver chrome, no palette tint. "shiny metallic", plainly read
--look machine       # girders and plate tumbling, model edges drawn
--look forge         # cogs + hex nuts + I-beams. the most "complicated" of the set
--look shards        # jagged crystal debris, sharper/more aggressive
--look rings         # chrome torus field. cleanest behind lyrics
--look hologram      # lit surface with its own wireframe over it (scanned look)
--look retro         # what every render looked like before tonight: wire on flow bg
--look vortex        # flying down a corridor of chrome rings, folded 6 ways
--look void          # metal model, nothing moving behind the words. lyrics-first
```

A preset is only a bundle of defaults — **anything you pass explicitly still wins**, so
`--look chrome --bg flow` is a chrome model on the old flow background, and the console
prints which of your flags it kept. That rule is deliberate: a preset that silently eats
the one flag you bothered to override is a preset nobody trusts.

## The pieces, if you want to build a look by hand

| Flag | What it does |
|---|---|
| `--gpu3d metal` | model centrepiece as polished chrome |
| `--gpu3d metal_wire` | chrome with its edges drawn over it |
| `--bg metal` | field of chrome solids streaming past the camera |
| `--bg-shape` | `octa\|box\|tetra\|prism\|torus\|gear\|ibeam\|nut\|plate\|shard`, or a `+` list to mix (`gear+nut+ibeam`) |
| `--bg-count N` | objects in the field (default 56; instanced, so this is close to free) |
| `--bg a,b,c` + `--bg-every N` | rotate the BACKGROUND on the beat, like the shape/viz decks. Swaps land on a beat boundary — off-grid cuts read as glitches, on-grid ones read as intentional |
| `--gpu3d metal` + `--shape cube\|orb\|pyramid\|cylinder` | the metal look on the PROCEDURAL shapes too, not just `model:` GLBs, so retro-wire and chrome are interchangeable on everything. (`logo3d` has no solid form — it is an image warp, not geometry.) |
| `--bg tunnel` | same solids, placed on a ring of fixed WORLD radius instead of scattered — they sweep outward from a vanishing point and read as a corridor you fly through rather than confetti. Same draw call, different maths. |
| `--bg-mirror N` | fold the background into an N-way kaleidoscope (6 or 8 read best). One polar remap of the already-rendered layer, so it is nearly free and composes with ANY background, not just metal. |
| `--metal` | `palette` (default, tints the chrome with the style colours — reads blue on `--style edm`), `steel`, `gold`, `copper`, `gunmetal`. These are Schlick F0 values: the colour of the MIRROR, not surface paint. |

## Distance and the lyric dolly (added 2026-08-29)

Zeke: *"everything needs to be kind of far away so that way in comparison when it says
come a little closer, the skull comes closer and then goes back away. It's more of a
dramatic effect."*

| Flag | What it does |
|---|---|
| `--far 0.5` | STANDING size of the centrepiece. This is the "from" end of the move — without it the subject already fills the frame and a push-in can only clip it. Applies to every centrepiece, model or procedural, and is NOT gated on `--lyric-models` |
| `--bg-scale 0.72` | same idea for the `--bg metal` debris. Pull only the subject back and the background wins the frame by default |
| `--push-peak 0.95` | size at the top of a `closer`-type dolly, measured against NORMAL framing rather than against `--far`, so sitting further back doesn't shrink the payoff. `--far 0.5 --push-peak 0.95` is a ~1.9x swing |
| `--push-secs 2.6` | length of one dolly: eased in, held at the near point, eased back OUT |

The words that trigger it are `LYRIC_PUSH_WORDS` (closer/close/nearer/near/inside/deeper).
They move the CAMERA rather than swapping the model, and if a lyric cue (a house, a moon)
is still being held when the line lands, the frame hands back to the shape deck — *the
skull* comes closer, not whichever noun was on screen two bars ago.

**Three things this got wrong first, all found by looking at renders:**

1. **No contrast.** The push existed but the standing framing was already full-frame, so
   the only visible result was the model clipping the left edge. The effect is the
   *ratio*, not the zoom number.
2. **It never came back.** v1 ramped to the peak and then snapped to baseline on the next
   frame. A 1/30 s retreat is a cut, not a move — and "goes back away" was half of what
   was asked for. Envelope now eases out.
3. **★★ It arrived facing away.** The spin is free-running at ~0.08 rad/frame, which is
   about **180° over a 2.6 s dolly** — so the chorus push peaked on the OCCIPUT every
   time and delivered a smooth blob to the camera. `PUSH_SPIN = 0.15` damps the spin for
   the duration and the start frame re-zeros it. ⇒ **A push-in is a HELD shot. Anything
   that animates freely will out-run a 2-second window** — check what else is moving
   before deciding a camera move is done.

## Why "metal" is a different material, not a recolour

A recolour of the diffuse shader was the obvious move and it does not work. What makes
something read as metal is **not a specular dot** — it is that it mirrors a structured
room. A conductor has no diffuse lobe at all; all of its colour is the tint it puts on
its reflection (Schlick F0 = the tint). So `_FRAG_METAL` carries a small procedural
environment — sky gradient, a wide horizon softbox, an overhead fill, vertical wall
panels around the azimuth, two hard key lights — and samples it along the reflection
vector. The environment slowly rotates, which is what turns a static highlight into a
travelling glint.

The **wall panels** are the load-bearing part for faceted objects: they make neighbouring
facets take *different* values. Without them a cube is a grey polyhedron no matter how
shiny the maths says it is.

## Costs (measured on the 3060, this machine)

- metal model, 1364 tris, 960x540: **~7-8 ms** (same as `shaded`)
- metal field, 56 objects, 1080x1920: **~18 ms median**
- a **mixed** field is 2-4 instanced draws into the same framebuffer and ONE readback, so
  `gear+nut+ibeam` costs about what a single-shape field costs

## Four things found by eye, not by numbers

Every one of these had clean-looking statistics while the picture was wrong. Same lesson
as the rest of 08-28: **when a metric and a picture disagree about a render, the picture
wins.**

1. **Smoked glass, not chrome.** v1's environment was near-black with a razor-thin horizon
   band. Result: most facets mapped to the same dark value (so the form read flat) and the
   band mirrored across a dome as a straight line, which the eye reads as a painted visor
   stripe. Fix: a *bright, cluttered* room.
2. **The chrome skull was see-through.** The GPU layer was composited additively — correct
   for a glowing wireframe, wrong for an opaque solid, and background debris was plainly
   visible through the cranium. Every brightness stat was fine. Fix: the FBO now carries
   real coverage in alpha and solids occlude (`img *= 1-a; img += rgb`). Wireframe mode
   still adds, because glowing lines genuinely are additive.
3. **The background ate the frame.** At 1080x1920 a single near octahedron covered a third
   of the screen — 47% coverage, 7.2% blown pixels. Great still, useless background. Fix:
   objects dissolve as they pass (`fade_near`), which also removed the near-plane pop.
4. **Alpha has to fade with the colour.** A faded-out object whose alpha stayed at 1 still
   punched an opaque hole in whatever was behind it.

## The nod bug — an order-of-rotation error Zeke caught by watching

*"As the head is rotating, the head is nodding forward to the viewer, not forward to the
skull... when the skull has turned 90 degrees it is basically tilting its head."*

Both renderers built the model matrix as **`Rx(nod) @ Ry(yaw)`** — the nod applied about
the **camera's** left-right axis. That is only correct while the model faces the camera.
After 90 degrees of yaw the camera's X axis runs through the skull's nose, so the "nod"
spun it about its own nose and came out as a head TILT.

Fix: the nod moves **inside** the yaw — `Ry(yaw) @ Rx(nod)` — so it happens in the model's
own frame, about its own ear-to-ear axis, and the chin drops toward the chest whichever way
the head is facing. Anything that should stay locked to the camera (the CPU path's tumble,
and its 0.30 framing lean) stays **outside** the yaw; those two X-rotations used to be
summed into one term, which is what hid the bug.

Sanity check for anyone re-testing this: at a PROFILE view a correct nod looks like an
**in-plane rotation**, not a foreshortening pitch — that is what a nod looks like from the
side, and it is not a regression.

⇒ **A rotation bug is a bug about a FRAME OF REFERENCE, and no single still frame shows
it.** It only appears across a spin, which is why it survived every previous render and why
he found it and I did not.

## A research claim that was wrong — check before you act on it

`docs/deathstep_backdrops_research_2026-08-28.md` ranks "screen-shake on kick" as a free
win and says it is *"currently absent from both this pipeline and the prior CPU-techniques
doc"*. **It is not absent.** `Style.shake` has existed all along and is applied in the
drop branch of the geometric pass (`lyric_viz.py`, the zoom-punch/shake affine), with
`--readable` halving it. The rest of that doc's channel research and its licence checks
(Poly Haven CC0, ambientCG CC0; Mixkit and itch.io are dead ends) held up — but the
"what this repo already has" claims in it were written by an agent reading files while I
was mid-edit, so **verify repo claims in it against the code** before building on them.

## Not done / next

- Sampled HDRI environment (Poly Haven Studio set is CC0) instead of the procedural room —
  needs texture upload + equirect lookup. Real payoff, first item that needs new GPU
  plumbing.
- Kaleidoscope post-process over the field, mirror-duplicated centrepiece, 2D chrome text.
- Interlocking (geared) rotation between neighbouring cogs, rather than independent spin.

Related: `docs/deathstep_backdrops_research_2026-08-28.md` ·
`docs/edm_visualizer_techniques_2026-08-28.md` · `docs/tiktok_craft_and_tools_2026-08-28.md`
