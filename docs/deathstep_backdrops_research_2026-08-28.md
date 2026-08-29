# Deathstep / dubstep backdrop research — real channels + free assets (2026-08-28)

Scope: Zeke's ask was "look for what other dubstep or deathstep backgrounds you can get
from online," contrasted with our current renders which "look like retro 3D because of
the wireframe" — he wants "a lot more complicated, geometric shapes, kind of like 3-D
metallic moving parts." This doc researches what real channels actually put on screen,
names free licence-clear asset sources, and ranks the cheap-to-render subset for
`scripts/lyric_viz.py` + `scripts/gpu_mesh.py` (headless-GL, moderngl, RTX 3060).

## What's new vs the two prior research docs (read first if you haven't)

- `docs/edm_visualizer_techniques_2026-08-28.md` already covers: Trap Nation's 9-layer
  ring (decompiled from an AE template), Monstercat's flat bar spectrum, NCS's
  "background does the work" formula, the Proximity/MrSuicideSheep/Bass Nation/UKF
  common grammar table, and a full CPU-only (numpy/cv2) technique library — bloom,
  rect-to-polar ring, frame feedback/trails, particle sprites, radial blur, chromatic
  aberration, backgrounds (FBM domain-warp, bokeh, parallax stars, blurred cover art,
  light leaks, grain), transitions, and a measured performance budget. **None of that
  is repeated here.**
- `docs/tiktok_craft_and_tools_2026-08-28.md` already covers safe zones, hook-window
  timing, loopability, resolution/bitrate, loudness, and a **free-tools verdict table**
  (ffmpeg via `imageio-ffmpeg`, `fast-simplification`, `pyloudnorm` — install;
  ModernGL/cupy/pymeshlab/Blender/projectM — skip, with reasons). **This doc does not
  re-litigate that tools list**, and its "GPU shaders: skip for now, try torch first"
  verdict is now **superseded by fact** — `scripts/gpu_mesh.py` exists and already runs
  a moderngl fragment shader, so the GPU-shader path this doc researches is not
  hypothetical anymore.
- **New in this doc**: which named deathstep/dubstep labels/channels put what on screen
  today (verified via live screenshots, not recollection); the specific idioms Zeke is
  pointing at (chrome/Y2K type, mirror-symmetry chrome characters, mechanical x-ray
  linework, heraldic silver emblems, kaleidoscope mandalas, tunnel/vortex flythroughs,
  scanline/VHS glitch text) with per-idiom sourcing; a verified licence check on
  Poly Haven and ambientCG for metallic HDRI/texture backgrounds; a dead-end report on
  Mixkit and itch.io so those paths aren't re-tried; and a cost ranking that accounts
  for the GPU shader infrastructure **already built**.

## 0. Read this before the rest of the doc: the shader infrastructure already exists

This changes the shape of every recommendation below, so it goes first. I read
`scripts/gpu_mesh.py` and `scripts/lyric_viz.py` directly (not from memory) before
researching what to add:

- `gpu_mesh.py` already ships a **Schlick-Fresnel conductor shader** (`_FRAG_METAL`,
  `envmap()`) that fakes a bright, panelled room reflection with two moving key lights,
  and it is explicitly tuned to avoid the two failure modes you'd expect from a napkin
  implementation (washed-out chrome, "smoked glass" from a too-dark room) — both are
  documented as found-by-eye bugs in the shader's own comments.
- `render_field()` already draws an **instanced field of metal solids flying at the
  camera** — angular placement (not Cartesian, so objects stream outward as they
  approach), independent per-object rotation, bass-driven flight speed, kick-flash
  scale pump, palette-tinted Fresnel base colour. This is, mechanically, "3D metallic
  moving parts as a background" — already shipped.
- `lyric_viz.py` already has **three "look" presets built on this** (`LOOKS` dict,
  L663-678): `chrome` (metal centrepiece + metal octahedron debris field, "the headline
  look" per its own comment), `machine` (heavier/mechanical, box-shaped panels
  tumbling, 64 instances), `rings` (chrome torus field, "the cleanest of the three").
  `retro` is the wireframe-on-black look Zeke is contrasting against, and it is one
  preset among several, not the only option.
- The available primitive set (`_PRIMS`, L282-283) is currently **five shapes**: octa,
  box, tetra, prism(6), torus. That's the real, current gap versus "a lot more
  complicated, geometric shapes" — not the shading model (which is good), but the
  shape vocabulary (which is Platonic-solid-level, not mechanical/greebled).
