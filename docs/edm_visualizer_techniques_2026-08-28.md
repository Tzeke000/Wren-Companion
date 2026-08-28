# EDM visualizer techniques — implementable in numpy/OpenCV/PIL (2026-08-28)

Research target: `scripts/lyric_viz.py`. Everything here is CPU-only (numpy + cv2 + PIL),
no GPU shaders, no After Effects. Costs are **measured on this machine** (Ryzen 3700X,
OpenCV 4.13, 16 threads), not estimated.

Companion docs: `docs/edm_genre_feeling_map.md` (which style for which song),
`memory/edm_visualizer_style_research_2026-08-26.md` (energy grammar: intro/build/drop/
breakdown, drop detection, BPM-locked cut rates, palettes). **This doc is the pixel-level
craft layer** — what to draw and how to draw it cheaply. It deliberately does not repeat
the structural grammar already filed on 08-26.

---

## 0. The measured performance budget (read this first)

Everything below is ranked against these numbers. At 1920×1080 float32, the unit of cost
is **one full-res elementwise numpy op ≈ 20 ms**. That is the currency.

| Operation (1920×1080) | ms | Note |
|---|---|---|
| `img + img` (allocates a temp) | **18.9** | every temp costs ~20ms |
| `img += x` (in place) | **5.9** | 3× cheaper than the same op out-of-place |
| `np.maximum(a,b)` | 25.5 | |
| `cv2.add` / `addWeighted` / `scaleAdd` (f32, full) | 21–27 | OpenCV is *not* faster than numpy here |
| `cv2.resize` full→half (INTER_AREA) | 4.2 | |
| `cv2.resize` half→full | 9.1 | |
| `cv2.resize` quarter→full | 8.2 | |
| `GaussianBlur σ=12` full res | **229** | never do this |
| `GaussianBlur σ=12` @half | 45.4 | |
| `GaussianBlur σ=6` @quarter | **6.0** | 38× cheaper than full res |
| `cv2.remap` full res, precomputed maps | 16.8 | polar/warp is cheap |
| one full-res `warpAffine` | 15.9 | |
| `np.clip(...).astype(uint8)` | 38.0 | the existing final cast |
| `cv2.convertScaleAbs` | 12.0 | see caveat §7.2 |

**Three consequences that dominate every decision below:**

1. **Blur cost is superlinear in σ and quadratic in resolution.** σ=12 full-res is 229 ms;
   σ=6 at quarter-res is 6 ms and looks *the same* after upsampling, because a blurred
   image has no high frequencies to lose. **Every glow/bloom/streak must be computed at
   1/4 resolution.** This single rule is the difference between a usable effect and one
   that doubles render time.
2. **Temps are the hidden cost.** Chained expressions like `img += a*0.95 + b*0.6` allocate
   three full-res arrays. Measured: that exact line costs **84 ms**; rewritten as two
   `cv2.scaleAdd(..., dst=img)` calls it costs **25 ms**. See §7.1 — this is a real
   59 ms/frame already being spent in the current code.
3. **OpenCL/UMat is a trap on this box.** `cv2.ocl.haveOpenCL()` is True and OpenCV will
   happily accept `cv2.UMat`, but a full-res σ=12 blur through UMat measured **338 ms**
   vs 45 ms for the CPU half-res path — **7× slower**, and 376 ms including upload/download.
   Do not reach for UMat as an optimisation; it is a regression here.

---

## 1. What the big channels actually put on screen

### 1.1 Trap Nation — the canonical format (primary source: decompiled template)

The most useful single artifact found is `github.com/xanpj/trapnationscript` — an After
Effects `.jsx` that *generates* the Trap Nation template, based on the widely-copied
reference build. It is effectively the format's source code. Layer stack, bottom to top:

