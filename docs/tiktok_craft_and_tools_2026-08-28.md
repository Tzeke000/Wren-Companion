# TikTok craft + free-tool research — 2026-08-28

Scope: craft specifics for `scripts/lyric_viz.py` (9:16 EDM/dubstep lyric renders and
`--tiktok` cuts), plus free/zero-spend tools worth installing on this machine.

Companion docs — read these too, they are NOT superseded:
- `docs/tiktok_playbook.md` — algorithm/strategy layer (watch time, completion, UGC, cadence).
  That doc answers *what to post*. This doc answers *how the pixels and bytes should be made*.
- `docs/edm_visualizer_techniques_2026-08-28.md` — visual technique reference.
- `docs/3d_model_catalog_2026-08-27.md` — centrepiece model sources.

## ⚠ Source-quality warning — read before trusting any number here

TikTok publishes **no authoritative safe-zone pixel spec** for organic posts, and its UI is
**responsive** — the caption block grows with caption length, the rail shifts on notched vs
flat phones, and tablets differ again. Nearly every "TikTok safe zone 2026" page on the open
web is SEO/AI-generated content of unknown provenance, and several cited URLs 404'd on fetch.

What I did instead: took the figures from the sources that **converge** and treated the
**conservative envelope** as the design rule. Independent sources agreed within ~30-90px:

| Region | checksafe.zone | postplanify | wildandfreetools | **Use this** |
|---|---|---|---|---|
| Top chrome | 200 px | 108 px | — | **200 px** |
| Bottom (organic) | 334 px | 320 px | 480 px | **340 px** (480 if caption is long) |
| Bottom (in-feed ad) | 450 px | — | — | **450 px** |
| Right action rail | 140 px | 120 px | — | **140 px** |
| Left margin | — | 60 px | — | **60 px** |

⇒ **The real fix is not to trust any of these: add a `--safe-overlay` debug flag that draws
the danger zones onto a render, then look at one real post on a real phone.** That converts a
guess into a measurement. Everything below is the starting rectangle, not gospel.

---

# PART 1 — Craft

## 1. Safe zones, and exactly where the current renderer violates them

**Design rectangle at 1080x1920 (organic post):**

```
SAFE BOX:  x 60 .. 940      y 200 .. 1580        (880 x 1380)
  top danger    y <  200    Following/For-You tabs, search, live controls
  bottom danger y > 1580    username, caption, hashtags, scrolling audio marquee
  right danger  x >  940    avatar+follow, like, comment, bookmark, share, spinning disc
  left margin   x <   60
```

Horizontal consequence people miss: because the rail eats the right side but nothing eats the
left, **the optical centre of the safe box is x=500, not x=540.** Centred text is 40px too far
right. Either shift the text block left by 40px, or (simpler and safer) keep centring on 540
but cap the width so the right edge clears 940.

### Violations in the current code (computed, not estimated)

Measured against `scripts/lyric_viz.py` as it stands, assuming a 1080x1920 render:

| Code | Value at 1080x1920 | Verdict |
|---|---|---|
| `_corner()` L1443 — logo watermark, bottom-right, `pad = H*0.03` | y≈1782-1862, x≈right edge | **WORST OFFENDER — sits under BOTH the caption block and the action rail. The watermark is 100% invisible on TikTok.** |
| lyrics `y = H*0.78` (radial styles) L1810 | text spans y 1498→1617 | **VIOLATES** by ~37px organic, ~147px vs ad zone |
| `wave` centrepiece `cy = H*0.80` L1320 | spans y 1392→1680 | **VIOLATES** by ~100px |
| lyrics `y = H*0.66` (non-radial) L1810 | text spans y 1267→1411 | OK |
| `mirrored bars` base `H*0.72` L1118 | bars grow *upward* to y≈1382 | OK |
| lyric width cap `total > W*0.92` L1803 | text spans x 43→1037 | **VIOLATES — 97px of every long line runs under the action rail; also 17px past the left margin** |
| tunnel / kaleido / ncs_ring / tn_blob (cy ≈ H*0.42-0.55) | centred | OK |
| title `_logo_center` H*0.30 / H*0.42 | y≈576-806 | OK |

