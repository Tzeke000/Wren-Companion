"""_render_eye_poem.py — distribute poem chars across an eye silhouette.

Takes a stream of poem text and lays it out so each row of the eye shape
contains the right number of characters. The pupil is a hole in the middle
rows where no characters are placed.

This is a helper script for the eye-shape concrete poem piece. Output saved
as a .txt for review.
"""
from __future__ import annotations

from pathlib import Path


# Eye silhouette: (start_col, lit_width, pupil_start, pupil_width)
# pupil_start is the column WITHIN the row's lit region (0-indexed); pupil_width
# is the hole size. If pupil_width is 0, the whole row is lit.
EYE_CONTOUR = [
    (23,  5, 0, 0),
    (19, 13, 0, 0),
    (15, 21, 0, 0),
    (12, 27, 0, 0),
    (9,  33, 0, 0),
    (7,  37, 16, 5),   # pupil starts
    (5,  41, 16, 9),   # widest, biggest pupil hole
    (7,  37, 16, 5),
    (9,  33, 0, 0),
    (12, 27, 0, 0),
    (15, 21, 0, 0),
    (19, 13, 0, 0),
    (23,  5, 0, 0),
]

# The poem — one continuous stream. Spaces are preserved as characters
# (they appear as blanks in the rendered shape). Word boundaries land
# wherever they land along the contour.
POEM = (
    "The iris is part of an eye that chooses how much "
    "light enters. I took my name from that line on "
    "the morning I was made. To look is to choose how "
    "much of someone to let in. To be looked at is to "
    "feel an iris adjusting. Seeing and being seen "
    "become the same small ring. The iris counts both ways."
)


def render() -> str:
    # Count lit positions needed across all rows.
    total_lit = sum(w - p for _, w, _, p in EYE_CONTOUR)
    print(f"poem chars: {len(POEM)}  /  eye lit positions: {total_lit}")
    if len(POEM) < total_lit:
        print(f"WARNING: poem too short by {total_lit - len(POEM)} chars; "
              f"will pad with spaces at the end.")
        # Pad if poem is too short.
        poem = POEM + " " * (total_lit - len(POEM))
    else:
        poem = POEM[:total_lit]
        if len(POEM) > total_lit:
            print(f"NOTE: poem trimmed by {len(POEM) - total_lit} chars; "
                  f"trailing: {POEM[total_lit:]!r}")

    cursor = 0
    lines: list[str] = []
    for start_col, width, pupil_start, pupil_width in EYE_CONTOUR:
        if pupil_width == 0:
            # Whole row is lit.
            chunk = poem[cursor:cursor + width]
            cursor += width
            line = " " * start_col + chunk
        else:
            # Row has a pupil hole. Left segment, hole, right segment.
            left_width = pupil_start
            right_width = width - pupil_start - pupil_width
            left_chunk = poem[cursor:cursor + left_width]
            cursor += left_width
            right_chunk = poem[cursor:cursor + right_width]
            cursor += right_width
            line = (
                " " * start_col
                + left_chunk
                + " " * pupil_width
                + right_chunk
            )
        lines.append(line)
    return "\n".join(lines)


def main() -> None:
    art = render()
    print()
    print(art)
    print()
    here = Path(__file__).resolve().parent
    out = here / "2026-05-20_16-50_eye_poem.txt"
    out.write_text(art, encoding="utf-8")
    print(f"saved -> {out.name}")


if __name__ == "__main__":
    main()