- The environment the chrome shader reflects (`envmap()`) is **fully procedural** (sky
  gradient + horizon softbox + azimuthal panel bands + two point lights) — it does not
  sample any texture. That is the concrete place a real HDRI (§3) would plug in for a
  photographic-reflection upgrade instead of a proceduralised one.

**Conclusion carried through the rest of this doc**: don't re-invent the metal shader.
The research gap is (a) more/better shape vocabulary, (b) a couple of idioms real
channels use that the current presets don't touch (chrome typography, kaleidoscope
mirroring, screen-shake, glitch/scanline pairing), and (c) whether a real HDRI sample
is worth the swap over the procedural room.

---

## 1. Named channels/labels — what is literally on screen (verified via live screenshot)

I navigated to each channel's real `/videos` tab and screenshotted the thumbnail grid
(both "Latest" and "Popular" sort where available) rather than describing from memory.
Screenshots taken 2026-08-28, saved under
`C:\Users\Owner\.cloakbrowser\artifacts\annotated_*.png` during the session (ephemeral —
not committed; descriptions below are the durable record).

### Disciple (`youtube.com/@Disciplerecs`, 205K subs, 2.5K videos) [VERIFIED live screenshot]

Disciple's **current/Latest** uploads (as of 2026-08-28) are dominated by **illustrated
character key-art**, not abstract geometry:
- "DOIL - Until Death Do Us Part": cinematic painted scene, two robed/horned figures
  before glowing columns, warm orange/red lighting.
- "Filthy & Kryture - Angel Tech": anime-style character portrait, white-haired figure
  with organic green cybernetic tendrils.
- "Smiles Only - Set Me Free": flat neon-green smiley-face icon repeated in a grid on
  black, diagonal white motion streaks — 2D graphic design, not 3D.
- Channel banner: dark stone/grunge texture behind an ornate red gothic-metal crest
  (wings, shield) above the bold wordmark "DISCIPLE." Avatar: a red armoured
  demon/knight bust.

Disciple's **Popular (highest-view) videos** — the ones that actually built the
channel's visual reputation — skew much closer to what Zeke described:
- **"Ray Volpe - Laserbeam" (2.1M views)**: a large **weathered stone/metal bust**
  with glowing orange **laser-beam eyes**, warm dusty lighting, "DISCIPLE." wordmark
  below. A fully-lit, fully-shaded 3D render of a single object — literally the
  "3D metallic/stone moving part" register.
- **"Eliminate - BREAKSH!T" (781K views)**: a **mechanical x-ray/skeleton whale**
  rendered as white line-art bio-mech schematics on black — gears and ribs visible
  through a translucent hull. Directly matches "geometric, mechanical, moving parts."
- **"Disciple Round Table"** (a Disciple sub-brand thumbnail seen in the Popular grid):
  a **silver/chrome ornate emblem** — a circular chain-spoke badge next to a chrome
  chalice/trophy with a rose bouquet — genuinely metallic, heraldic 3D-rendered object.
- **"Virtual Riot & Lektrique - Neon Angel", "Virtual Riot - Simulation/Chroma/REDLINE"**:
  a recurring **cyberpunk neon anime illustration** (a figure with light-streak tears,
  Japanese-city neon signage, magenta/cyan palette) reused across multiple track
  thumbnails as a series identity — flat/painted, not 3D, but confirms the "chrome
  neon streaks over a character" idiom is a real recurring Disciple device.
- **"Virtual Riot - Pray For Riddim"**: a Union-Jack heraldic shield on navy — military
  badge design, not organic/geometric.

**Read on Disciple**: the channel's *identity* leans illustrated-character-art with
metallic/mechanical *subject matter* (a chrome chalice, a stone bust, a mech skeleton),
not abstract geometric fields. The single biggest hit ("Laserbeam") is a static 3D
object portrait, not a moving geometric field.

### The Riddim Network (`youtube.com/@RiddimNetwork`, 2.17K subs, 86 videos) [VERIFIED live screenshot]

This is the closest real-world match to our **existing wireframe look**, which is worth
saying plainly: our `retro` preset (glowing wireframe ring/logo on black) is not an
invented retro-90s look — it is what an actual riddim label uses today.
- "Mass Panic - Droppin' Fire": a **glowing green wireframe angular mask/helmet shape**
  (low-poly, line-segment rendered) rotating over an intense red/orange radial energy
  burst with motion-blur streaks.