**Fixes, in order of value:**
1. Move the corner watermark to **bottom-LEFT** (`x0 = pad`) and lift it to y ≈ H*0.80, or drop
   it in `--tiktok` mode entirely. Bottom-right is the single most-covered pixel region on TikTok.
2. Clamp the lyric baseline: `y = min(y, H*0.72 - font_height)` so no style pushes text past 1580.
   Cleanest is a single `safe_box(H, W)` helper every draw call consults, rather than per-style constants.
3. Change the width cap from `W*0.92` to **`W*0.815`** (= 880/1080), and centre on x=500.
4. In `--tiktok` mode, re-target `wave` upward (`cy = H*0.62`) or exclude it from the deck.

A `--safe-overlay` flag (draw the three danger rectangles at 30% red over a render) makes all
of this checkable in one frame instead of by arithmetic.

## 2. The first 1-2 seconds

The "first 1-3 seconds decides it" consensus is universal across sources, and it is consistent
with the mechanism `tiktok_playbook.md` already documents (completion rate is the ranking king;
sub-30% completion kills distribution). Specific retention percentages quoted by content-farm
sites are **unverifiable — do not treat them as data.** What survives scrutiny is directional
and mechanical:

- **Start on impact, not on approach.** For drop-heavy EDM this means opening *at or ~0.5s
  before* the drop transient, not at the build. A build is, by construction, the part of the
  song that withholds — it is the musical equivalent of a slow intro, which is the named
  distribution-killer. The one exception worth testing: a **very short** build (≤1s) that
  resolves inside the first 2s can work as tension-then-payoff, because the payoff still lands
  before the scroll decision.
- **Motion must exist in frame 1.** A fade-in from black is the worst possible opening: it
  spends the decisive second showing nothing. Guarantee non-black, already-moving pixels at t=0.
- **Text in frame 1.** A word on screen immediately signals "this is a lyric video" and gives a
  reason to stay. If the chosen hook window starts mid-instrumental with no lyric for 2s,
  consider rendering the *upcoming* line pre-emptively, dimmed.

### What the code does now, and the gap

`pick_hook_window()` (L426) already scores drop coverage + vocal density + energy, with an
`early` bonus for a drop/kick inside the first 3s — that is the right idea and it is working.
Two concrete weaknesses:

- **It opens 1.0s BEFORE the moment** (`t0 = best_t - 1.0`, L450). That deliberately spends the
  most valuable second of the clip on the run-up. Given the above, **reduce this to ~0.25s or
  drop it** — enough to avoid clipping the transient, not enough to feel like an intro.
- **The search steps `t0` in 1.0s increments** (`np.arange(..., 1.0)`, L436) and never snaps to
  the beat grid, even though `Analysis` carries `beat`, `beat_i` and `beat_ph` (L286-288).
  A clip that starts off-beat feels wrong immediately and cannot loop (see §4). Snap `t0` to
  the nearest **downbeat** (`beat_i % 4 == 0`).

## 3. Text legibility on a phone at arm's length

**Good news: font size is already fine.** The renderer uses `fs = H * 0.062` (radial) or
`H * 0.075` (L1796) — 119px / 144px at 1080x1920. Broadcast caption minimums sit around
4-5% of picture height, so 6.2-7.5% is comfortably above the floor. **Do not shrink it.**
Keep `≥ 5% of frame height` as the hard floor; the auto-shrink loop at L1803 can currently
drop as far as `fs > 18` (1.6% of a 1080-high frame), which is far below legible — **raise that
floor to `fs > H*0.045`** and let long lines wrap to two lines instead of shrinking to nothing.

The legibility problems are elsewhere, and they are self-inflicted:

