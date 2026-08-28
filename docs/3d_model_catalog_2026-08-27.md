# 3D model library — what is ACTUALLY on disk (rewritten 2026-08-28)

All files live in `assets/models3d/`. Use with
`--shape model:<name>` (comma-list to rotate several — see `--shape-every`).

**42 models.** All verified to load through `Renderer._model_mesh` and all inside the
wireframe edge budget.

## Why the edge budget exists (200–4000)

These render as **wireframes** — every mesh edge drawn as a line. Verified by eye
2026-08-28: **low-poly reads as the object; dense meshes collapse into an unreadable ball
of lines.** `scripts/fetch_models.py` enforces 40–4000 edges and *deletes* anything outside
it rather than leaving it to disappoint someone later. 20+ downloads were rejected on this
rule alone — cars, motorcycles, city scenes and realistic skulls run 8k–92k edges.

## ★ The naming lesson (2026-08-28)

The first pass named files after the **search term** that found them. That produced names
that lied: `dragon_1` was a **dragonfly**, `headphones_2` was a **phone**, `helmet_2` was a
**top hat**, `star_2` was a **sphere**. Nothing catches this except *rendering every model
and looking at it* — which I did, in one contact sheet, and then renamed or deleted.
**A library whose names lie is worse than a smaller honest one.** 9 unidentifiable models
were deleted outright.

⇒ If you add models: render them face-on as wireframes and LOOK before trusting the name.

## Licensing — read before posting publicly

Sources are **poly.pizza** (Google Poly archive + living creators). Two licenses appear:

- **CC0** — public domain. No credit needed. Preferred, and what most of this library is.
- **CC-BY 3.0** — free but **legally requires crediting the author** in the video
  description/caption.

**The known CC-BY authors in this set** (from the original 08-27 catalog): Jake K-H,
CreativeTechLab, Alex Safayan, Ian Wall, Poly by Google, Jakob Hippe, Ignition Labs,
J-Toastie, IvOfficial, Alan Zimmerman, Michal Minecki, smoj, Zsky, Jarlan Perez.
CC0 authors: **Quaternius**, **Kay Lousberg**.

⚠ The per-file license mapping was not fully preserved through the rename/prune pass.
**Before Zeke posts anything using a non-skull model, re-check that model's page** — the
CC0 skulls (Kay Lousberg + Quaternius) are the safe default and are what the current
Chipped White Car render uses, so **that post is clean**.

## The library

| Name | Reads as |
|---|---|
| `skull_kay`, `skull_quaternius`, `skull_2`, `skull_3`, `skull_4`, `skull_q3`, `skull_q4` | skulls — the workhorses, all legible |
| `heart`, `heart_2` | hearts |
| `diamond`, `jewel`, `gem_green` | faceted gems — very low poly, very crisp |
| `crystal_1`, `crystal_2`, `crystal_3` | crystal shards / clusters |
| `lightning_bolt`, `lightning_bolt_2` | bolts |
| `fire`, `campfire` | flames |
| `moon_1`, `moon_2`, `moon_3`, `sphere_1` | spheres / planetoids |
| `planets_set`, `sun` | planet row; spiky sun |
| `star_1` | a real 5-point star |
| `rocket_1`, `rocket_3` | rockets |
| `wolf_1`, `wolf_2`, `wolf_3` | standing wolves — ⚠ read well at an angle, edge-on they vanish |
| `dragonfly` | dragonfly |
| `figure` | small human figure |
| `red_car` | low-poly car |
| `harp`, `headphones_3`, `phone` | instruments/objects |
| `crown_1`, `crown_2`, `top_hat`, `hat_1`, `dome` | headwear / domes |

## Adding more

```
py scripts/fetch_models.py --search "crystal,helmet,wolf" --limit 3
py scripts/fetch_models.py --from-catalog
py scripts/fetch_models.py mything:PolyPizzaShortId
```
★ The short ID in a poly.pizza URL is **not** the asset filename —
`static.poly.pizza/<shortid>.glb` returns 403. The script scrapes the model page for the
real `static.poly.pizza/<uuid>.glb`. A downloaded file starting with `<?xm` instead of
`glTF` is an S3 error page, not a model.
