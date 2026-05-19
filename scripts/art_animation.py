"""Animation — the data-eye with the catchlight rotating to show time passing.

24 frames, one for each hour, rotating the catchlight position around the
pupil edge. Loops continuously. Saved as animated GIF.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import List

from PIL import Image, ImageDraw  # type: ignore


def frame_at_minute_of_day(minute_of_day: int, mood_emotions, pupil_r: int, W: int = 1200, H: int = 800) -> Image.Image:
    img = Image.new("RGB", (W, H), (12, 10, 16))
    drw = ImageDraw.Draw(img)

    cx, cy = W // 2, H // 2
    eye_w = 950
    eye_h = 525
    iris_outer_r = 205
    iris_inner_r = 45

    # Sclera
    drw.ellipse([cx - eye_w // 2, cy - eye_h // 2, cx + eye_w // 2, cy + eye_h // 2], fill=(218, 212, 204))

    # Iris spokes
    n_em = max(1, len(mood_emotions))
    max_w = mood_emotions[0][1] if mood_emotions else 1.0
    n_spokes = 360
    for i in range(n_spokes):
        angle = (i / n_spokes) * 2 * math.pi
        ename, w = mood_emotions[i % n_em]
        nw = w / max_w if max_w > 0 else 0.0
        bright = int(80 + nw * 170)
        r, g, b = int(bright * 0.85), int(bright * 0.95), int(bright * 0.45)
        x0 = cx + int(iris_inner_r * math.cos(angle))
        y0 = cy + int(iris_inner_r * math.sin(angle))
        x1 = cx + int(iris_outer_r * math.cos(angle))
        y1 = cy + int(iris_outer_r * math.sin(angle))
        drw.line([(x0, y0), (x1, y1)], fill=(r, g, b), width=2)

    # Limbal + inner ring
    drw.ellipse([cx - iris_outer_r, cy - iris_outer_r, cx + iris_outer_r, cy + iris_outer_r],
                outline=(15, 18, 12), width=5)
    drw.ellipse([cx - iris_inner_r, cy - iris_inner_r, cx + iris_inner_r, cy + iris_inner_r],
                outline=(10, 8, 5), width=2)

    # Pupil
    drw.ellipse([cx - pupil_r, cy - pupil_r, cx + pupil_r, cy + pupil_r], fill=(4, 4, 4))

    # Catchlight — angle determined by minute_of_day
    catch_angle = (minute_of_day / 1440.0) * 2 * math.pi - math.pi / 2
    catch_offset = pupil_r - 12
    cl_x = cx + int(catch_offset * math.cos(catch_angle))
    cl_y = cy + int(catch_offset * math.sin(catch_angle))
    drw.ellipse([cl_x - 9, cl_y - 13, cl_x + 9, cl_y + 13], fill=(250, 248, 240))

    # Mask outside the almond
    mask = Image.new("L", (W, H), 0)
    mdrw = ImageDraw.Draw(mask)
    mdrw.ellipse([cx - eye_w // 2, cy - eye_h // 2, cx + eye_w // 2, cy + eye_h // 2], fill=255)
    skin = Image.new("RGB", (W, H), (78, 60, 52))
    out = Image.composite(img, skin, mask)

    # Hour label bottom
    return out


def main(out_path: str) -> None:
    mood = json.loads(Path("state/iris_mood.json").read_text(encoding="utf-8"))
    emotions = sorted(mood.get("emotion_weights", {}).items(), key=lambda x: -x[1])

    # 24 frames, one per hour. Catchlight sweeps full clock.
    frames: List[Image.Image] = []
    for hour in range(24):
        minute_of_day = hour * 60
        # Pupil gets larger at night (24h cycle, dilating midnight, contracting noon)
        pupil_r = int(40 + 22 * math.cos((hour - 12) * math.pi / 12))
        f = frame_at_minute_of_day(minute_of_day, emotions, pupil_r)
        frames.append(f)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=180,   # ms per frame
        loop=0,
        disposal=2,
    )
    print(f"wrote {out_path} ({len(frames)} frames)")


if __name__ == "__main__":
    main("art/made/2026-05-19_animation_eye_day_cycle.gif")