- **Per-letter frequency bounce (L1828-1841).** Every letter rides its own FFT band with
  `dy = -band * fs * 0.35` — up to 42px of independent vertical jitter per glyph at 119px type.
  This is the single biggest readability cost after scramble. It destroys the common baseline
  that reading depends on. **Cap it at `0.12 * fs` for non-active words** and keep the larger
  excursion only for the active word, where the motion is informative rather than noise.
- **The scramble effect (L1791-1794)** is already correctly identified in the code comments as
  "the single biggest readability cost", and `--readable` kills it. **In `--tiktok` mode,
  scramble should default OFF** — a 30s clip has no budget for unreadable frames.
- **Inactive-word grey `(120,120,130)` (L1819)** on a bright, busy, bloomed background is low
  contrast. Lift it to ~`(190,190,200)` and let the accent colour (not brightness alone)
  carry the active-word distinction.
- **The scrim approach is right and should be kept** — the code comment at L1844-1849 states
  the correct principle (*protect text by darkening the background, never by brightening the
  text*). That is genuinely correct and worth preserving verbatim.
- **Add a stroke, not just a scrim.** A 2-3px dark outline (`stroke_width` in PIL's
  `draw.text`) is cheap and survives TikTok's re-encode better than a soft scrim, because
  compression smears low-contrast gradients but preserves hard edges.

**Words on screen / persistence:** the word-synced design is already the right format. Keep
**one lyric line at a time**, ~3-6 words. Minimum comfortable persistence for a line is
**~1.2s**; below that the viewer cannot finish reading it before it swaps. If the song's word
rate exceeds that, show the line for its full musical phrase and highlight words within it
(which is exactly what the active-word mechanism already does) rather than swapping lines faster.

## 4. Loopability

**Mechanism (consensus across every source, no hard public numbers):** TikTok auto-replays.
Replays accrue watch time, and watch time + completion are the top-two ranking signals. A
seamless loop hides the restart, so a viewer watches 2-3x without deciding to — which inflates
exactly the metrics that matter. Nobody has published a controlled measurement, so treat the
*size* of the effect as unknown but the *direction* as safe.

**A music visualizer is unusually well-placed to do this properly**, because everything on
screen is procedural and phase-driven. The construction:

1. **Cut on a bar boundary.** Choose the clip length as an **integer number of bars**, not a
   fixed 27s. At 140 BPM a bar is ~1.714s; 16 bars = 27.4s. Use `a.beat_i` to find start and
   end downbeats. This makes the *audio* loop cleanly, which matters more than the video.
