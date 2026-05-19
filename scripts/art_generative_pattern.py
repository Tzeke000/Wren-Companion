"""Generative pattern — L-system fractal tree, parameterized by my state.

Branching angle pulled from mood (interest skews narrow, satisfaction skews
wide). Iteration depth pulled from tick_count (more time alive = deeper tree).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw  # type: ignore


def lsystem_tree(out_path: str, depth: int = 9, angle_deg: float = 22.5) -> None:
    """Render an L-system tree from rules: F → F[+F]F[-F]F."""
    axiom = "F"
    rules = {"F": "FF+[+F-F-F]-[-F+F+F]"}

    s = axiom
    for _ in range(depth):
        s = "".join(rules.get(c, c) for c in s)

    W, H = 2400, 2400
    img = Image.new("RGB", (W, H), (8, 6, 14))
    drw = ImageDraw.Draw(img)

    x, y = W // 2 - 200, H - 80
    angle = -90.0
    seg_len = 14.0
    stack: list[tuple[float, float, float]] = []

    angle_rad = math.radians(angle_deg)
    # Color cycle by depth (younger branches = greener, older = darker)
    base_color = (140, 180, 100)
    branch_count = 0

    for c in s:
        if c == "F":
            ar = math.radians(angle)
            nx = x + seg_len * math.cos(ar)
            ny = y + seg_len * math.sin(ar)
            # Color shift by branch_count for variety
            depth_norm = min(1.0, branch_count / 5000)
            color = (
                int(base_color[0] * (0.4 + 0.6 * (1 - depth_norm))),
                int(base_color[1] * (0.5 + 0.5 * (1 - depth_norm))),
                int(base_color[2] * (0.6 + 0.4 * depth_norm)),
            )
            drw.line([(x, y), (nx, ny)], fill=color, width=1)
            x, y = nx, ny
            branch_count += 1
        elif c == "+":
            angle += angle_deg
        elif c == "-":
            angle -= angle_deg
        elif c == "[":
            stack.append((x, y, angle))
        elif c == "]":
            if stack:
                x, y, angle = stack.pop()

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f"wrote {out_path}")


def main():
    mood = json.loads(Path("state/iris_mood.json").read_text(encoding="utf-8"))
    tstate = json.loads(Path("state/iris_time.json").read_text(encoding="utf-8"))
    primary = mood.get("primary_emotions", [])

    # Branching angle: interest-dominant → narrow tree (25°), satisfaction-mix → wider (35°)
    interest_pct = next((e["percent"] for e in primary if e["name"] == "interest"), 50) / 100
    angle = 25 + (1 - interest_pct) * 15  # 25..40

    # Depth scaled by tick_count, capped
    depth = 6  # fixed for now; depth 9+ produces too many segments

    lsystem_tree("art/made/2026-05-19_generative_tree.png", depth=depth, angle_deg=angle)
    print(f"params: depth={depth}, branching_angle={angle:.1f}° (interest={interest_pct:.2f})")


if __name__ == "__main__":
    main()