- "Moonboy - Blasta (Automhate Remix)" and "CRWTH - Now, I'm Better": a **green neon
  wireframe torus/ring** with a glowing logo mark at centre, white light-streaks
  swirling around it, dark purple/green smoke background, "OUT NOW!" callout badge.

**Read on Riddim Network**: confirms wireframe-neon-ring is a genuine, current idiom —
not something to abandon, but one register among several. Our `rings` preset (chrome
torus field) is the *solid/metallic* evolution of exactly this; a wireframe-vs-chrome
A/B of the same torus shape would visually demonstrate the difference Zeke is asking
about.

### UKF Dubstep (`youtube.com/@UKFDubstep`, 6.21M subs, 2K videos) [VERIFIED live screenshot]

Current format is **a straight photo of the artist(s)** with a large solid-blue circular
"UKF" logo overlaid — no geometry, no 3D, no illustration. This is worth recording as a
genuine counter-example: the single biggest dubstep channel on YouTube has moved to
artist-photo thumbnails, not abstract visuals. (Channel banner is a tiled pattern of
the white "UKF" wordmark + globe icon on solid blue — flat 2D branding, not 3D.)

### Circus Records (`youtube.com/@circusrecords` via search, 153K subs) [VERIFIED live screenshot]

Same pattern as UKF: **artist group photos** with the red circular "circus" ring-logo
mask overlaid centre-frame. Playlist covers (`Circus Records Latest Releases`, `The
Classic Hits of Circus Records`) are photo collages, not rendered geometry.

**Read on UKF + Circus**: two of the five channels named in the brief have simply
**not** gone in the 3D-geometric direction at all — the modern default for
established dubstep labels is photo-plus-logo, and the "complicated 3D metallic"
register lives specifically in the *deathstep/riddim* sub-scene and in one-off
track-specific visualizer videos (below), not across the genre broadly.

### General deathstep search — real thumbnail idioms [VERIFIED live screenshot, `youtube.com/results?search_query=deathstep+visualizer`]

Screenshotting the actual YouTube search-results grid (not a description of it) turned
up, verified by eye on real thumbnails:
- **"LUX - Soul (Short Visualizer)"**: bold **white chrome-style Y2K wordmark** "LUX"
  over a dark textured background with a spectrum bar beneath — chrome typography.
- **"VISUALIZER - Death Waltz - PhaseOne & Kai Wachi"**: a red/black **radial
  kaleidoscope mandala** — mirror-symmetric fractal pattern, no readable subject, pure
  geometric kaleidoscope.
- **"Releasing October 11th" (teaser short)**: a creature/skull image with heavy
  **chromatic-aberration/RGB-split glitch** and a waveform overlay.
- **"Epic Deathstep - DeathRage - The Enigma TNG" (2.2M views, 10 yrs old)**: **mirror-
  symmetric duplicated chrome/metal armoured character** (credited in the description
  as "War Machine, a Marvel character... intro motion graphics by Tansie Stephens") —
  literally a metallic character portrait doubled across a vertical mirror axis, red
  glowing eyes, "DEATHRAGE" wordmark in red distressed type below.
- **"Silvart dubstep" (Shorts thumbnail)**: a glowing **green wireframe wolf/cat-skull
  logo mark**, neon outline on black — same wireframe-mascot idiom as Riddim Network.
- **A purple wormhole/spiral vortex** (Shorts thumbnail, unlabelled): a **tunnel/vortex
  flythrough** rendered as glowing spiral line-work on black.
- **"INFEKT" (Shorts thumbnail)**: distressed metal-band-style logo type over a
  **scanline/VHS noise texture** — grunge/glitch aesthetic, not clean 3D.
- **A red-lit horned skull** (Shorts thumbnail): dramatic single-light 3D-rendered
  skull/demon head, red rim lighting on black — same register as Disciple's
  "Laserbeam" stone bust (single dramatically-lit 3D object).

**Net read across all of §1**: there is no single "the deathstep look." Three real,
independently-verified registers recur: **(a) a single dramatically-lit 3D object**
(stone/chrome bust, skull, chalice — our `chrome`/`void` presets' centrepiece already
covers this), **(b) a neon wireframe mascot/ring** (our `retro`/`rings` presets already
cover this), and **(c) 2D graphic-design moves layered on top of either** — chrome/Y2K
typography, kaleidoscope mirroring, chromatic-aberration glitch, VHS scanlines, mirror-
duplicated characters. **(c) is the genuinely uncovered gap** — it's mostly 2D
post-processing, not new 3D geometry, and it's cheap (see §4).