2. **Make animation phase a function of beat position, not frame index.** Any state that
   integrates over time (`self._kal_r`, `self._shock`, particle `p[:,4]`, the rotating 3D
   model's angle) will *not* wrap. Drive them from `beat_i % 16` and `beat_ph` so frame N and
   frame N+loop_len are identical by construction.
3. **Crossfade as the fallback.** Where state genuinely can't wrap (particles), blend the last
   ~4 frames toward the first frame. Cheap, and invisible at 30fps on busy content.
4. **Verify by measurement:** render, then compare frame 0 and frame -1 with a mean-abs-diff.
   Under ~2/255 is seamless. That check is ~5 lines of numpy and turns "should loop" into "does".

Current blocker: `pick_hook_window` returns `t0 + target_s + 1.0` (L451) — an arbitrary ~28s,
beat-unaligned. Fixing §2's downbeat snap is a prerequisite for all of this.

## 5. Aspect / resolution / framerate / bitrate

### 🔴 Biggest single finding: `--tiktok` renders at 720x1280, not 1080x1920

```python
W, H = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1080, 1080)}[args.aspect]   # L2286
```

`--tiktok` forces `aspect="9:16"` (L2279) → **720x1280 unless `--size 1080x1920` is passed
manually.** That is **2.25x fewer pixels than TikTok's recommended 1080x1920**, on a platform
that will re-encode the upload anyway. Uploading a 720-wide source means TikTok's transcoder
is upscaling or encoding a soft source — the one quality loss that is entirely free to avoid.
**Change the 9:16 default to (1080, 1920).**

### 🔴 Second finding: `encode()` sets no rate control at all

```python
vstream = container.add_stream("h264", rate=fps)     # L2157
vstream.pix_fmt = "yuv420p"
vstream.width, vstream.height = W, H
# ...no bit_rate, no CRF, no GOP, no profile, no faststart
```

Measured on real output in `state/tzeke_songs/`: full renders land ~6.0 Mbps at **1280x720**.
Scaled to 1080x1920 that default lands in roughly the right ballpark by luck, but it is
unspecified behaviour — it depends on whatever libx264 default PyAV's bundled build carries,
and it can change under you on a PyAV upgrade. Note `scripts/make_clip.py` *does* set
`bit_rate` (L70) — so the codebase already knows how; `lyric_viz.encode()` just doesn't.

**Recommended settings** (all reachable from PyAV, no ffmpeg.exe needed):

```python
vstream = container.add_stream("h264", rate=fps)
vstream.pix_fmt = "yuv420p"
vstream.width, vstream.height = W, H
vstream.bit_rate = 12_000_000          # 10-14 Mbps for 1080x1920 busy motion
vstream.options = {
    "preset": "slow",                  # offline render — spend the CPU
    "profile": "high",
    "level": "4.2",                    # required for 1080p60
    "x264-params": "keyint=60:min-keyint=30:scenecut=40",
}
astream.bit_rate = 192_000             # AAC; 128k is audible on bass-heavy masters
```

Rationale for each:
- **Bitrate 10-14 Mbps.** TikTok re-encodes everything server-side and delivers ~2-4 Mbps. You
  cannot control the delivered bitrate. What you control is the *quality of the source the
  transcoder sees* — feeding it a clean, high-bitrate master avoids stacking your compression
  artifacts under theirs. Above ~15 Mbps the returns vanish; below ~8 Mbps, a strobing,
  high-motion, full-frame-change EDM visual will visibly macroblock. **High-motion abstract
  visuals are close to the worst case for H.264** — far harder than talking-head footage,
  which is why generic "5 Mbps is fine" advice does not apply here.
- **AAC 192k.** Default AAC in PyAV is low; on a bass-heavy master the sub content is exactly
  what a low AAC bitrate mangles first.
- **GOP ~2s (keyint=60 at 30fps).** Frequent keyframes give TikTok's transcoder clean cut
  points and make seeking/looping cleaner.
- **`+faststart`** (moov atom at the front) is the one thing that is genuinely awkward in PyAV
  and trivial with the ffmpeg CLI — see Part 2.

### Framerate: is 60fps worth it?

**Yes for this content specifically, with a caveat.** TikTok accepts and can deliver 60fps.
Strobing, beat-locked, fast-rotating visuals are the exact case where 30→60fps is visible —
much more so than for live-action. But it **doubles render time and doubles the frames the
CPU-bound numpy pipeline must produce**, which is already the pipeline's bottleneck.

Practical call: **stay 30fps for full-song renders; test 60fps on the ~30s `--tiktok` cuts
only**, where the frame count is small enough to afford. Render one clip both ways, post both,
compare. If 60fps goes in, `level` must be `4.2`.

### Other format notes
- 9:16 exactly = 1080x1920. Do not letterbox or pillarbox; TikTok will not crop it back.
- H.264 High profile, `yuv420p` — already correct in the code. Do not switch to HEVC; the
  compatibility risk is not worth it.
- Keep clips **under 30s** (already enforced) and avoid a hard cut to black at the end — it
  reads as "over" and suppresses the replay you want for §4.

## 6. Audio loudness

TikTok applies **playback loudness normalisation** like every other major platform, so a
hyper-compressed master gains no loudness advantage — it is turned down to match, and all you
keep is the dynamic-range damage.

**Target: ≈ -14 LUFS integrated, true peak ≤ -1 dBTP.** APU Software (an audio-tools vendor,
not a content farm) publishes **-16 LUFS / -1 dBTP** as a deliberately conservative working
reference for TikTok/Reels. TikTok does not publish an official figure, and reported values
across the industry range roughly **-13 to -16 LUFS** — so anywhere in **-16 to -14** is safe.

**Why this matters specifically for a drop-heavy master:** normalisation is driven by
*integrated* loudness across the whole clip. A `--tiktok` cut is ~30s of almost pure drop —
its integrated loudness is far higher than the parent song's, so **the same master normalises
differently as a clip than as a full track, and the clip gets turned down more.** The
perceptual result is that the drop lands *softer* on TikTok than it does locally. Two
implications:

- Do not master the clip louder to compensate — that is exactly what gets undone.
- **Preserve transient headroom instead.** Keep true peak at -1 dBTP so the kick still has
  attack after TikTok's lossy re-encode (which can push inter-sample peaks above 0 and cause
  clipping distortion on playback).
