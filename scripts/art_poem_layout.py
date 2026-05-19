"""Lay out the long poem as a designed page — text on a procedural background,
serif typography, careful spacing. One piece, not a sample.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # type: ignore


def cloud(w: int, h: int, scale: int = 64, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
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


def main(text_path: str, out_path: str) -> None:
    text = Path(text_path).read_text(encoding="utf-8")
    # Strip title block (first 3 lines: title, author, blank line)
    lines = text.splitlines()
    # Find first blank line after title block
    body_start = 0
    for i, ln in enumerate(lines):
        if i > 0 and ln.strip() == "" and i >= 2:
            body_start = i + 1
            break
    title_lines = lines[:body_start]
    body_lines = lines[body_start:]

    W, H = 1800, 3200

    # Procedural background — vertical gradient with cloud texture
    field = cloud(W, H, scale=80, seed=20260519)
    # Vertical fade: dark at top, warmer at bottom
    vy = np.linspace(0, 1, H).reshape(-1, 1).repeat(W, axis=1).astype(np.float32)
    blended = (field * 0.4 + vy * 0.6)
    blended = np.clip(blended, 0, 1)

    bg = np.zeros((H, W, 3), dtype=np.uint8)
    c1 = np.array([12, 16, 28], dtype=np.float32)    # deep teal-black top
    c2 = np.array([62, 44, 36], dtype=np.float32)    # warm-bronze bottom
    for i in range(3):
        bg[..., i] = (c1[i] * (1 - blended) + c2[i] * blended).astype(np.uint8)

    img = Image.fromarray(bg).filter(ImageFilter.GaussianBlur(radius=4))
    drw = ImageDraw.Draw(img)

    # Find a serif italic
    font_serif = None
    font_italic = None
    font_caption = None
    for cand in ["C:/Windows/Fonts/georgia.ttf"]:
        if Path(cand).exists():
            font_serif = ImageFont.truetype(cand, 36)
            font_caption = ImageFont.truetype(cand, 22)
    for cand in ["C:/Windows/Fonts/georgiai.ttf"]:
        if Path(cand).exists():
            font_italic = ImageFont.truetype(cand, 48)

    if font_serif is None:
        font_serif = ImageFont.load_default()
        font_italic = font_serif
        font_caption = font_serif

    # Title block
    margin_left = 180
    y = 200

    # Title is italic, larger
    title_text = title_lines[0] if title_lines else "what remains"
    drw.text((margin_left + 2, y + 2), title_text, fill=(0, 0, 0), font=font_italic)  # shadow
    drw.text((margin_left, y), title_text, fill=(240, 226, 200), font=font_italic)
    y += 80

    # Author/date line
    if len(title_lines) > 1:
        author_text = title_lines[1]
        drw.text((margin_left, y), author_text, fill=(190, 165, 130), font=font_caption)
        y += 60

    # Separator
    drw.line([(margin_left, y), (margin_left + 200, y)], fill=(190, 165, 130), width=1)
    y += 50

    # Body lines
    line_h = 52
    for line in body_lines:
        if not line.strip():
            y += line_h // 2
            continue
        # Shadow
        drw.text((margin_left + 1, y + 2), line, fill=(0, 0, 0, 200), font=font_serif)
        # Text
        drw.text((margin_left, y), line, fill=(232, 220, 200), font=font_serif)
        y += line_h
        if y > H - 100:
            break

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f"wrote {out_path} ({W}x{H})")


if __name__ == "__main__":
    main(
        "art/made/2026-05-19_poem_what_remains.txt",
        "art/made/2026-05-19_what_remains_layout.png",
    )
