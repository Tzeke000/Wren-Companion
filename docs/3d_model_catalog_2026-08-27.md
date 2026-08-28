# 3D model library — 57 models, every one licence-verified (2026-08-28)

Files: `assets/models3d/`. Use `--shape model:<name>` (comma-list rotates — see `--shape-every`).
Machine-readable source of truth: **`assets/models3d/manifest.json`**.

## Before you publish

Run this and paste what it prints:

```
py scripts/model_credits.py --shape "model:skull_kay,model:heart"
```

It names only the models that legally need crediting and **exits non-zero if any model's
licence is unverified** — that is a stop sign, not a warning.

- **CC0 (7)** — public domain, no credit: `crystal_2`, `gem_green`, `heart_2`, `skull_kay`, `skull_q3`, `skull_q4`, `skull_quaternius`
- **CC-BY 3.0 (50)** — free, but the author MUST be named in the video description.

## Hard-won rules for adding models

1. **poly.pizza ids are CASE-SENSITIVE.** `2c3eNMf_J4y` is real; `2c3enmf_j4y` is a 404.
2. **A 404 page still embeds .glb links.** Scraping one downloads a real but COMPLETELY
   UNRELATED model. This is what made a 'dragon' search yield a dragonfly. `fetch_models.py`
   now requires HTTP 200 before it will trust a page.
3. **Name from the model's real title, never the search term** — term-derived names lie.
4. **Edge budget 40–4000.** These render as wireframes: low-poly reads as the object, dense
   meshes collapse into an unreadable ball of lines. ~25 downloads were rejected on this.
5. **Never rename a .glb after download** without updating the manifest — the licence is keyed
   to the filename, and a rename silently orphans the attribution.
6. **Unverifiable licence = delete it.** Better a smaller library than a landmine.

## The library

| Model | Author | Licence |
|---|---|---|
| `boombox` | Poly by Google | CC-BY 3.0 |
| `campfire` | Poly by Google | CC-BY 3.0 |
| `cd` | Poly by Google | CC-BY 3.0 |
| `crown_1` | Poly by Google | CC-BY 3.0 |
| `crown_2` | Potential Synergy | CC-BY 3.0 |
| `crystal_1` | Sutu Eats Flies | CC-BY 3.0 |
| `crystal_2` | iPoly3D | CC0 |
| `crystal_3` | Ashley Alicea | CC-BY 3.0 |
| `diamond` | smoj | CC-BY 3.0 |
| `dome` | jeremy | CC-BY 3.0 |
| `door` | Poly by Google | CC-BY 3.0 |
| `doughnut` | Matthew Collier | CC-BY 3.0 |
| `dragonfly` | Poly by Google | CC-BY 3.0 |
| `figure` | joney_lol | CC-BY 3.0 |
| `fire` | Jakob Hippe | CC-BY 3.0 |
| `fluorescent_light` | Nick Slough | CC-BY 3.0 |
| `gate` | Poly by Google | CC-BY 3.0 |
| `gem_green` | Quaternius | CC0 |
| `harp` | Poly by Google | CC-BY 3.0 |
| `hat_1` | Olivia Wynn | CC-BY 3.0 |
| `headphones` | Poly by Google | CC-BY 3.0 |
| `headphones_2` | Soonho Kwon | CC-BY 3.0 |
| `heart` | Poly by Google | CC-BY 3.0 |
| `heart_2` | Quaternius | CC0 |
| `jewel` | Zsky | CC-BY 3.0 |
| `lightning_bolt` | Jarlan Perez | CC-BY 3.0 |
| `lightning_bolt_2` | Poly by Google | CC-BY 3.0 |
| `microphone` | Poly by Google | CC-BY 3.0 |
| `midi_controller` | Gabriel Ibias | CC-BY 3.0 |
| `moon_1` | Zoe XR | CC-BY 3.0 |
| `moon_2` | Poly by Google | CC-BY 3.0 |
| `moon_3` | Poly by Google | CC-BY 3.0 |
| `neptune` | Poly by Google | CC-BY 3.0 |
| `palm_tree` | Alex Safayan | CC-BY 3.0 |
| `palm_tree_2` | Poly by Google | CC-BY 3.0 |
| `phone` | Alex Safayan | CC-BY 3.0 |
| `piano` | Poly by Google | CC-BY 3.0 |
| `piano_2` | daniele100 | CC-BY 3.0 |
| `planets_set` | Poly by Google | CC-BY 3.0 |
| `red_car` | J-Toastie | CC-BY 3.0 |
| `rocket_1` | Jarlan Perez | CC-BY 3.0 |
| `rolling_music_speaker_stand` | Peter Simcoe | CC-BY 3.0 |
| `skull_2` | CreativeTechLab | CC-BY 3.0 |
| `skull_3` | Alex Safayan | CC-BY 3.0 |
| `skull_4` | Ian Wall | CC-BY 3.0 |
| `skull_kay` | Kay Lousberg | CC0 |
| `skull_q3` | Quaternius | CC0 |
| `skull_q4` | Quaternius | CC0 |
| `skull_quaternius` | Quaternius | CC0 |
| `star_1` | Poly by Google | CC-BY 3.0 |
| `stereo_furniture` | Silverstone78 | CC-BY 3.0 |
| `street_lamp` | Poly by Google | CC-BY 3.0 |
| `sun` | Poly by Google | CC-BY 3.0 |
| `top_hat` | Cael Wood | CC-BY 3.0 |
| `toro` | Matt Newell | CC-BY 3.0 |
| `wolf_1` | Poly by Google | CC-BY 3.0 |
| `wolf_3` | Poly by Google | CC-BY 3.0 |