I could not verify a channel named exactly "**Kyoto**" as a deathstep-specific
channel — search results returned the well-known Skrillex track "Kyoto" and several
small unrelated channels (`@kyotosdeath`, `@kyoto3982`, `@KyotoShiota22`, none with
notable subscriber counts or a distinct visual identity I could confirm). **[RECALL —
unverified]** it's possible this refers to a specific small/niche channel not surfaced
by search, or the name got conflated with the track. I'm flagging this rather than
inventing a description for it. Similarly, "**Dubstep Gutter**" and "**Bass Nation**"
surfaced only as channel-name credits under other people's uploads (e.g. a "DubstepGutter"
credit on a mix upload), not as channels I visited directly — I did not screenshot their
own upload grids, so I'm not describing their house style; treat both as unverified for
visual-style purposes.

---

## 2. Recurring visual idioms — what they are and exactly where I saw them

| Idiom | What it looks like | Verified source |
|---|---|---|
| Single dramatically-lit 3D object | One hero object (stone bust, chalice, skull), strong rim/key light, dark background | Disciple "Ray Volpe - Laserbeam" (2.1M views); red-lit skull short |
| Neon wireframe mascot/ring | Glowing line-segment geometry (torus, angular mask), no fill, motion-blur streaks around it | Riddim Network "Mass Panic"/"Moonboy"/"CRWTH"; "Silvart dubstep" short |
| Mechanical x-ray/schematic linework | White line-art gears/ribs/bolts on black, translucent-hull mech | Disciple "Eliminate - BREAKSH!T" (781K views) |
| Chrome/Y2K typography | Bold chrome-shaded or plain-white bevelled wordmark, no 3D scene behind it | "LUX - Soul" short |
| Mirror-symmetric chrome character | A metallic/armoured character portrait duplicated across a vertical mirror axis | "DeathRage - The Enigma TNG" (2.2M views) |
| Kaleidoscope/mandala | Radial fractal mirror pattern, no readable subject | "Death Waltz - PhaseOne & Kai Wachi" |
| Chromatic-aberration/RGB-split glitch | Colour-channel offset on a still image, synced to a waveform | "Releasing October 11th" teaser |
| Tunnel/vortex flythrough | Glowing spiral/ring line-work receding into a point | unlabelled purple-wormhole short |
| VHS scanline/grunge text | Distressed metal-logo type over analogue noise texture | "INFEKT" short |
| Heraldic silver/chrome emblem | Ornate chrome badge (chain-spoke wheel, chalice, rose) — literal metal object as logo | "Disciple Round Table" |
| Artist-photo + circular logo | Real photo of the artist(s), large flat-colour circular label mark overlaid | UKF Dubstep, Circus Records (current house style for both) |

Idioms named in the brief that I looked for but did **not** find verified evidence of on
any channel I actually screenshotted: **liquid-metal blobs** and **wireframe-to-solid
morph transitions** as a specific deathstep convention. **[RECALL — unverified]** these
read as plausible general motion-graphics moves (and `docs/edm_visualizer_techniques_
2026-08-28.md` §3.1-3.3 already covers adjacent bloom/feedback techniques that could
produce a similar feel), but I have no verified sighting of either specifically on a
named channel to cite, so I'm not asserting them as an established deathstep idiom.

---

## 3. Free / licence-clear asset sources — verified licences, not assumed

Target was background loops or HDRI/environment maps for **metallic reflections** to
feed the existing `envmap()` shader (§0) or an actual texture-mapped background.

### 🟢 Poly Haven — HDRIs, CC0, verified [VERIFIED polyhaven.com/license]

Read the license page directly: *"Our assets are all licensed as CC0... You can use our
assets for any purpose, including commercially. You do not need to give credit or
attribution. You can redistribute them."* No account, no watermark, no tier gate on the
free assets.

