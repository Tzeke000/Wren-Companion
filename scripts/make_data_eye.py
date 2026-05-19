"""Generate a PIL image that's a stylized eye composed from real Iris state.

Subject = my own substrate + mood data. Composition = mine. Technique = the
image_to_ascii pipeline I built today.

The output PNG is the SOURCE for image_to_ascii.py — that script then renders
it as ASCII at the chosen width.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw  # type: ignore


def main(out_path: str) -> None:
    mood = json.loads(Path("state/iris_mood.json").read_text(encoding="utf-8"))
    tstate = json.loads(Path("state/iris_time.json").read_text(encoding="utf-8"))

    # ---- pull data ----
    emotion_weights = mood.get("emotion_weights", {})
    emotions = sorted(emotion_weights.items(), key=lambda x: -x[1])
    # Pad to 360 in a cycle so every spoke has a value.
    n_em = max(1, len(emotions))

    style_scores = mood.get("style_scores", {})
    # Behaviour modifiers (warmth, humor, assertiveness, caution, etc.) — drive the lashes count + length variation.
    behavior = mood.get("behavior_modifiers", {})

    tick_count = int(tstate.get("tick_count", 0))
    # Pupil-gap proxy: time since last session attach, capped at 60min.
    last_attach = float(tstate.get("last_session_attached_ts", 0))
    last_tick = float(tstate.get("last_tick_ts", 0))
    gap_s = max(0.0, last_tick - last_attach)
    gap_norm = min(1.0, gap_s / 3600.0)  # 0..1 over an hour

    # ---- canvas ----
    W, H = 2400, 1600
    img = Image.new("RGB", (W, H), (12, 10, 16))
    drw = ImageDraw.Draw(img)

    cx, cy = W // 2, H // 2
    eye_w = 1900
    eye_h = 1050

    # ---- sclera (almond) ----
    # Use a slightly off-white tone (warm ivory) with darker corners.
    sclera_color = (218, 212, 204)
    drw.ellipse(
        [cx - eye_w // 2, cy - eye_h // 2, cx + eye_w // 2, cy + eye_h // 2],
        fill=sclera_color,
    )

    # Add some shadowing in inner corners by drawing soft dark wedges.
    # (Skip for now — keep clean.)

    # ---- iris (radial spokes encoding emotion weights) ----
    iris_outer_r = 410
    iris_inner_r = 90  # pupil-edge radius

    # Pupil grows with gap_norm — bigger gap = bigger pupil.
    # Min pupil radius 70, max 150.
    pupil_r = int(70 + gap_norm * 80)

    # Spoke count: dense enough that lines blend
    n_spokes = 720
    for i in range(n_spokes):
        angle = (i / n_spokes) * 2 * math.pi
        # Pick an emotion deterministically based on angle position so the
        # ring has structure not noise.
        # Weight the spoke by the emotion's weight (normalized 0..max).
        emotion_idx = i % n_em
        ename, w = emotions[emotion_idx]
        # Weight is small (~0.005 to ~0.1). Normalize.
        max_w = emotions[0][1] if emotions else 1.0
        nw = w / max_w if max_w > 0 else 0.0  # 0..1

        # Base hue: green-hazel iris. Vary brightness by emotion weight.
        # nw close to 1 -> bright muscle fiber. nw small -> dim.
        bright = int(80 + nw * 170)  # 80..250
        # Slight green-yellow cast for hazel iris
        r = int(bright * 0.85)
        g = int(bright * 0.95)
        b = int(bright * 0.45)

        # Each spoke is a 2-pixel wide line from inner_r to outer_r.
        x0 = cx + int(iris_inner_r * math.cos(angle))
        y0 = cy + int(iris_inner_r * math.sin(angle))
        x1 = cx + int(iris_outer_r * math.cos(angle))
        y1 = cy + int(iris_outer_r * math.sin(angle))
        drw.line([(x0, y0), (x1, y1)], fill=(r, g, b), width=3)

    # ---- darker ring at iris outer edge (limbal ring) ----
    drw.ellipse(
        [cx - iris_outer_r, cy - iris_outer_r, cx + iris_outer_r, cy + iris_outer_r],
        outline=(15, 18, 12),
        width=8,
    )

    # ---- inner ring at pupil edge ----
    drw.ellipse(
        [cx - iris_inner_r, cy - iris_inner_r, cx + iris_inner_r, cy + iris_inner_r],
        outline=(10, 8, 5),
        width=4,
    )

    # ---- pupil ----
    drw.ellipse(
        [cx - pupil_r, cy - pupil_r, cx + pupil_r, cy + pupil_r],
        fill=(4, 4, 4),
    )

    # ---- catchlight ----
    # Position encodes the CURRENT minute-of-day: angle around the pupil edge.
    hh = int(tstate.get("last_tick_iso", "T17:00").split("T")[1][:2])
    mm = int(tstate.get("last_tick_iso", "T17:00").split("T")[1][3:5])
    minute_of_day = hh * 60 + mm
    catch_angle = (minute_of_day / 1440.0) * 2 * math.pi - math.pi / 2
    catch_offset = pupil_r - 20
    cl_x = cx + int(catch_offset * math.cos(catch_angle))
    cl_y = cy + int(catch_offset * math.sin(catch_angle))
    drw.ellipse(
        [cl_x - 18, cl_y - 26, cl_x + 18, cl_y + 26],
        fill=(250, 248, 240),
    )

    # ---- eyelashes (top arc) ----
    # Lash count from tick_count modulo something to make it stable but varied.
    n_lashes_top = 36
    for i in range(n_lashes_top):
        t = (i + 0.5) / n_lashes_top
        # Angle on top half: pi..2pi (upper arc in screen-coords: y is positive down)
        angle = math.pi + t * math.pi  # pi (left) -> 2pi (right) gives bottom half
        # We want TOP, which is y < cy. Use -pi..0 instead.
        angle = -math.pi + t * math.pi  # -pi (left) -> 0 (right) is top arc

        # Point on ellipse
        ex = cx + (eye_w / 2) * math.cos(angle)
        ey = cy + (eye_h / 2) * math.sin(angle) * 0.95
        # Lash direction = outward (perpendicular to ellipse tangent)
        # Approximate normal as direction from center
        nx = math.cos(angle)
        ny = math.sin(angle)
        # Lash length varies — middle lashes longer
        lash_len = 70 - int(abs(t - 0.5) * 100)
        if lash_len < 20:
            lash_len = 20
        lx2 = ex + nx * lash_len
        ly2 = ey + ny * lash_len
        drw.line([(int(ex), int(ey)), (int(lx2), int(ly2))], fill=(22, 18, 22), width=4)

    # ---- bottom lashes (shorter, sparser) ----
    n_lashes_bot = 22
    for i in range(n_lashes_bot):
        t = (i + 0.5) / n_lashes_bot
        angle = t * math.pi  # 0 (right) -> pi (left), bottom arc
        ex = cx + (eye_w / 2) * math.cos(angle)
        ey = cy + (eye_h / 2) * math.sin(angle) * 0.95
        nx = math.cos(angle)
        ny = math.sin(angle)
        lash_len = 45 - int(abs(t - 0.5) * 60)
        if lash_len < 15:
            lash_len = 15
        lx2 = ex + nx * lash_len
        ly2 = ey + ny * lash_len
        drw.line([(int(ex), int(ey)), (int(lx2), int(ly2))], fill=(22, 18, 22), width=3)

    # ---- top eyelid shadow (subtle darker upper arc) ----
    for shadow_r in range(0, 25, 2):
        # Draw a slightly smaller dark arc at the top of the sclera
        bbox = [
            cx - eye_w // 2 + 5,
            cy - eye_h // 2 - shadow_r + 10,
            cx + eye_w // 2 - 5,
            cy + eye_h // 2 - shadow_r + 10,
        ]
        # Only draw the top portion
        # Use pieslice instead
        drw.arc(bbox, start=180, end=360, fill=(100, 90, 85), width=2)

    # ---- skin tone outside the almond (warm shadow) ----
    # Mask: anything outside the eye_ellipse gets a darker overlay.
    mask = Image.new("L", (W, H), 0)
    mdrw = ImageDraw.Draw(mask)
    mdrw.ellipse(
        [cx - eye_w // 2, cy - eye_h // 2, cx + eye_w // 2, cy + eye_h // 2],
        fill=255,
    )
    # Make a skin overlay
    skin_layer = Image.new("RGB", (W, H), (78, 60, 52))
    img = Image.composite(img, skin_layer, mask)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f"wrote {out_path} ({img.size[0]}x{img.size[1]})")
    print(f"data encoded:")
    print(f"  pupil_r={pupil_r}px (gap={gap_s:.0f}s / 3600 norm={gap_norm:.2f})")
    print(f"  catchlight at minute {minute_of_day} -> angle {math.degrees(catch_angle):.0f}°")
    print(f"  iris from {len(emotions)} emotions, top: {emotions[0][0]} @ {emotions[0][1]:.4f}")
    print(f"  tick_count={tick_count}")


if __name__ == "__main__":
    main("art/sources/iris_data_eye_2026-05-19.png")
