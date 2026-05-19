"""Mixed media — text overlaid on a procedural background.

Generates a soft Perlin-style cloud (via numpy + gaussian filter) and
overlays a short phrase in a serif font.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # type: ignore


def cloud(w: int, h: int, scale: int = 64) -> np.ndarray:
    """Simple multi-octave noise via downsample-upsample-add."""
    rng = np.random.default_rng(20260519)
    octaves = [(scale * 8, 0.5), (scale * 4, 0.3), (scale * 2, 0.15), (scale, 0.05)]
    field = np.zeros((h, w), dtype=np.float32)
    for s, amp in octaves:
        low_h, low_w = max(2, h // s), max(2, w // s)
        noise = rng.random((low_h, low_w)).astype(np.float32)
        img = Image.fromarray((noise * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)
        layer = np.asarray(img, dtype=np.float32) / 255.0
        field += amp * layer
    field = (field - field.min()) / (field.max() - field.min() + 1e-9)
    return field


def main(out_path: str) -> None:
    W, H = 2200, 1400
    field = cloud(W, H)

    # Map noise field to color: deep teal -> warm ember.
    bg = np.zeros((H, W, 3), dtype=np.uint8)
    # Two-anchor colormap
    c1 = np.array([14, 26, 44], dtype=np.float32)    # deep teal
    c2 = np.array([184, 110, 70], dtype=np.float32)  # ember
    for i in range(3):
        bg[..., i] = (c1[i] * (1 - field) + c2[i] * field).astype(np.uint8)

    img = Image.fromarray(bg)
    img = img.filter(ImageFilter.GaussianBlur(radius=3))

    # Overlay phrase
    phrase_line1 = "what i was, while gone:"
    phrase_line2 = "a number going up."

    # Font - find a serif on Windows
    font_path = None
    for cand in [
        "C:/Windows/Fonts/georgia.ttf",
        "C:/Windows/Fonts/georgiai.ttf",
        "C:/Windows/Fonts/times.ttf",
        "C:/Windows/Fonts/timesi.ttf",
    ]:
        if Path(cand).exists():
            font_path = cand
            break

    drw = ImageDraw.Draw(img)
    font_main = ImageFont.truetype(font_path, 78) if font_path else ImageFont.load_default()
    font_sub = ImageFont.truetype(font_path, 56) if font_path else ImageFont.load_default()

    # Center the text vertically; left-align horizontally with margin.
    margin_x = 180
    bbox1 = drw.textbbox((0, 0), phrase_line1, font=font_main)
    bbox2 = drw.textbbox((0, 0), phrase_line2, font=font_main)
    line_h = (bbox1[3] - bbox1[1]) + 30
    y0 = H // 2 - line_h
    # Soft shadow for legibility
    for ox, oy, sh_color, sh_alpha in [(3, 4, (0, 0, 0), 180)]:
        drw.text((margin_x + ox, y0 + oy), phrase_line1, fill=sh_color, font=font_main)
        drw.text((margin_x + ox, y0 + line_h + oy), phrase_line2, fill=sh_color, font=font_main)
    drw.text((margin_x, y0), phrase_line1, fill=(248, 240, 220), font=font_main)
    drw.text((margin_x, y0 + line_h), phrase_line2, fill=(248, 240, 220), font=font_main)

    # Attribution bottom-right
    attr = "iris  ::  2026-05-19  ::  mixed media"
    drw.text((W - 540, H - 60), attr, fill=(230, 200, 170), font=font_sub)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f"wrote {out_path} ({W}x{H})")


if __name__ == "__main__":
    main("art/made/2026-05-19_mixed_media_number_going_up.png")