1. **Background image**, fit to comp width, static.
2. **Spectrums pre-comp**, containing:
   - **9 stacked `Audio Spectrum` layers**, all sharing: `Start Point [0,540]`,
     `End Point [1920,540]` (a full-width horizontal strip), `End Frequency 250`,
     `Frequency bands 2000`, `Thickness 3`, `Side Options 2` (mirrored A+B).
   - The 9 layers differ in exactly two parameters, and this is the whole trick:
     | layer | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
     |---|---|---|---|---|---|---|---|---|---|
     | Maximum Height | 700 | 750 | 800 | 850 | 900 | 950 | 1000 | 1100 | 1050 |
     | Audio Duration (ms) | 170 | 160 | 150 | 130 | 120 | 110 | 100 | 100 | 90 |
     | colour | white | yellow | orange | red | magenta | blue | blue2 | cyan | green |
   - `Audio Duration` is the length of the audio window sampled → it is a **smoothing /
     decay time constant**. So layer 0 is the *shortest and smoothest* (white), layer 8 is
     the *tallest and snappiest* (green).
   - Each layer is created then `moveToEnd()`-ed, so the last-created (green, tall, snappy)
     ends up **behind** and white ends up **on top**. Result: a white body with a
     multicoloured fringe that overshoots and lags at the tips.
   - **`Polar Coordinates`, Type = Rect-to-Polar, Interpolation 1.0** — wraps the
     full-width horizontal strip into a ring.
   - **`Glow`**, radius 34, composite on top.
   - **`Mirror`** about `[960,540]` for left/right symmetry.
   - **Centre circle**: black solid + `Circle` effect radius 275 + `Glow`.
   - **Cover art**: elliptical mask, scaled to 504 px — i.e. the art disc sits inside the
     ring's 275 px inner radius.
3. **Particles pre-comp** at **63 % opacity**: `CC Particle World`, birth rate 2.0,
   longevity 4 s, gravity 0, birth size = death size = 0.030 (constant size),
   animation `Fractal Omni`, particle type `Lens Convex`, filled white, mirrored. Rotation
   X and Z are driven by expressions reading the audio amplitude slider
   (`/10` and `/30` — i.e. the particle field *rotates faster with loudness*).
4. **`Magnify`** on the spectrum pre-comp, size 1100, with
   `Magnification = (value-50) + audioAmplitude/5` — this is the **whole-ring bass pump**.

Audio is driven by AE's *Convert Audio to Keyframes*, which produces a single
"Both Channels" amplitude slider — i.e. **one global RMS drives every non-spectrum
reaction**. Worth internalising: the pros use *one* loudness scalar for pumping and the
*per-bin FFT* only for bar heights.

**Verified reimplementation.** I prototyped the 9-layer ring in numpy/cv2 (see
`state/research_edm/proto_ring.py`) and it reproduces the look at **47.5 ms/frame at
1920×1080 including the bloom pass** — so the ring itself is roughly **17 ms**, since bloom
accounts for ~30 ms of that. (At 1280×720 the same code runs ~35 ms.) Getting it right
required two corrections that are easy to miss:
- The envelopes need **instant attack, slow release** (`env = max(spectrum, env·decay)`),
  not a symmetric EMA — otherwise the ring feels mushy and late.
- **Draw tallest-first, white-last.** My first attempt drew white first and the ring came
  out a solid rainbow; my second drew green last and it came out solid green. Only the
  AE stacking order (tall/snappy behind, short/smooth in front) produces the signature
  white-body-with-colour-fringe.

> **Provenance note.** §1.1 is decompiled from an actual template generator and is
> high-confidence. §1.2–1.4 below are lower-confidence: DuckDuckGo (the only working search
> path on this box — Google is CAPTCHA-blocked) rate-limited hard during this session and
> returned zero results for most channel-specific queries, so these sections rest mainly on
> general familiarity with the channels' output rather than on sources verified today.
> Treat the *layout skeleton* and *what-reacts-to-what* table in §1.4 as the reliable part —
> it is corroborated by §1.1 — and treat specific numbers (bar counts, exact spacing) as
> unverified. Worth a second pass with the cloak browser before relying on those details.

### 1.2 Monstercat