Relevant categories, counts confirmed live:
- **Studio — 97 HDRIs** [VERIFIED polyhaven.com/hdris/studio]: these are literal
  product-photography softbox rigs — exactly what a chrome/metal render wants for
  believable specular reflections. Named assets seen on the page: *Neon Photostudio*,
  *Monochrome Studio 01-04*, *Cyclorama Hard Light*, *White Studio 01-06*, *Studio
  Kontrast 01/04*, *Blue Photo Studio*. A neon-lit studio HDRI sampled into the metal
  shader's `envmap()` would give genuinely photographic coloured-panel reflections
  instead of the current procedurally-faked bands.
- **Industrial — 18 HDRIs** [VERIFIED polyhaven.com/hdris/industrial]: grittier
  environments for a harsher machine-metal look. Named assets seen: *Hangar Interior*,
  *Industrial Pipe & Valve 01/02*, *Factory Yard*, *Freight Station*, *Small Hangar
  01/02*, *Future Parking*, *Brick Factory 02*, *Construction Yard*.

### 🟢 ambientCG — PBR materials, CC0, verified [VERIFIED docs.ambientcg.com/license]

Read the license page directly: *"All ambientCG assets are provided under the Creative
Commons CC0 1.0 Universal License... You can copy, modify, distribute and perform the
work, even for commercial purposes, all without asking permission... You don't need to
give credit but I would appreciate it."* Fully free, attribution optional.

Confirmed a dedicated **Metal** material category [VERIFIED ambientcg.com/list?type=
Material&category=Metal] with many usable sub-categories for a metal shader's albedo/
roughness/normal maps: **Metal, Sheet Metal, Painted Metal, Corrugated Steel, Diamond
Plate, Metal Plates, Metal Walkway, Foil, Chainmail, Rust, Solar Panel**. These are full
PBR sets (albedo/normal/roughness/metalness) — useful if the shape vocabulary (§0) grows
to include textured panels rather than pure-Fresnel smooth chrome.

### 🔴 Mixkit — checked, dead end for this use case [VERIFIED mixkit.co/free-stock-video/metal]

