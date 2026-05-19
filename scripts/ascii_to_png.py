"""Render an ASCII art .txt file to a PNG using a monospace font.

So Discord can show the result inline without monospace-font-dependent wrapping.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont  # type: ignore


def render(text: str, font_size: int = 12, fg: str = "white", bg: str = "black") -> Image.Image:
    # Try to find a monospace font. Fall back to default if not.
    font = None
    for candidate in [
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/cour.ttf",
        "C:/Windows/Fonts/lucon.ttf",
    ]:
        try:
            font = ImageFont.truetype(candidate, font_size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    lines = text.splitlines()
    if not lines:
        lines = [""]

    # Measure with a dummy draw.
    dummy = Image.new("RGB", (1, 1))
    drw = ImageDraw.Draw(dummy)
    # Use a wide line to determine character width.
    sample = max(lines, key=len) if lines else "M"
    bbox = drw.textbbox((0, 0), sample, font=font)
    line_w = bbox[2] - bbox[0]
    bbox2 = drw.textbbox((0, 0), "Mg", font=font)
    char_h = (bbox2[3] - bbox2[1])
    line_h = int(char_h * 1.15)

    w = line_w + 20
    h = line_h * len(lines) + 20
    img = Image.new("RGB", (w, h), bg)
    drw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        drw.text((10, 10 + i * line_h), line, fill=fg, font=font)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--out", required=True)
    ap.add_argument("--font-size", type=int, default=12)
    ap.add_argument("--fg", default="white")
    ap.add_argument("--bg", default="black")
    args = ap.parse_args()

    text = Path(args.src).read_text(encoding="utf-8")
    # Strip leading "# source:..." comment lines from the ASCII file's header.
    cleaned = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
    img = render(cleaned, args.font_size, args.fg, args.bg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"wrote {out} ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    main()