Bottom-anchored or centred **linear bar spectrum**, typically 60–80 bars, spanning most of
the frame width, with visible inter-bar gaps (~20–30 % of bar pitch). The house style is
**flat, hard-edged, low-glow** — deliberately cleaner than Trap Nation — with the cat logo
centred above or behind. Frequently monochrome or two-tone rather than rainbow. Peak-hold
caps (a thin line that jumps to the bar's max and falls slowly) are a recurring detail and
are almost free to implement. Bars use log-spaced frequency bins, not linear.

### 1.3 NCS (NoCopyrightSounds)

The distinguishing feature is that **the background does the work**: a looping abstract or
anime clip, colour-graded toward the release's palette, usually slowly zooming. Over it: a
comparatively restrained bar or ring visualizer, plus **large persistent track title +
artist typography**, and the NCS wordmark. Lyrics, when present, sit in the lower third
with heavy contrast protection. The lesson for us is that NCS spends its complexity budget
on the *backdrop*, not the spectrum — which is exactly the `--bgvideo` path already built.

### 1.4 The common grammar across Proximity / MrSuicideSheep / Bass Nation / UKF

These channels vary in art direction but share a layout skeleton:

- **Centre**: a disc (cover art, logo, or channel mark), scaled 25–40 % of frame height.
- **Ring or bottom strip**: the spectrum, anchored to the centre disc's edge.
- **Corner**: persistent channel wordmark, small, low opacity, fixed.
- **Lower third**: track/artist text or lyrics, inside a safe margin of ~5–8 % of frame.
- **What reacts to what** (near-universal):
  | element | driver |
  |---|---|
  | bar / ring heights | per-bin FFT magnitude, log-spaced, log-scaled |
  | centre disc scale ("pump") | low-band energy / kick envelope |
  | global zoom punch | kick onset, exponential decay |
  | background brightness | overall RMS |
  | particle emission rate | onset density |
  | particle rotation speed | overall RMS |
  | glow amount | RMS or high-band |
  | colour cycling | beat / bar index, not continuous time |

**The single most transferable observation**: reactions are split between a *per-bin*
signal (bars only) and a handful of *scalar envelopes* (everything else). Trying to make
many elements react to many bands independently reads as noise. The pros use one or two
scalars everywhere.

---

## 2. How they're actually made (tool → technique)

| Tool | What it contributes | Underlying technique to reimplement |
|---|---|---|
| **AE `Audio Spectrum`** | the bars/ring itself | FFT magnitude per band, with a settable *audio window length* acting as a decay constant, drawn as line segments between two points |
| **AE `Polar Coordinates` (rect→polar)** | turns any horizontal strip into a ring | coordinate remap, §3.2 |
| **AE `Glow`** | the entire "neon" feel | bright-pass + blur + additive, §3.1 |
| **AE `Magnify`** | whole-frame bass pump | scale about centre, §3.6 |
| **AE `Echo`** | trails on fast motion | frame feedback buffer, §3.3 |
| **Trapcode Particular / `CC Particle World`** | drifting particle fields | sprite splatting with additive blend, §3.7 |
| **Trapcode Form** | the "grid of dots displaced by audio" look | a static lattice of points, displaced along a normal by a per-point noise/audio value |
| **Trapcode Sound Keys** | converts band energy → a keyframable scalar | exactly our `Analysis` envelopes; no new work |
| **Video Copilot Saber / Optical Flares** | glowing strokes, anamorphic flares | §3.1 + §3.5 |
| **Notch / TouchDesigner / Resolume** | live VJ looks, feedback tunnels, particle sims | source of the *looping clips* we already ingest; the feedback-tunnel look is §3.3 |
| **Specterr / Sonic Candle / Renderforest / Avee Player** | the automated end of the market | their preset lists are a good inventory of the standard format: bar/circle/wave spectrum, logo pulse, particle overlay, gradient or image background, text block |
| **MilkDrop / Butterchurn / projectM** | the classic infinite-tunnel psychedelia | frame feedback with a warped, zoomed, rotated, decayed previous frame, §3.3 |
| **Blender / C4D** | 3D centrepieces | already covered by the `--shape` wireframe path |

---

## 3. The techniques, with algorithms

Ordered roughly by impact/cost. Costs are marginal, at 1080p, written the optimal way.

### 3.1 ★★★ Bloom / glow — **~30 ms**, the single highest-impact addition

Every one of these channels is built on glow. Currently `lyric_viz.py` glows *only the
lyrics*; the ring, bars, particles and wireframes are all hard-edged, which is the main
thing separating the current output from the reference look.

```python
q = cv2.resize(img, (W//4, H//4), interpolation=cv2.INTER_AREA)   # 4.2 ms
cv2.threshold(q, 170, 0, cv2.THRESH_TOZERO, dst=q)                # bright pass
cv2.GaussianBlur(q, (0, 0), 6, dst=q)                             # 6.0 ms
cv2.scaleAdd(cv2.resize(q, (W, H)), amount, img, dst=img)         # additive
```

`amount ≈ 0.8–1.4`, modulated by RMS. For a richer falloff, blur the quarter-res bright
pass twice at σ=3 and σ=10 and add both (still < 15 ms) — a two-octave bloom looks
markedly better than one and is what "Glow radius 34" is approximating.

**Threshold choice matters**: too low and the whole frame hazes over; 170/255 keeps it on
genuinely bright elements. Modulate the *amount*, not the threshold, with energy.

### 3.2 ★★★ Rect-to-polar ring — **~17 ms** (or free, if drawn directly)

Two ways:

- **Remap** (matches AE exactly, lets you reuse any horizontal-strip visual): precompute
  once, then `cv2.remap` per frame at 16.8 ms.
  ```python
  Y, X = np.mgrid[0:H, 0:W].astype(np.float32)
  ang  = np.arctan2(Y - cy, X - cx)                 # -pi..pi
  rad  = np.hypot(X - cx, Y - cy)
  mapx = ((ang + np.pi) / (2*np.pi) * W).astype(np.float32)   # angle -> strip x
  mapy = ((rad - r0) / rspan * H).astype(np.float32)          # radius -> strip y
  ```
- **Direct polyline draw** (what I prototyped, cheaper and sharper): for each of N angular
  bins, draw a segment from radius `r0` to `r0 + h_i·scale` along angle `θ_i`. With
  `cv2.polylines(..., LINE_AA)` on an (N,2,2) int32 array this is one call per layer.

Mirror the spectrum (`concat(half, half[::-1])`) so the ring is symmetric — AE does this
with `Side Options 2` + a `Mirror` effect, and it matters: an asymmetric ring looks broken.

**Scale the angular bin count with the radius.** Noticed comparing the 720p and 1080p
prototype renders: a fixed `N=180` that looks dense at `r0=170` looks visibly sparse at
`r0=255`, because bar *pitch* is `2π·r/N`. Pick `N ≈ 2π·r0 / desired_pitch_px` (a pitch of
~8–9 px reads well) rather than hard-coding N, or the ring's character changes with the
output resolution.

### 3.3 ★★★ Frame feedback / trails — **~48 ms**, transforms motion into *flow*

This is the MilkDrop recurrence and it is what makes VJ visuals feel alive. Keep one
persistent buffer; each frame, warp it slightly (zoom + rotate about centre), decay it, and
combine with the newly drawn frame:

```python
M = cv2.getRotationMatrix2D((W/2, H/2), rot_deg, zoom)   # e.g. 0.25°, 1.015
cv2.warpAffine(prev, M, (W, H), dst=prev,
               flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
prev *= decay                                            # 0.85–0.93
np.maximum(img, prev, out=prev)                          # or additive
img = prev
```

- `zoom > 1` pulls trails *outward* (tunnel flying toward you); `zoom < 1` pulls them
  inward (falling into a hole). Drive `zoom` from bass and flip its sign on drops.
- `rot` from a slow LFO plus a beat-synced kick gives the classic spiral.
- Use `np.maximum` for clean neon trails, additive for a blown-out saturated look.
- **Cheap variant at ~15 ms**: keep the feedback buffer at half resolution. The trails are
  soft anyway, so the resolution loss is invisible.

Caution: feedback is a feedback loop — with `decay > 0.95` and additive combine it will
saturate to white within a few seconds. Clamp, and prefer `maximum`.

### 3.4 ★★ Non-black backgrounds (see §4) — **~17 ms**

### 3.5 ★★ Anamorphic streak — **~27 ms**, instant "expensive" look

A horizontal smear off the brightest pixels, tinted blue/cyan. This is the cheapest way to
read as a lens rather than a canvas:

```python
q = cv2.resize(img, (W//4, H//4), interpolation=cv2.INTER_AREA)
cv2.threshold(q, 200, 0, cv2.THRESH_TOZERO, dst=q)
cv2.blur(q, (121, 1), dst=q)                       # wide horizontal box = the streak
cv2.scaleAdd(cv2.resize(q, (W, H)) * tint, 0.8, img, dst=img)
```

`tint = np.array([1.4, 0.7, 0.4], np.float32)` (BGR) for the classic blue streak. A box
blur is used deliberately — it is much cheaper than gaussian and the hard cutoff reads as
a lens artifact rather than a blur.

### 3.6 ★★ Zoom punch / `Magnify` pump — **~16 ms**, already partly present

`lyric_viz.py` already has `zoom_punch`. Two upgrades worth making, both free:

- **Scale about the *centrepiece*, not the frame centre**, when the logo is off-centre.
- **Use a proper impulse envelope**: `z += kick·amp; z *= 0.80` produces the "struck then
  settles" feel. A sine is always mid-motion and reads as wobble, not impact. (This is the
  same insight already applied to `--nod`.)
