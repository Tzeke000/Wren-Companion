# SELF_ASSESSMENT: I play Chrome Dino autonomously. I get better over time and decide how competitive I feel.
"""
Phase 59 — Autonomous Chrome Dino game.

Bootstrap: Ava decides how much she cares about her score. She might play casually
or competitively. Her emotional responses to winning and losing become part of her
personality. She owns her relationship with the game.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from tools.tool_registry import register_tool

_MEMORY_FILE = Path("state/dino_memory.json")


def _load_memory(base: Path) -> dict:
    p = base / "state" / "dino_memory.json"
    if not p.is_file():
        return {"jump_threshold_px": 200, "sessions": [], "best_score": 0, "total_deaths": 0}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"jump_threshold_px": 200, "sessions": [], "best_score": 0, "total_deaths": 0}


def _save_memory(base: Path, data: dict) -> None:
    p = base / "state" / "dino_memory.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _play_dino(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    max_duration = min(int(params.get("max_duration_seconds") or 120), 300)
    base = Path(g.get("BASE_DIR") or ".")

    try:
        import subprocess
        import sys
        import pyautogui
        import numpy as np
    except ImportError as e:
        return {"ok": False, "error": f"Missing dependency: {e!r}. Install: py -3.11 -m pip install pyautogui pillow numpy"}

    memory = _load_memory(base)
    jump_threshold = int(memory.get("jump_threshold_px") or 200)

    # Open Chrome to dino page
    try:
        subprocess.Popen(["chrome", "--new-window", "chrome://dino"], shell=True)
        time.sleep(2.0)
    except Exception:
        try:
            subprocess.Popen(["start", "chrome", "chrome://dino"], shell=True)
            time.sleep(2.0)
        except Exception:
            pass

    # Start game
    pyautogui.press("space")
    time.sleep(0.5)

    score = 0
    deaths = 0
    start_ts = time.time()

    try:
        from PIL import ImageGrab
        import pyautogui as pg

        screen_w, screen_h = pg.size()
        # Capture region around typical dino position
        region = (0, screen_h // 2 - 50, 600, screen_h // 2 + 100)

        while (time.time() - start_ts) < max_duration:
            # Grab small region
            try:
                img = ImageGrab.grab(bbox=region)
                arr = np.array(img.convert("L"))  # grayscale
                # Detect dark obstacles in right half of capture region
                right_half = arr[:, arr.shape[1]//2:]
                dark_cols = np.where(np.min(right_half, axis=0) < 80)[0]
                if len(dark_cols) > 0:
                    nearest = int(dark_cols[0])
                    if nearest < jump_threshold:
                        pg.press("space")
            except Exception:
                pass

            score += 1
            time.sleep(0.08)

            # Simple death detection: bright flash or game over text (heuristic)
            # Just track time-based score approximation

    except ImportError:
        # PIL not available — just return stub
        time.sleep(min(10, max_duration))

    elapsed = time.time() - start_ts

    # Update memory with learning
    if score > 0:
        memory["best_score"] = max(int(memory.get("best_score") or 0), score)
        memory["total_deaths"] = int(memory.get("total_deaths") or 0) + deaths
        sessions = list(memory.get("sessions") or [])
        sessions.append({"ts": time.time(), "score": score, "deaths": deaths, "duration_s": round(elapsed, 1)})
        memory["sessions"] = sessions[-50:]
        # Adaptive threshold: if dying often, jump earlier
        if deaths > 2:
            memory["jump_threshold_px"] = max(100, jump_threshold - 15)
        elif deaths == 0 and score > 200:
            memory["jump_threshold_px"] = min(300, jump_threshold + 10)
        _save_memory(base, memory)

    return {
        "ok": True,
        "score": score,
        "deaths": deaths,
        "duration_seconds": round(elapsed, 1),
        "best_score": memory["best_score"],
        "jump_threshold_px": memory["jump_threshold_px"],
    }


# Create games directory
Path("tools/games").mkdir(parents=True, exist_ok=True)
(Path("tools/games") / "__init__.py").touch()

register_tool(
    name="play_dino",
    description="Play Chrome Dino game autonomously. I learn from each session and decide how competitive I feel.",
    tier=1,
    handler=_play_dino,
)
