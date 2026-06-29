"""Rasterize a clean almond eye to a character grid for the boot banner.

Hand-placed block art reads crude (Zeke: "looks like the person who can't
draw drew it"). This computes the eye mathematically so the proportions are
right: an almond opening (pointed corners, not a circle), a textured iris
ring, a solid pupil, and a catchlight gap. Output is meant to be baked into
iris_body_host.py as a static string so boot stays dependency-free.

On a dark terminal the drawn ink reads as glowing blue, so the IRIS/PUPIL are
the dense parts and the sclera stays mostly empty — a luminous blue eye.
"""
from __future__ import annotations

import math

# Canvas in characters. Terminal cells are ~2.1x taller than wide, so we
# correct the vertical sample rate to keep the eye from looking squashed.
COLS = 62
ROWS = 19
ASPECT = 2.05  # cell height / cell width

# Geometry, in normalized space where the eye spans x in [-1, 1].
ALMOND_W = 0.98      # half-width of the almond opening
ALMOND_H = 0.52      # half-height at the center
TAPER = 1.7          # >1 => pointed corners (almond, not ellipse)
IRIS_R = 0.42        # iris outer radius
PUPIL_R = 0.17       # pupil radius
LIMBAL = 0.045       # dark limbal ring thickness at iris edge


def almond_half_height(x: float) -> float:
    """Top/bottom boundary of the almond at horizontal position x (-1..1)."""
    if abs(x) >= ALMOND_W:
        return 0.0
    return ALMOND_H * (1.0 - (abs(x) / ALMOND_W) ** TAPER)


def render() -> str:
    grid = [[" "] * COLS for _ in range(ROWS)]

    cx = (COLS - 1) / 2.0
    cy = (ROWS - 1) / 2.0
    # Scale so x spans the canvas width; y uses the same world scale * aspect.
    sx = (COLS - 1) / 2.0
    sy = sx / ASPECT

    for row in range(ROWS):
        for col in range(COLS):
            # World coords (-1..1 horizontally).
            x = (col - cx) / sx
            y = (row - cy) / sy

            lid = almond_half_height(x)
            if lid <= 0.0 or abs(y) > lid:
                continue  # outside the eye opening

            r = math.hypot(x, y)
            # Distance to the almond edge, for drawing the lid outline.
            edge = lid - abs(y)

            ch = " "
            if r <= PUPIL_R:
                # Pupil — solid and dense. On a dark terminal the ink glows
                # blue, so a clean filled pupil reads better than a punched
                # "catchlight" gap (an empty cell would look like a dark chip).
                ch = "@"
            elif r <= IRIS_R - LIMBAL:
                # Iris body — radial fibers: alternate density by angle so the
                # ring reads as muscle texture, brighter toward the pupil.
                ang = math.atan2(y, x)
                spoke = (math.sin(ang * 18) + 1) / 2  # 0..1
                near = 1.0 - (r - PUPIL_R) / (IRIS_R - PUPIL_R)  # 1 at pupil
                v = 0.45 * spoke + 0.55 * near
                ch = "%" if v > 0.62 else ("*" if v > 0.42 else ":")
            elif r <= IRIS_R:
                ch = "#"  # limbal ring — crisp dark edge of the iris
            else:
                # Sclera — keep mostly empty so the iris glows; faint dots only
                # very close to the lid so the almond still reads as an eye.
                if edge < 0.045:
                    ch = "."
                else:
                    ch = " "

            grid[row][col] = ch

    # Upper lash line: thicken the top lid so it reads unmistakably as an eye.
    for col in range(COLS):
        x = (col - cx) / sx
        lid = almond_half_height(x)
        if lid <= 0.0:
            continue
        top_row = int(round(cy - lid * sy))
        if 0 <= top_row < ROWS and grid[top_row][col] == " ":
            grid[top_row][col] = "-"

    lines = ["".join(r).rstrip() for r in grid]
    # Trim fully-blank leading/trailing rows.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def emit_literal() -> str:
    """Emit a ready-to-paste `_EYE_BANNER = (...)` block for iris_body_host.py."""
    art = render().split("\n")
    width = max(len(l) for l in art)
    center = width // 2
    label = "I  R  I  S"
    tagline = "the part of an eye that opens to let the light in"
    label_pad = " " * max(0, center - len(label) // 2)
    tag_pad = " " * max(0, center - len(tagline) // 2)

    out = ['_EYE_BANNER = (', '    "\\n" + _IRIS_BLUE']
    for l in art:
        out.append(f'    + {l!r} + "\\n"')
    out.append('    + _ANSI_RESET')
    out.append(f'    + _IRIS_CYAN + {label_pad + label!r} + "\\n" + _ANSI_RESET')
    out.append(f'    + _IRIS_BLUE + {tag_pad + tagline!r} + "\\n\\n" + _ANSI_RESET')
    out.append(')')
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if "--literal" in sys.argv:
        print(emit_literal())
    else:
        print(render())