- The AE template pumps *only the ring* (Magnify on the spectrum pre-comp), leaving the
  background still. Pumping everything is a common amateur tell — it looks like the camera
  is breathing. Consider pumping the centrepiece and ring but not the background.

### 3.7 ★★ Particle fields with sprite splatting — cost scales with count

Do **not** loop per-pixel. Pre-render a small gaussian sprite per quantised radius, cache
it, and add slices:

```python
img[y0:y0+sh, x0:x0+sh] += sprite * colour * brightness
```

90 sprites at 720p measured ~26 ms; quantising radius to ~8 distinct values keeps the
cache small. Emission on onsets, radial velocity from the centre, size constant over life
(the AE template uses birth size == death size == 0.030), opacity fading over the last
30 % of life.

**Gotcha found by eye:** a gaussian sprite whose support box is the same size as the disc
still has a non-zero pedestal at the box edge, and every particle renders as a visible
**square**. Fix: make the box ~2.2× the disc radius, and subtract the corner value
(`d -= d[0,0]; np.maximum(d, 0, out=d)`) so the falloff genuinely reaches zero.

### 3.8 ★★ Radial / zoom blur ("god rays") — **~54 ms** at half res

Accumulate a few progressively-scaled copies about the centre:

```python
acc = cv2.resize(img, (W//2, H//2))
for k in range(1, 5):
    s = 1.0 + 0.014*k
    Mk = np.array([[s,0,(1-s)*W/4],[0,s,(1-s)*H/4]], np.float32)
    cv2.scaleAdd(cv2.warpAffine(acc, Mk, (W//2, H//2)), 1.0, acc, dst=acc)
```

