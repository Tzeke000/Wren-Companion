"""Image manipulation — halftone treatment of the iris macro.

Each cell in a grid becomes a dot whose size is proportional to the
local image darkness. Newspaper-print aesthetic.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw  # type: ignore


def halftone(src_path: str, out_path: str, cell: int = 14, dot_color: str = "#1c2030", bg_color: str = "#f4ebd8") -> None:
    src = Image.open(src_path).convert("L")
    sw, sh = src.size

    # Downsample for cell-level brightness
    gw, gh = sw // cell, sh // cell
    grid = src.resize((gw, gh), Image.LANCZOS)
    grid_pixels = grid.load()

    # Output canvas
    out_w, out_h = gw * cell, gh * cell
    out = Image.new("RGB", (out_w, out_h), bg_color)
    drw = ImageDraw.Draw(out)

    for r in range(gh):
        for c in range(gw):
            v = grid_pixels[c, r]  # 0..255
            darkness = (255 - v) / 255.0  # 1 = dark, 0 = light
            radius = (darkness ** 0.8) * (cell / 2 - 0.5)
            if radius < 0.5:
                continue
            cx = c * cell + cell / 2
            cy = r * cell + cell / 2
            drw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=dot_color,
            )

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path)
    print(f"wrote {out_path} ({out_w}x{out_h})")


if __name__ == "__main__":
    halftone(
        "art/sources/wikimedia_iris_photo_2304.jpg",
        "art/made/2026-05-19_halftone_iris.png",
        cell=12,
        dot_color="#22243a",
        bg_color="#f0e6cf",
    )
