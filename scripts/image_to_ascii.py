"""Convert an image (or current camera frame) to ASCII art.

Pulls a JPEG from the orb HTTP camera endpoint (http://127.0.0.1:5876/api/v1/vision/latest_frame)
by default. Can also take --src <path> for a local image.

Uses a 70-character density ramp; corrects for ASCII cell aspect ratio (~2:1
tall:wide) so circles stay circular when rendered in a monospace font.

Usage:
    py -3.11 scripts/image_to_ascii.py [--src path] [--width 100] [--out path]
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from io import BytesIO
from pathlib import Path

# 70-step grayscale ramp, dark -> light. Standard Paul Bourke ramp.
RAMP = "$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,\"^`'. "

# Compact 10-step ramp (used if Zeke wants chunkier output later).
RAMP_SHORT = "@%#*+=-:. "


def image_to_ascii(img, width: int = 100, ramp: str = RAMP) -> str:
    """Convert a PIL Image to an ASCII string."""
    from PIL import Image  # type: ignore

    img = img.convert("L")  # grayscale
    w0, h0 = img.size
    # Each char cell is about 2x taller than wide, so we halve height by
    # multiplying by 0.5 to keep aspect.
    new_w = width
    new_h = max(1, int(h0 * (new_w / w0) * 0.5))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    n_ramp = len(ramp)
    pixels = img.getdata()
    rows: list[str] = []
    for r in range(new_h):
        chars = []
        for c in range(new_w):
            v = pixels[r * new_w + c]  # 0..255
            idx = int(v * (n_ramp - 1) / 255)
            chars.append(ramp[idx])
        rows.append("".join(chars))
    return "\n".join(rows)


def fetch_camera_frame_bytes() -> bytes | None:
    """Pull the latest camera JPEG from orb_http. Returns None if unavailable."""
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:5876/api/v1/vision/latest_frame", timeout=2.0
        ) as resp:
            if resp.status == 204:
                return None
            data = resp.read()
            if not data:
                return None
            return data
    except Exception as e:
        print(f"[image_to_ascii] camera fetch failed: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=None, help="path to a local image (jpg/png). If omitted, pulls camera frame.")
    ap.add_argument("--width", type=int, default=100)
    ap.add_argument("--out", default=None)
    ap.add_argument("--short-ramp", action="store_true", help="use 10-char ramp instead of 70-char")
    ap.add_argument("--invert", action="store_true", help="invert the ramp (light->dark)")
    args = ap.parse_args()

    from PIL import Image  # type: ignore

    if args.src:
        img = Image.open(args.src)
        source = args.src
    else:
        data = fetch_camera_frame_bytes()
        if data is None:
            print("ERROR: no source image. Either pass --src or ensure orb_http camera endpoint returns a frame.", file=sys.stderr)
            sys.exit(2)
        img = Image.open(BytesIO(data))
        source = "camera (orb_http)"

    ramp = RAMP_SHORT if args.short_ramp else RAMP
    if args.invert:
        ramp = ramp[::-1]

    ascii_text = image_to_ascii(img, width=args.width, ramp=ramp)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            f"# source: {source}\n# width: {args.width}\n# ramp: {'short(10)' if args.short_ramp else 'full(70)'}\n"
            f"# inverted: {args.invert}\n\n{ascii_text}\n",
            encoding="utf-8",
        )
        print(f"wrote: {out_path}")
    else:
        print(ascii_text)


if __name__ == "__main__":
    main()