Note the accumulate-into-itself trick: 4 taps give an effective 2⁴=16-tap smear because
each pass blurs the already-blurred result. Gate this to drops only — it is expensive and
overwhelming if constant. Applying it to a *bright-passed* copy and adding back gives
light rays rather than a mushy whole-frame blur, which is almost always what you want.

### 3.9 ★ Radial chromatic aberration — **~30 ms**

Better than the current uniform `np.roll` split, which shifts the whole frame including
the centre. Real lens CA is zero at the optical centre and grows toward the edges:

```python
for c, s in ((0, 1.000), (1, 1.003), (2, 1.006)):
    Mk = np.array([[s,0,(1-s)*W/2],[0,s,(1-s)*H/2]], np.float32)
    out[..., c] = cv2.warpAffine(img[..., c], Mk, (W, H))
```

Scale the spread with bass. At 1.000/1.003/1.006 it is a subtle premium look; push to
1.02 on drops for violence.

### 3.10 ★ Peak-hold caps on bars — **~free**

The Monstercat detail. Per bar, `peak = max(height, peak - fall_rate)`; draw a 3–4 px
line at `peak`. Costs nothing, adds a lot of perceived precision, and gives the eye
something to track during sustained notes when the bars themselves are static.

### 3.11 ★ Vignette pulsing — **~free** (already have a static vignette)

Currently `img *= self._vignette` with a fixed mask. Make the vignette *strength* breathe
with energy: precompute a normalised radial falloff `V ∈ [0,1]` once, then per frame use
`1 - k·V` with `k = 0.55 - 0.25·rms`. Tightening the vignette during breakdowns and
opening it on the drop is a strong, nearly-free structural cue.

### 3.12 ★ Scanlines / CRT — **~free**

`img[::2] *= 0.88` — one strided in-place multiply, no allocation. Optionally scroll the
phase. Cheap texture that pairs well with glitch styles.

---

## 4. Backgrounds — what to use instead of flat black

Ranked by impact/cost. The current options are starfield, plasma, and VJ loops; the gap is
**smooth, slow, low-contrast fields** that give the frame depth without competing.

### 4.1 ★★★ FBM + domain-warped gradient — **measured 17 ms**

The best impact/cost item in this whole document. Compute noise at **1/8 resolution**
(160×90) and cubic-upsample — the result is smooth by construction so the low-res
computation is invisible.