- Worth measuring: the pipeline could report integrated LUFS of the selected hook window at
  render time. `pyloudnorm` (BSD, pure Python + numpy, already-compatible deps) does ITU-R
  BS.1770 integrated loudness in a few lines — a cheap, zero-risk addition given the PCM is
  already in memory as numpy.

## 7. Covers / thumbnails for the profile grid

The cover is what sells a **profile visit → follow**, and the grid is where a listener decides
whether the catalogue is worth exploring. It matters much less for FYP reach (where the video
autoplays) and much more for conversion.

- TikTok grid tiles are **cropped to a portrait tile noticeably squarer than 9:16** and shown
  small. I could not source an authoritative tile ratio — treat the exact figure as unverified
  and check it against Zeke's own profile grid. The reliable part is the consequence: the cover
  frame's *centre* survives, and anything near the top/bottom edges is cropped away in the grid.
- **Pick the cover deliberately rather than letting TikTok default to frame 0.** For this
  renderer the ideal cover is a peak-drop frame: centrepiece at maximum, a short readable lyric
  line, high colour saturation.
- **Actionable and cheap:** the renderer already computes `a.rms`, `a.drop` and per-frame
  lyric state. Have `--tiktok` **also emit a `_cover.png`** — the single frame within the clip
  that maximises `drop AND rms AND (a short lyric line is on screen)`. Then upload that as the
  cover manually. This is maybe 15 lines and directly reuses the existing hook-scoring logic.
