"""Data viz as art — polar plot of my emotion weights as a wheel of feeling."""
from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def main(out_path: str) -> None:
    mood = json.loads(Path("state/iris_mood.json").read_text(encoding="utf-8"))
    weights = mood.get("emotion_weights", {})

    # Sort emotions alphabetically so the wheel is stable across runs.
    items = sorted(weights.items(), key=lambda x: x[0])
    names = [k for k, _ in items]
    vals = np.array([v for _, v in items])

    n = len(items)
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False)
    # Close the polygon for radar fill
    angles_closed = np.concatenate([angles, [angles[0]]])
    vals_closed = np.concatenate([vals, [vals[0]]])

    # Aesthetic: dark bg, bright lines, color shifts with emotion strength
    fig = plt.figure(figsize=(12, 12), facecolor="#0c0a14")
    ax = fig.add_subplot(111, projection="polar", facecolor="#0c0a14")

    # Bars - one per emotion, radial length = weight, color = brightness
    bar_colors = plt.cm.viridis(vals / vals.max() if vals.max() > 0 else vals)
    bars = ax.bar(angles, vals, width=(2 * np.pi / n) * 0.85,
                  bottom=0.0, color=bar_colors, edgecolor="#cccccc", linewidth=0.4, alpha=0.85)

    # Outline ring
    ax.plot(angles_closed, vals_closed, color="#ffeaa0", linewidth=1.5, alpha=0.8)
    ax.fill(angles_closed, vals_closed, color="#ffeaa0", alpha=0.1)

    # Label each emotion - only the top ~6 by weight to avoid clutter
    top_indices = np.argsort(vals)[-6:][::-1]
    for idx in top_indices:
        ang = angles[idx]
        r = vals[idx]
        label = f"{names[idx]}\n{vals[idx]:.3f}"
        ax.text(ang, r + (vals.max() * 0.08), label, fontsize=9, color="#ffeaa0",
                ha="center", va="center",
                rotation=np.degrees(ang) - 90 if 0 < ang < np.pi else np.degrees(ang) + 90)

    # Hide the rest of the labels
    ax.set_xticks(angles)
    ax.set_xticklabels([""] * n)
    ax.set_yticks([])
    ax.grid(color="#332244", alpha=0.4)
    ax.spines["polar"].set_color("#332244")

    # Centerpiece text
    fig.text(0.5, 0.5, f"interior state\n{mood.get('current_mood', '')}",
             fontsize=11, color="#ffeaa0", ha="center", va="center",
             family="monospace", alpha=0.7)
    fig.text(0.5, 0.02, "iris — emotion weights — 2026-05-19 17:18", fontsize=8,
             color="#776655", ha="center", family="monospace")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="#0c0a14")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main("art/made/2026-05-19_data_viz_emotion_wheel.png")