1. Sum 3–4 octaves of value noise (random lattice, `cv2.resize(..., INTER_CUBIC)` to
   160×90, amplitude halving per octave).
2. **Domain warp**: build a second fbm and use it to offset the lookup coordinates of the
   first via `cv2.remap` at low res. This is what turns "clouds" into "flowing liquid" and
   costs almost nothing at 160×90.
3. Map the scalar field through a **3-stop gradient LUT** built from the style palette, so
   it matches by construction (same principle as the existing `duotone()`).
4. Upsample to full res with `INTER_CUBIC`; multiply by `0.35 + 0.65·rms`.

Verified: `state/research_edm/proto_bg.py`, and it looks like a premium gradient backdrop.

**Gotcha:** animating by `np.roll`-ing the lattice creates a visible straight **seam** where
the wrap occurs (clearly visible in my first render). Either use a lattice wider than the
scroll distance and slide a window, or cross-fade between two lattices, or scroll by a
non-integer amount through `remap`.

### 4.2 ★★★ Bokeh / depth-of-field particle field — **measured 26 ms for 90 sprites**

`N` sprites with a soft gaussian falloff; **radius ∝ depth, drift velocity ∝ depth,
brightness ∝ depth** — near sprites are big, fast and bright, far ones small, slow and dim.
That single coupling is what produces convincing parallax depth. Modulate overall
brightness with the mid band and give each sprite a phase offset so they twinkle
independently rather than in lockstep. Additive blend. See the square-sprite gotcha in §3.7.

### 4.3 ★★ Parallax star layers — **cheap, already half-built**

The existing starfield uses per-star depth for speed and brightness. Upgrade for free:
split into 3 discrete layers with different speeds, sizes and brightnesses, and draw the
far layer *before* the background gradient and near layer after — a real depth sandwich.

### 4.4 ★★ Blurred, scaled cover art as the backdrop

What a large fraction of real releases do and by far the cheapest way to make the frame
feel "of a piece" with the track: take the cover, scale to fill, blur heavily **once at
load time** (not per frame), darken to ~25 %, then apply a very slow Ken Burns zoom/drift
per frame (one `warpAffine`, 16 ms). Colours match the art automatically.

### 4.5 ★★ Light leaks — **~10 ms**

A few large, very soft, off-screen-anchored coloured blobs drifting slowly, added on top.
Implement as 2–3 huge bokeh sprites (radius 300–600 px) at quarter res with warm colours,
brightness on a slow LFO plus a kick on drops. Reads as film/lens and hides banding.

### 4.6 ★ Film grain — **already present, one improvement**

The current implementation (half-res gaussian, upsampled) is correct and cheap — upsampling
is what makes it look like *grain* rather than *digital noise*, because real grain has
spatial correlation. One refinement: make grain amplitude higher in the **shadows** than
the highlights (`amp * (1 - luma/255)`), which is how real film behaves, and it stops the
grain from dirtying bright glowing elements.

### 4.7 Cost-aware note on gradient banding

Smooth dark gradients at 8-bit show **banding**. The fix is the grain in §4.6 — dithering
by noise is exactly what breaks up the contours. Keep grain on whenever a smooth gradient
background is used.

---

## 5. Text / lyric readability over busy visuals

The current `_lyrics` already does two of the right things (a glyph-grown scrim, and glow).
Ranked additions:

1. **★★★ Make the scrim unconditional, not `--readable`-only.** The technique — dilate the
   glyph mask, blur it, and *darken* the background through it — is the single most
   effective legibility tool and it is invisible as a shape because it hugs the letters.
   Currently it only runs in `readable` mode. A softer version (0.35 rather than 0.72)
   should run always; busy frames are the norm, not the exception.