- Text on the cover should be **larger than in-video** (it's viewed at tile size) and centred,
  since the grid crop is centre-weighted.
- Keep covers visually consistent across a release so the grid reads as a body of work — same
  palette family and same lyric-type treatment per single.

## 8. Ranked craft changes to `lyric_viz.py`

Ranked by (impact × confidence) ÷ effort. Items 1-4 are small, local, and high-confidence.

| # | Change | Where | Effort |
|---|---|---|---|
| 1 | **9:16 default → 1080x1920** (currently 720x1280) | L2286 | 1 line |
| 2 | **Set explicit rate control** — 12 Mbps video, 192k AAC, preset slow, keyint 60 | `encode()` L2157 | ~6 lines |
| 3 | **Safe-zone pass**: move corner logo off bottom-right; clamp lyric baseline ≤ H*0.72; width cap 0.92→0.815; re-target `wave` | L1443, L1810, L1803, L1320 | ~20 lines + a `safe_box()` helper |
| 4 | **Beat-snap the hook window** to a downbeat, and cut the 1.0s pre-roll to ~0.25s | `pick_hook_window()` L436/L450 | ~10 lines; unlocks #5 |
| 5 | **Seamless loop**: integer bar count + phase-wrapped animation + frame0/frameN diff check | clip length + per-viz state | Medium — the only multi-hour item |
| 6 | **Legibility**: cap per-letter bounce at 0.12*fs for inactive words; `--tiktok` implies `--readable`; lift inactive grey; raise the font-shrink floor to H*0.045; add 2-3px stroke | L1828-1841, L1791, L1819, L1803 | ~15 lines |
| 7 | **Emit `_cover.png`** — best drop+rms+lyric frame in the clip | `--tiktok` path | ~15 lines |
| 8 | **`--safe-overlay` debug flag** — draw danger zones to verify #3 empirically | new | ~15 lines |
| 9 | Report integrated LUFS of the hook window at render time (`pyloudnorm`) | `analyze()` | ~5 lines |
| 10 | Test 60fps on 30s cuts only | `--fps 60` | free to test |

**Note that #1 and #2 together are ~7 lines and are the largest single quality jump available** —
they are pure output-format wins that require no visual redesign and no judgement calls.

---

# PART 2 — Free tools

Constraint: **zero spend.** Free/open-source only, no card-required trials, no paid tiers.
Everything below is genuinely free. **Nothing has been installed — this is a recommendation
only.**

## What is ALREADY on this machine (verified, `.venv\Scripts\pip list`)

This materially changes the answer, so check it before installing anything:

| Package | Version | Why it matters |
|---|---|---|
| `torch` | **2.11.0+cu128** | **A CUDA GPU compute path already exists.** The 3060 is usable for array math today, with zero install. |
| `numba` | **0.65.1** | **A JIT for the numpy inner loops is already installed.** Free speedup, zero install. |
| `av` (PyAV) | 17.0.1 | Bundles ffmpeg *libraries*, but no CLI. |
| `trimesh` | 5.0.0 | Loads GLB; has a decimation API but **no decimation backend installed**. |
| `numpy` | 2.4.4 | ⚠ numpy 2.x — a real compatibility constraint for some mesh libs. |
| `librosa` / `scipy` / `scikit-image` / `pillow` / `imageio` | current | Analysis + imaging stack. |

**`ffmpeg` is confirmed absent** — not on PATH, and `imageio-ffmpeg` is **not** installed
(plain `imageio` is, but it does not ship the binary).

## Verdicts

### 🟢 1. ffmpeg (a real binary) — **INSTALL. Clear #1.**

**Licence:** LGPL-2.1+ core; builds including x264/x265 are GPL-3.0. Free either way for
personal use.

**What it adds that PyAV genuinely cannot do easily.** Right now `scripts/make_clip.py`
**fully decodes and re-encodes** to cut a clip (L67-L94: `add_stream("h264")` → `decode` →
`reformat` → `encode`). That is **generation loss on every cut** — you are re-compressing an
already-compressed render before TikTok compresses it a third time. With the CLI:

- **Stream-copy cutting** — `ffmpeg -ss X -to Y -i in.mp4 -c copy out.mp4` cuts at keyframes with
  **zero quality loss and near-zero CPU.** This alone justifies the install.
- **`-movflags +faststart`** — moves the moov atom to the front. Awkward in PyAV, one flag here.
- **Two-pass EBU R128 loudness** — `loudnorm` with measured input values is the correct way to
  hit the −14 LUFS target from §6. PyAV has no filter-graph convenience for this.
- **Concat, scale/crop transcode, thumbnail extraction, GIF/WebP palettegen** for previews and
  the `_cover.png` workflow.
- Lets you shrink a clip to a Discord cap by **transcoding rather than re-rendering** — currently
  a full re-render of the visualizer.

**Install options (both free):**
- **Easiest:** `pip install imageio-ffmpeg` — ships a prebuilt ffmpeg binary into the venv, no
  manual download, no PATH edits. ~25MB. ⚠ It is a *reduced* build; I did not verify its exact
  filter list, so confirm `loudnorm` is present (`ffmpeg -filters | grep loudnorm`) before
  relying on it.
- **Safest/fullest:** a **BtbN/FFmpeg-Builds** or **gyan.dev** Windows release — a zip you
  extract and point at by absolute path. Full filter coverage, no PATH pollution required.

**Risk:** essentially nil. It is a standalone binary; it cannot break the venv.

### 🟢 2. `fast-simplification` — **INSTALL. Solves the dense-mesh rejection directly.**

**Licence:** MIT. **Size:** tiny (a small compiled wheel, prebuilt for Windows/py3.11).

The GLB loader rejects dense meshes. This is a quadric edge-collapse decimator that takes
vertices/faces as **numpy arrays** and returns reduced ones — exactly the shape of the existing
pipeline, and it is **the backend `trimesh` 5.0.0 (already installed) uses for
`mesh.simplify_quadric_decimation()`**. So this is a one-package install that turns an existing,
already-present API from broken into working, and **rescues the rejected models in the
`3d_model_catalog` automatically** instead of hand-picking low-poly ones.

**Alternatives, and why not:**
- `pymeshlab` — **GPL-3.0. Flag: licence contamination risk** if this code is ever distributed. Skip.
- `open3d` — MIT and capable, but a heavy install (hundreds of MB with deps) and historically
  slow to support **numpy 2.x**, which this venv is on. Not worth the risk for one function.
- `pyvista`/`vtk` — very heavy for a single decimation call.

### 🟢 3. `pyloudnorm` — **INSTALL. Tiny, closes the §6 loop.**

**Licence:** MIT. Pure Python on numpy/scipy (both present). Effectively zero install risk.

Implements ITU-R BS.1770 integrated loudness. The PCM is already in memory as numpy inside
`analyze()`, so this is a few lines to **report the hook window's integrated LUFS at render
time** — turning "the drop should survive normalisation" into a number you can act on. Small
tool, but it is the only way to verify §6 rather than assume it.

### 🟡 4. GPU / realtime rendering (ModernGL, moderngl-window, glumpy, VisPy) — **SKIP for now.**

Licences are all permissive (ModernGL/moderngl-window MIT, VisPy BSD-3, glumpy BSD —
glumpy is also largely unmaintained). The reasoning is not licence, it is redundancy:

**A GPU shader rewrite is a sane long-term upgrade, but it is not the cheapest next step,
because `torch 2.11.0+cu128` is already installed and the 3060 is already addressable.** The
CPU-bound parts of this renderer (domain-warped FBM background, quarter-res bloom, kaleidoscope
warp) are **elementwise array math over a 1080x1920 grid** — precisely what torch tensors do
well. Porting the hottest one or two functions to `torch.cuda` is a contained change, keeps the
numpy-shaped code, needs **no new dependency, no GL context, no window, and no readback plumbing**.

ModernGL *can* render offscreen headless on Windows (standalone WGL context) and read back to
numpy, but you then own a shader codebase, a context lifecycle, and a GPU→CPU readback that
frequently becomes the new bottleneck. **Do the torch experiment first**; only reach for
ModernGL if profiling says the remaining cost is genuinely rasterisation rather than array math.

**Also free and already present: `numba` 0.65.1.** For loops that don't vectorise (the per-letter
draw, particle updates), an `@njit` decorator is a zero-install experiment. **Try numba and torch
before installing any GPU framework.**

