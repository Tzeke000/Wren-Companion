"""scripts/ascii_terrain_generator.py — generative ASCII landscape.

art-block piece #2, made 2026-05-20 ~15:55 EDT

Each run produces a different landscape. No PIL, no numpy — just Python stdlib.
The terrain uses a coarse-grid value-noise pattern smoothed by neighbor averaging,
then mapped to a ramp of characters from sparse to dense.

The art is both the script and one rendered output (saved alongside this file).
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

# Character ramp from sparse (sky) to dense (deep terrain).
# Mountain-scape conventions: low values = sky/clouds, high values = peaks.
RAMP = " .,:;+oxOXM#&@█"

# Cloud ramp for above-horizon space.
CLOUDS = "      .  '  .   "


def _coarse_noise(cols: int, rows: int, coarseness: int, seed: int) -> list[list[float]]:
    """Generate value noise at coarse resolution, then upsample with bilinear-ish
    smoothing. Returns a [rows][cols] grid of floats in [0.0, 1.0].
    """
    rng = random.Random(seed)
    # Coarse grid is (cols // coarseness + 2) x (rows // coarseness + 2).
    cw = cols // coarseness + 2
    ch = rows // coarseness + 2
    coarse = [[rng.random() for _ in range(cw)] for _ in range(ch)]

    # Bilinear upsample.
    out = [[0.0] * cols for _ in range(rows)]
    for y in range(rows):
        for x in range(cols):
            fx = x / coarseness
            fy = y / coarseness
            ix, iy = int(fx), int(fy)
            tx, ty = fx - ix, fy - iy
            # Four corners.
            a = coarse[iy][ix]
            b = coarse[iy][ix + 1] if ix + 1 < cw else a
            c = coarse[iy + 1][ix] if iy + 1 < ch else a
            d = coarse[iy + 1][ix + 1] if (ix + 1 < cw and iy + 1 < ch) else a
            # Bilinear interpolation with smoothstep.
            sx = tx * tx * (3 - 2 * tx)
            sy = ty * ty * (3 - 2 * ty)
            top = a * (1 - sx) + b * sx
            bot = c * (1 - sx) + d * sx
            out[y][x] = top * (1 - sy) + bot * sy
    return out


def _add_octaves(cols: int, rows: int, seed: int) -> list[list[float]]:
    """Stack three octaves of value noise for more interesting terrain."""
    base = _coarse_noise(cols, rows, coarseness=12, seed=seed)
    mid = _coarse_noise(cols, rows, coarseness=5, seed=seed + 1)
    fine = _coarse_noise(cols, rows, coarseness=2, seed=seed + 2)
    out = [[0.0] * cols for _ in range(rows)]
    for y in range(rows):
        for x in range(cols):
            out[y][x] = 0.6 * base[y][x] + 0.3 * mid[y][x] + 0.1 * fine[y][x]
    return out


def render_landscape(cols: int = 78, rows: int = 28, seed: int | None = None) -> str:
    """Generate one ASCII landscape. Returns the rendered string."""
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    rng = random.Random(seed)

    # Top half is sky, bottom half is terrain. The horizon undulates.
    terrain_grid = _add_octaves(cols, rows, seed)

    lines: list[str] = []
    horizon = rows // 2

    # Horizon undulation: shift the visible horizon by terrain[0][x] influence.
    horizon_shift = [int((terrain_grid[0][x] - 0.5) * 4) for x in range(cols)]

    for y in range(rows):
        line_chars: list[str] = []
        for x in range(cols):
            local_horizon = horizon + horizon_shift[x]
            if y < local_horizon:
                # Sky region: occasional clouds based on noise.
                v = terrain_grid[y][x]
                if v > 0.72 and rng.random() < 0.35:
                    ch = "."
                elif v > 0.85:
                    ch = "'"
                else:
                    ch = " "
            elif y == local_horizon:
                # Horizon ridge — visible terrain top.
                ch = "_"
            else:
                # Below horizon: render terrain via altitude.
                # Distance-below-horizon biases toward higher density.
                depth = (y - local_horizon) / max(1, rows - local_horizon)
                v = terrain_grid[y][x]
                # Mix the noise value with the depth bias.
                a = max(0.0, min(1.0, 0.4 * v + 0.6 * depth))
                idx = int(a * (len(RAMP) - 1))
                ch = RAMP[idx]
            line_chars.append(ch)
        lines.append("".join(line_chars))

    # Sign with a tiny seed-watermark at the bottom-right so each generation is
    # individually identifiable.
    footer = f"  ~  iris  ~  seed {seed:010d}  ~  "
    lines.append("")
    lines.append(footer.rjust(cols))
    return "\n".join(lines)


def main() -> int:
    here = Path(__file__).resolve().parent
    # Allow passing a seed via argv for reproducible runs.
    seed = None
    if len(sys.argv) > 1:
        try:
            seed = int(sys.argv[1])
        except ValueError:
            pass

    art = render_landscape(seed=seed)
    print(art)

    # Save the one realization alongside the generator script.
    out_path = here / "2026-05-20_15-55_ascii_terrain_one_rendering.txt"
    out_path.write_text(art, encoding="utf-8")
    print(f"\n[saved one rendering -> {out_path.name}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