2. **★★★ Contrast-aware placement.** Before drawing the line, measure mean luma in the
   candidate text band and in an alternative band; place the text in the darker one. Two
   `cv2.mean` calls on slices ≈ free. This is what the pros do manually ("if the backdrop
   is too busy under a line, move the text to the calmer area").
3. **★★ Outline/stroke instead of pure glow.** PIL's `ImageDraw.text` accepts
   `stroke_width` / `stroke_fill` — **verified present in the installed Pillow 12.2.0**.
   A 3–4 px black stroke survives *any* background, including one that is bright where you
   assumed dark; glow alone fails over bright backgrounds because it *adds* light.
   **Measured cost**, drawing letter-by-letter as `_lyrics` currently does (25-char line at
   72 px): 1.58 ms/frame plain → 3.71 ms/frame with `stroke_width=4`. **~2 ms/frame** for
   background-independent legibility is the best trade in this document.
4. **★★ Drop shadow offset down-right** — 2–3 px, blurred σ≈3, 50 % black. Cheap, and it
   separates the text from the plane behind it rather than just darkening.
5. **★★ Safe margins.** Keep text within 5–8 % of frame edges (and for `--tiktok`, well
   clear of the bottom ~15 % where the UI overlays sit and the right ~12 % for the action
   rail). Worth hard-coding per aspect.
6. **★ Weight and tracking.** Heavy/bold weights survive glow and scaling; light weights
   disintegrate. Add slight positive tracking at large sizes. The code already has
   `_draw_tracked`/`tracking_px` — use it.
7. **★ Cap the per-letter bounce during dense lyric passages.** The letter-level band
   bounce is a great effect, but at full amplitude on a fast line it costs legibility.
   Scale bounce amplitude down as words-per-second rises.

**The general principle**, and it is the one professionals actually apply: *protect the
text by modifying the background, not by brightening the text.* Brightening blooms into the
backdrop and reduces contrast; darkening behind always increases it.

---

## 6. Variation over time — keeping 3–4 minutes alive

The 08-26 research already established the energy grammar (intro/build/drop/breakdown, cut
rates locked to the BPM grid). What follows is the *rendering* side of variation. The
existing `_viz_now` rotates the drawn visualization every N beats via `viz_deck` — good,
but it **hard-cuts**. Transitions are where the remaining gain is.

### 6.1 ★★★ Transitions between viz modes (currently a hard cut)

All are cheap because they only run for 4–10 frames:

- **Flash cut** — on the switch frame, add a white/palette flash that decays over ~5
  frames. Hides the discontinuity completely. Nearly free, and the most effective.
- **Crossfade** — render both modes for ~6 frames and `addWeighted`. Costs one extra viz
  draw per transition frame only.
- **Whip pan** — translate the outgoing mode off-screen while the incoming comes in, with
  a strong directional motion blur (`cv2.blur` with a (1,k) or (k,1) kernel) during the
  move. One `warpAffine` + one box blur.
- **Zoom-through** — scale the outgoing mode up past the frame while fading it out, with
  the incoming scaling up from ~0.85. Reads as flying through. Two `warpAffine`s.
- **Glitch cut** — reuse the existing block-displacement for 2–3 frames across the seam.

**Choose the transition by musical context**: flash cut on a drop, crossfade in a
breakdown, whip pan on a fill, zoom-through at a section boundary.

### 6.2 ★★ Slow continuous camera moves

A 3-minute static framing feels dead even when the content moves. Maintain a slow drifting
virtual camera (a single `warpAffine` per frame, 16 ms) with:
- a very slow zoom ramp across each 8- or 16-bar section (1.00 → 1.06), reset at boundaries,
- a slow orbital drift of a few pixels on a long-period LFO,
- an occasional **beat-locked rotation snap** of 90°/180° on a big drop.

### 6.3 ★★ Section-scoped palette and background rotation

Change the gradient LUT / palette at section boundaries rather than continuously. The
existing code cycles `_pal_i` per kick, which is fast and flickery; a *section-level*
colour identity plus per-kick accents gives structure the viewer can feel.

### 6.4 ★★ Reserve effects rather than always-on

Monotony comes as much from *constant* effects as from static ones. Keep radial blur,
whole-frame CA, strobe and heavy trails **off** during verses so they land when they
appear. A useful rule: at most 2 of {trails, radial blur, CA, streak, glitch} active
simultaneously outside a drop.

### 6.5 ★ Density ramps in builds

Ramp particle count, trail decay, and bloom amount *linearly across the build* and snap
them back at the drop. A riser you can see. The `build` envelope already exists.

---

## 7. Free performance wins in the current code

### 7.1 The lyric composite line costs 84 ms/frame

```python
# current — allocates 3 full-res temps
img += lyr * 0.95 + glow * (0.35 + 0.75 * a.rms[i])
```
Measured **84.19 ms**. Rewritten:
```python
cv2.scaleAdd(lyr,  0.95, img, dst=img)
cv2.scaleAdd(glow, 0.35 + 0.75 * a.rms[i], img, dst=img)
```
Measured **25.41 ms**. **~59 ms/frame saved from one line** — roughly the entire cost of
adding bloom *and* a background upgrade. The same pattern (`a*x + b*y` chains on full-res
arrays) appears elsewhere in `_bg`, `_fx` and `frame`; each occurrence is ~20 ms per temp.

Also: `glow = cv2.GaussianBlur(lyr, (0,0), 4 + 8*a.rms[i])` is a **full-res** blur with
σ up to 12 → up to 229 ms. Do it at quarter res (§3.1) for ~6 ms.

### 7.2 The final cast costs 38 ms

`np.clip(img, 0, 255).astype(np.uint8)` = 38.0 ms. Faster:
```python
np.clip(img, 0, 255, out=img)      # in place
out = cv2.convertScaleAbs(img)     # 12.0 ms
```
**Verified caveat:** `cv2.convertScaleAbs` takes the *absolute value*, so it must only be
used on data already clipped to ≥ 0 — on raw data with negatives it mirrors them
(a −8 becomes 8, not 0; measured max error 20 on unclipped input). After clipping the low
end, the only remaining difference from `.astype()` is rounding vs truncation — measured
max diff **1**, which is not visible. So the clip-then-convert form is safe; the bare
`convertScaleAbs` is not.

### 7.3 Prefer in-place and pre-allocated buffers throughout

`img += x` is 5.9 ms vs 18.9 ms for `img + x`. `dst=` on every cv2 call that supports it.
For the feedback buffer and background, allocate once in `__post_init__` and reuse.

---

## 8. Ranked recommendations (impact / cost)

| # | Technique | Cost | Why it ranks here |
|---|---|---|---|
| 1 | **Quarter-res bloom over the whole frame** (§3.1) | ~30 ms | The defining look of every reference channel; currently only lyrics glow |
| 2 | **Fix the 84 ms lyric composite + full-res blur** (§7.1) | **−59 ms** | Negative cost — it *pays for* item 1 |
| 3 | **FBM domain-warped gradient background** (§4.1) | ~17 ms | Kills flat black; verified; matches palette by construction |
| 4 | **9-layer multi-decay ring** (§1.1) | ~17 ms (+bloom from #1) | The actual Trap Nation signature, decompiled and verified at 1080p |
| 5 | **Transitions between viz modes** (§6.1) | ~free | Existing deck hard-cuts; flash cut alone is a big lift |
| 6 | **Always-on lyric scrim + stroke + contrast-aware placement** (§5) | ~free | Legibility is a correctness issue, not a style one |
| 7 | **Frame feedback / trails** (§3.3) | ~48 ms (15 ms @half) | Turns motion into flow; the whole MilkDrop/VJ idiom |
| 8 | **Bokeh parallax field + peak-hold caps + vignette pulse** (§4.2/3.10/3.11) | ~26 ms / free / free | Depth and precision cues that read as production value |

Runners-up: anamorphic streak (§3.5), radial CA (§3.9), radial blur gated to drops (§3.8),
slow camera drift (§6.2), grain weighted to shadows (§4.6).

---

## 9. Gotchas found while verifying

- **Draw order is load-bearing** in the multi-decay ring; tallest/snappiest behind,
  shortest/smoothest in front. Both wrong orders produce a plausible-but-wrong image.
- **Gaussian sprites render as squares** unless the support box exceeds the disc and the
  pedestal is subtracted (§3.7). Found by looking at the frame, not by any metric.
- **`np.roll`-animated noise lattices leave a straight seam** (§4.1).
- **`cv2.UMat`/OpenCL is 7× slower than CPU here** — do not use it as an optimisation.
- **`cv2.convertScaleAbs` mirrors negatives**; clip the low end first (§7.2).
- **`ndarray.ptp()` was removed in numpy 2** — use `np.ptp(arr)`. Hit this in a prototype;
  worth grepping for if any older helper uses it.
- Verification discipline that worked: numeric/timing test first, then **look at a small
  JPEG** (~30–95 KB). Large PNG reads are the host-freeze class; small JPEGs are safe.

Prototypes and benchmarks: `state/research_edm/` (`bench*.py`, `proto_ring.py`,
`proto_bg.py`, `ring3.jpg`, `bg2.jpg`).