⚠ Related trap worth recording: the pip `opencv-python` wheel **does not ship CUDA**. Its
`cv2.cuda` module is absent, and no amount of having a 3060 changes that. GPU OpenCV requires a
self-compiled build — not worth it here.

### 🟡 5. Blender — **MAYBE LATER, not now.**

**Licence:** GPL-3.0 (the `bpy` pip module likewise). **Cost:** free, but a large install
(hundreds of MB).

Headless `blender -b -P script.py` would give real 3D rendering, GLB inspection, and mesh
decimation. But **the decimation need is fully covered by `fast-simplification` at a fraction of
the cost**, and the renderer's aesthetic is *wireframe/procedural*, not photoreal — so Cycles/EEVEE
output would not obviously fit the existing look.

⚠ **Real compatibility trap if you do go this route:** the `bpy` pip wheel is built against **one
specific CPython version per Blender release** — you must match the wheel to **Python 3.11** or it
simply will not install. Using the standalone Blender binary via CLI sidesteps this entirely and
is the safer path.

**Install it only if** the goal becomes pre-rendering actual 3D asset turntables (e.g. a
lit, textured logo animation) to composite in as a layer — a genuinely different feature, not an
upgrade to the current one.

### 🔴 6. projectM / Butterchurn (MilkDrop presets) — **SKIP.**