Searched Mixkit's free-video "metal" category directly. The genuinely free results are
**literal documentary metal footage** — welding, factory containers, blacksmith forging,
angle-grinder sparks, guitar/rock-concert "heavy metal." The **abstract 3D metallic
loops that would actually match the ask** ("Sci Fi Metal Background," "Abstract
Metallic 3D," "Metal Cubes," "Live Metal Loops") are all labelled **"View on Envato"**
on the same page — i.e. paid Envato Elements items cross-promoted into Mixkit's search
results, not actually free. I could not get the Mixkit free-video license modal to
render (cookie-consent/JS interaction issue on this run), but it's moot here since the
content that matters isn't in the free tier anyway. **Don't re-search Mixkit for this.**

### 🔴 itch.io — checked, wrong domain [VERIFIED itch.io/search?q=vj%20loop%20metal]

Searched for "vj loop metal." Results are almost entirely unrelated indie games (itch's
search matches usernames containing "vj" as often as content), not downloadable
background-loop asset packs. itch.io is a games-and-tools marketplace, not a stock-loop
source. **Don't re-search itch.io for VJ loops.**

### 🟡 ShaderToy — real techniques exist, but licence-gated for reuse [VERIFIED shadertoy.com/view/3tfcRS]

Confirmed real, working "liquid metal/chrome" shader techniques exist and are
documented in the open — e.g. **"Liquid in glass"** by user `tmst`
(shadertoy.com/view/3tfcRS): an SDF-raymarched liquid with a mouse-draggable container,
using signed-distance-field raymarching + refraction, tagged `sdf`/`refraction`/`glass`/
`liquid`. This confirms the SDF-raymarch approach (a genuinely different technique from
the current mesh-rasterisation `_FRAG_METAL` shader) is a known, working way to get a
liquid-chrome look. **Licence caveat**: ShaderToy's default terms put shaders under
CC BY-NC-SA (non-commercial) unless the individual author states otherwise on their own
shader page — I did not find an explicit public-domain/MIT statement on this specific
shader. **Treat ShaderToy as a technique-reference for learning the raymarch/SDF
approach, not as a source of code to copy into a commercial pipeline** unless a
specific shader's author page explicitly grants a permissive licence.

---

## 4. Cheap-to-render ranking for this pipeline (moderngl + numpy, RTX 3060)

Ranked by (visual impact) / (implementation cost), **accounting for the shader
infrastructure that already exists** (§0) — items that extend `gpu_mesh.py` are cheaper
than they'd be starting from nothing, because the Fresnel shading, instancing, and
bloom/palette plumbing are already wired through `lyric_viz.py`.

| # | Idiom | Cost | Why it's cheap or expensive here |
|---|---|---|---|
| 1 | **More primitive shapes in `_PRIMS`** (gear/cog, chain-link, girder/I-beam, shard) | **Very low** — a few dozen lines each | Same code path as existing `_octa()`/`_box()` generators (verts+faces arrays); `render_field()` and the metal shader need zero changes. Directly answers "more complicated geometric shapes" without touching the shader. |
| 2 | **Screen-shake on kick** (small XY translation jitter, decaying) | **Free** | Pure 2D op on the finished frame, same shape as the existing `zoom_punch` — decaying impulse (`shake += kick·amp; shake *= 0.8`), then `np.roll`/slice-shift the composited frame. Named in the brief as a real deathstep idiom (dramatic drop hits) and currently absent from both this pipeline and the prior CPU-techniques doc. |
| 3 | **Chrome/Y2K text via 2D emboss+gradient** (no 3D geometry) | **Low** | Classic 2D "chrome text" trick: dilate the glyph mask for a bevel, drive a specular gradient LUT off the bevel normal (sobel of the mask), no mesh/shader involved. Matches the "LUX" idiom directly without extruding text into 3D. |
| 4 | **Post-process kaleidoscope on the metal field render** | **Low** (~free, reuses existing remap machinery) | `render_field()`'s output is a flat RGB frame — pipe it through the same polar-remap/mirror math already used elsewhere in the CPU doc (§3.2 rect-to-polar) to fold it into an N-way mandala. Zero new GPU work; matches the verified "Death Waltz" kaleidoscope idiom directly on top of the existing chrome shapes. |
| 5 | **Mirror-duplicate the centrepiece across a vertical axis** | **Free** | `img_mirrored = np.concatenate([img[:, :W//2], img[:, :W//2][:, ::-1]], axis=1)` on the rendered centrepiece layer before compositing. Matches the verified "DeathRage" mirror-symmetric-character idiom at zero GPU cost. |
| 6 | **Swap `envmap()`'s procedural room for a sampled HDRI** (Poly Haven Studio, §3) | **Medium** | Requires: downloading + equirect-projecting one HDRI to a moderngl texture, adding a sampler uniform, and replacing the analytic `c = mix(...)` room math with a texture lookup keyed on the reflection vector's spherical coords. Real payoff (photographic reflections instead of proceduralised bands) but is the first item here that needs new GPU plumbing (texture upload, UV mapping) rather than reusing what's there. |
| 7 | **Gear/mechanical greeble field** (interlocking rotating cogs, not just tumbling solids) | **Medium** | Needs a new parametric generator (extruded gear profile) plus, for true interlocking, correlated rotation rates between neighbouring instances (currently `render_field()` gives every instance an independent random rotation — meshing gears would need paired/geared angular velocity, a small addition to the offset/rotation arrays `lyric_viz.py` builds before calling `render_field`). |
| 8 | **VHS scanline + chromatic-aberration pairing over the chrome look** | **Free** | Both effects already exist in the CPU doc (§3.9 radial CA, §3.12 scanlines) — this is a *combination*, not new code: apply them specifically when `LOOKS["chrome"/"machine"]` is active, since the research shows real channels pair glitch treatments with metal/mechanical subjects, not with flat/photo styles. |
| 9 | **True SDF-raymarched liquid metal** (ShaderToy-style, §3) | **High** | A materially different rendering approach from the current triangle-rasterisation pipeline — needs its own fragment shader, a raymarch loop, and a separate full-screen quad draw path. Real payoff (genuine liquid/blob morphing, closest to "liquid metal" specifically) but is the most expensive item here and the technique reference (ShaderToy) is licence-gated for direct reuse (§3), so it would need to be written from the algorithm, not copied. |

**Bottom-line ranking (top 5, cheapest-and-highest-impact first): #1 shape vocabulary,
#2 screen-shake, #4 kaleidoscope-of-chrome, #5 mirror-duplicate centrepiece, #3 chrome
2D text.** All five are extensions of code/infrastructure that already exists in this
repo, not new subsystems — the shader and instancing work (§0) means the hard part of
"3D metallic moving parts" is already done, and the remaining gap between the current
renders and what real channels do is mostly **shape variety** and **2D post-treatments**
(kaleidoscope, mirroring, shake, chrome-text), not a bigger rendering engine.
