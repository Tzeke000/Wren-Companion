"""Symbiote v2 — look fixes from the first contact sheet (2026-09-18 12:2x).

Seen: body flat grey (height field saturates → no interior curvature → no
specular across the body); whole mass floods RED on kicks (the style's
frame-wide flash / rgb-split in _fx is ADDED on top of the black); droplets
read as pink pancakes with a dot (their height bump is taller than the body
and too small to merge).

Fixes: dome height profile + wet surface ripple; re-occlude AFTER _fx so the
black survives the style's flashes (the specular gets the bloom, the body does
not get the flash); droplets lower, larger, blurrier (viscosity up); rim is a
white-blue fresnel with only a touch of palette; sheen dialled down.
"""
from __future__ import annotations

import io
import py_compile
from pathlib import Path

P = Path(__file__).resolve().parent / "lyric_viz.py"
s = io.open(P, encoding="utf-8").read()
assert "_viz_symbiote" in s, "v1 not applied"
if "symbiote_post" in s:
    raise SystemExit("v2 already applied")

# 1. dome height profile + ripple (replaces the saturating clip)
old = ("        hfield = cv2.distanceTransform(mask, cv2.DIST_L2, 3) / max(1.0, R0 * 0.55)\n"
       "        np.clip(hfield, 0, 1, out=hfield)\n")
assert s.count(old) == 1, "height anchor"
new = ("        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 3) / max(1.0, R0 * 0.85)\n"
       "        np.clip(dist, 0, 1, out=dist)\n"
       "        hfield = 1.0 - (1.0 - dist) ** 2          # rounded dome, curvature everywhere\n"
       "        # wet surface ripple: slow interference pattern, louder with rms\n"
       "        hfield += (0.05 + 0.06 * rms) * (np.sin(xx * 0.11 + t * 1.7)\n"
       "                                         * np.sin(yy * 0.13 - t * 1.1)) * (dist > 0)\n")
s = s.replace(old, new)

# 2. droplets: lower, larger, and the goo blur wider
old = "                                    R0 * rng.uniform(0.16, 0.34)])\n"
assert s.count(old) == 1, "droplet radius anchor"
s = s.replace(old, "                                    R0 * rng.uniform(0.22, 0.42)])\n")
old = ("            hfield += 1.15 * np.exp(-((xx - dr[0]) ** 2 + (yy - dr[1]) ** 2)\n"
       "                                    / (2.0 * (r * 0.7) ** 2))\n")
assert s.count(old) == 1, "droplet bump anchor"
s = s.replace(old, ("            hfield += 0.80 * np.exp(-((xx - dr[0]) ** 2 + (yy - dr[1]) ** 2)\n"
                    "                                    / (2.0 * (r * 0.75) ** 2))\n"))
old = "        hb = cv2.GaussianBlur(hfield, (0, 0), 2.4)\n        sil = np.clip((hb - 0.13) * 9.0, 0, 1)\n"
assert s.count(old) == 1, "blur anchor"
s = s.replace(old, "        hb = cv2.GaussianBlur(hfield, (0, 0), 3.2)\n        sil = np.clip((hb - 0.16) * 8.0, 0, 1)\n")

# 3. shading colours: darker body, white-blue fresnel rim, less sheen; and STASH for the post-fx pass
old = ("        img *= 0.30                                   # dim whatever bg was drawn\n"
       "        img += st[\"plate\"] * (0.75 + 0.45 * rms + 0.30 * ke)\n"
       "        img *= (1.0 - S * 0.985)                      # the mass occludes\n"
       "        shade = (up(ndotl)[..., None] * np.array((24.0, 20.0, 30.0), np.float32)\n"
       "                 + up(spec)[..., None] * np.array((225.0, 232.0, 255.0), np.float32)\n"
       "                 + up(sheen)[..., None] * pal1 * 0.9\n"
       "                 + up(rim)[..., None] * pal0 * 0.85)\n"
       "        img += S * shade\n")
assert s.count(old) == 1, "compose anchor"
new = ("        img *= 0.30                                   # dim whatever bg was drawn\n"
       "        img += st[\"plate\"] * (0.75 + 0.45 * rms + 0.30 * ke)\n"
       "        rim_col = np.array((150.0, 165.0, 200.0), np.float32) * 0.7 + pal0 * 0.3\n"
       "        shade = (up(ndotl)[..., None] * np.array((16.0, 13.0, 20.0), np.float32)\n"
       "                 + up(spec)[..., None] * np.array((228.0, 236.0, 255.0), np.float32)\n"
       "                 + up(sheen)[..., None] * pal1 * 0.45\n"
       "                 + up(rim)[..., None] * rim_col)\n"
       "        img *= (1.0 - S * 0.985)                      # the mass occludes\n"
       "        img += S * shade\n"
       "        # stash for symbiote_post(): re-occlude AFTER the style's frame-wide\n"
       "        # flash / rgb-split in _fx, or a kick paints the black mass red\n"
       "        st[\"post\"] = (S, shade)\n")
s = s.replace(old, new)

# 4. the post-fx hook in frame()
old = ("        img = self._fx(img, i, a)\n"
       "        if self.bloom > 0:\n"
       "            self._bloom(img, i, a)\n")
assert s.count(old) == 1, "frame anchor"
new = ("        img = self._fx(img, i, a)\n"
       "        if self._symb is not None and self._symb.get(\"post\") is not None:\n"
       "            # symbiote_post: the black mass must survive _fx's additive\n"
       "            # flashes; put it back, highlights included, before the bloom\n"
       "            S, shade = self._symb[\"post\"]\n"
       "            img *= (1.0 - S * 0.985)\n"
       "            img += S * shade\n"
       "            self._symb[\"post\"] = None\n"
       "        if self.bloom > 0:\n"
       "            self._bloom(img, i, a)\n")
s = s.replace(old, new)

io.open(P, "w", encoding="utf-8", newline="\n").write(s)
py_compile.compile(str(P), doraise=True)
print("symbiote v2 applied; lyric_viz.py compiles")