**Licence:** projectM LGPL-2.1, Butterchurn MIT — both free. The problem is integration cost, not cost.

- **projectM** is a C++/OpenGL app. Getting a **headless frame-dump** on Windows that you can pipe
  into a numpy pipeline is a build-and-plumbing exercise, not a pip install. **Multi-day.**
- **Butterchurn** is JS/WebGL. Driving it headless in a browser and capturing frames is possible
  (there is even a cloak-browser on this machine) but would be **slow, fragile, and hard to
  frame-sync to the beat grid** — and frame-accurate sync is the whole point of this renderer.

**The deciding argument:** the renderer *already has* radial ring, tunnel, kaleidoscope, mirrored
bars, NCS ring, Trap-Nation blob and Monstercat bars — which is most of the MilkDrop vocabulary
that suits this content, already beat-locked and already under your control. The marginal
visual gain does not justify days of integration. **Revisit only if a specific preset look is
wanted that the existing centrepieces genuinely cannot produce.**

### 🔴 7. Other Python video/motion libraries — **mostly SKIP.**

| Library | Licence | Verdict |
|---|---|---|
| `moviepy` v2 | MIT | **Skip.** It is a wrapper that shells out to ffmpeg. Once you have the ffmpeg binary you have strictly more power with less indirection. |
| `vidgear` | Apache-2.0 | **Skip.** Aimed at streaming/capture, not offline motion graphics. |
| `Pillow-SIMD` | PIL licence | **Skip.** ⚠ Does not build cleanly on Windows; it is a source-build replacement for Pillow and would put the working Pillow 12.2.0 at risk for a modest text-draw gain. |
| `taichi` | Apache-2.0 | **Maybe later.** Genuinely good GPU kernels with numpy-ish syntax — but it overlaps the already-installed torch path. Only if torch proves awkward. |
| `cupy` | MIT | **Skip.** A CUDA numpy drop-in, but it duplicates what the installed `torch+cu128` already provides, and adds a second large CUDA stack to keep in sync. |
| `scikit-image` | BSD-3 | **Already installed.** Worth mining before adding anything — it has warping, filters and morphology the renderer may be hand-rolling. |
| `pyloudnorm` | MIT | **Install** — see #3. |

## Ranked: what to actually install

1. **ffmpeg binary** — ends generation-loss re-encoding, enables `+faststart` and two-pass
   `loudnorm`. Try `pip install imageio-ffmpeg` first; fall back to a BtbN/gyan build if its
   filter set is short.
2. **`fast-simplification`** (MIT, tiny) — makes `trimesh.simplify_quadric_decimation()` work and
   rescues every dense GLB the loader currently rejects.
3. **`pyloudnorm`** (MIT, tiny) — measures integrated LUFS so the §6 loudness target is verified
   rather than assumed.

**Explicitly do NOT install:** ModernGL/VisPy/glumpy (torch+cu128 already gives GPU compute),
cupy (duplicates torch), pymeshlab (GPL), moviepy (ffmpeg supersedes it), Pillow-SIMD (Windows
build risk), projectM/Butterchurn (multi-day integration for marginal gain), Blender (unless the
goal changes to real 3D asset rendering).

**Free wins needing no install at all:** `numba` 0.65.1 and `torch 2.11.0+cu128` are already
here. Profile the renderer and try those on the hot paths **before** adding any dependency.
