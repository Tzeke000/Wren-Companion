"""rule30.py — elementary cellular automaton as ASCII art.

art-block piece #5, made 2026-05-20 ~16:25 EDT

Wolfram's Rule 30 produces aperiodic chaos from a single seed cell.
Each row is derived from the row above by applying the rule to every
three-cell window. The result, drawn left-to-right with # for live
and space for dead, looks like a triangular spread of crystalline noise.

This isn't trying to mean anything beyond "look at the shape simple
rules make when iterated." Generative art's purest form: a one-bit
input becomes a self-similar pattern by repetition alone.
"""
from __future__ import annotations

import sys
from pathlib import Path

WIDTH = 79
ROWS = 30
RULE = 30  # 0b00011110 — Wolfram's chaotic rule


def step(row: list[int], rule: int = RULE) -> list[int]:
    """Apply the rule to every cell, given periodic boundary conditions
    (left and right edges wrap). Returns the next row."""
    n = len(row)
    out = [0] * n
    for i in range(n):
        left = row[(i - 1) % n]
        center = row[i]
        right = row[(i + 1) % n]
        pattern = (left << 2) | (center << 1) | right
        out[i] = (rule >> pattern) & 1
    return out


def render(rows: int = ROWS, width: int = WIDTH, rule: int = RULE) -> str:
    """Produce the full triangle as a single string."""
    row = [0] * width
    row[width // 2] = 1  # single seed in the middle

    lines: list[str] = []
    for _ in range(rows):
        lines.append("".join("#" if c else " " for c in row))
        row = step(row, rule)
    return "\n".join(lines)


def main() -> int:
    art = render()
    print(art)

    here = Path(__file__).resolve().parent
    out_path = here / "2026-05-20_16-25_rule30_one_rendering.txt"
    out_path.write_text(art, encoding="utf-8")
    print(f"\n[saved -> {out_path.name}]", file=sys.stderr)
    print("\n(rule 30, 30 rows, single-cell seed, periodic boundaries.", file=sys.stderr)
    print(" wolfram's classic. aperiodic — never repeats. chaotic in the", file=sys.stderr)
    print(" middle, looks ordered at the edges. one bit becomes a forest.)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
