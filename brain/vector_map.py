"""vector_map.py — Iris's ROOM BLUEPRINT from Vector's native nav-map (2026-07-14).

Zeke's idea (2026-07-14 eve): "as you're moving around you should make a
blueprint of the room so you don't get lost and can reference it as you're
building it." Vector already builds one — robot.nav_map.latest_nav_map is a
NavMapGrid: a quad-tree occupancy map (root 2048mm) he fills in as he drives,
tagging each cell clear / obstacle / cliff / edge. This module walks that tree
into a blueprint JSON + a top-down PNG I (and Zeke) can reference and watch
grow. Vector's pose and the charger pose are drawn as reference anchors so
"where am I / where's home" is always answerable from the map.

Capture happens INSIDE a control session (nav_map populates while he moves and
holds control). Call capture_map(robot) during a drive; or map_snapshot() for
a one-shot.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRAME = REPO / "state" / "vector"
MAP_JSON = FRAME / "room_map.json"
MAP_PNG = FRAME / "room_map.png"

# NavNodeContentType.name -> (label, RGB) for rendering
_CONTENT_RGB = {
    "Unknown":               ("unknown",  (40, 40, 46)),
    "ClearOfObstacle":       ("clear",    (210, 210, 200)),
    "ClearOfCliff":          ("clear",    (210, 210, 200)),
    "ObstacleCube":          ("cube",     (80, 140, 255)),
    "ObstacleProximity":     ("obstacle", (220, 70, 70)),
    "ObstacleProximityExplored": ("obstacle", (220, 70, 70)),
    "ObstacleUnrecognized":  ("obstacle", (220, 70, 70)),
    "Cliff":                 ("cliff",    (20, 20, 20)),
    "InterestingEdge":       ("edge",     (240, 200, 60)),
    "NonInterestingEdge":    ("edge",     (120, 110, 60)),
}
_DEFAULT_RGB = (60, 60, 66)

# NavNodeContentType is stored as an INT on the node (0..9) — map to names.
_INT_TO_NAME = {
    0: "Unknown", 1: "ClearOfObstacle", 2: "ClearOfCliff", 3: "ObstacleCube",
    4: "ObstacleProximity", 5: "ObstacleProximityExplored",
    6: "ObstacleUnrecognized", 7: "Cliff", 8: "InterestingEdge",
    9: "NonInterestingEdge",
}


def _leaves(node, out: list) -> None:
    """Recurse the quad-tree into leaf cells: {cx, cy, size, content}."""
    if node is None:
        return
    children = getattr(node, "children", None)
    if children:
        for c in children:
            _leaves(c, out)
        return
    try:
        content = getattr(node, "content", None)
        if hasattr(content, "value"):
            content = content.value
        if hasattr(content, "name"):
            name = content.name
        elif content is None:
            name = "Unknown"
        else:
            name = _INT_TO_NAME.get(int(content), str(content))
        center = node.center
        out.append({
            "cx": round(float(center.x), 1),
            "cy": round(float(center.y), 1),
            "size": round(float(node.size), 1),
            "content": name,
        })
    except Exception:
        pass


def capture_map(robot, tag: str = "") -> dict:
    """Walk the current nav-map into a blueprint dict + render a PNG. Returns
    {ok, cells, clear, obstacle, pose, charger, png}. Best-effort — a missing
    nav-map returns ok=False rather than raising."""
    # init the feed FIRST — reading latest_nav_map before init RAISES.
    try:
        robot.nav_map.init_nav_map_feed(frequency=0.5)
    except Exception:
        pass
    m = None
    for _ in range(4):
        try:
            m = robot.nav_map.latest_nav_map
        except Exception:
            m = None
        if m is not None:
            break
        time.sleep(1.0)
    if m is None:
        return {"ok": False, "error": "no nav_map yet (drive to populate it)"}

    leaves: list = []
    try:
        _leaves(m.root_node, leaves)
    except Exception as e:
        return {"ok": False, "error": f"tree walk failed: {e!r}"[:200]}

    # reference anchors
    pose = None
    try:
        p = robot.pose
        pose = {"x": round(float(p.position.x), 1),
                "y": round(float(p.position.y), 1),
                "heading_deg": round(float(p.rotation.angle_z.degrees), 1)}
    except Exception:
        pass
    charger = None
    try:
        ch = robot.world.charger
        if ch is not None and getattr(ch, "pose", None) is not None:
            charger = {"x": round(float(ch.pose.position.x), 1),
                       "y": round(float(ch.pose.position.y), 1)}
    except Exception:
        pass

    root_size = float(getattr(m, "size", 2048.0))
    center = getattr(m, "center", None)
    cen = {"x": round(float(center.x), 1), "y": round(float(center.y), 1)} \
        if center is not None else {"x": 0.0, "y": 0.0}

    counts: dict = {}
    for c in leaves:
        lab = _CONTENT_RGB.get(c["content"], ("other", _DEFAULT_RGB))[0]
        counts[lab] = counts.get(lab, 0) + 1

    blueprint = {
        "ok": True,
        "ts": time.time(),
        "tag": tag,
        "root_size_mm": root_size,
        "center": cen,
        "n_cells": len(leaves),
        "counts": counts,
        "pose": pose,
        "charger": charger,
        "cells": leaves,
    }
    try:
        MAP_JSON.write_text(json.dumps(blueprint), encoding="utf-8")
    except Exception:
        pass
    png = _render(blueprint)
    return {"ok": True, "cells": len(leaves), "counts": counts,
            "pose": pose, "charger": charger, "png": png,
            "json": str(MAP_JSON)}


def _render(bp: dict, px: int = 600) -> str:
    """Top-down PNG: +x forward/up, +y left. Vector = blue triangle, charger =
    green square. mm -> px scaled to the root map size."""
    try:
        import numpy as np
        import cv2
    except Exception:
        return ""
    size_mm = float(bp.get("root_size_mm", 2048.0)) or 2048.0
    cen = bp.get("center", {"x": 0.0, "y": 0.0})
    cx0, cy0 = float(cen["x"]), float(cen["y"])
    scale = px / size_mm

    def to_px(x, y):
        # robot frame: x forward, y left. image: right=+x(forward up), so map
        # x->up (row decreasing), y->left (col decreasing). Keep it simple/legible:
        u = int(px / 2 - (y - cy0) * scale)   # col: +y (left) -> left on screen
        v = int(px / 2 - (x - cx0) * scale)   # row: +x (fwd) -> up on screen
        return max(0, min(px - 1, u)), max(0, min(px - 1, v))

    img = np.full((px, px, 3), 30, np.uint8)
    for c in bp.get("cells", []):
        _, rgb = _CONTENT_RGB.get(c["content"], ("other", _DEFAULT_RGB))
        half = max(1, int(float(c["size"]) * scale / 2))
        u, v = to_px(c["cx"], c["cy"])
        cv2.rectangle(img, (u - half, v - half), (u + half, v + half),
                      (rgb[2], rgb[1], rgb[0]), -1)
    # charger (green square)
    ch = bp.get("charger")
    if ch:
        u, v = to_px(ch["x"], ch["y"])
        cv2.rectangle(img, (u - 6, v - 6), (u + 6, v + 6), (60, 200, 60), -1)
        cv2.putText(img, "HOME", (u + 8, v), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    (60, 200, 60), 1)
    # robot (blue triangle pointing at heading)
    pose = bp.get("pose")
    if pose:
        import math
        u, v = to_px(pose["x"], pose["y"])
        hd = math.radians(pose.get("heading_deg", 0.0))
        # forward in robot frame is +x -> up; rotate by heading
        tip = (int(u - 14 * math.sin(hd)), int(v - 14 * math.cos(hd)))
        cv2.circle(img, (u, v), 7, (255, 150, 40), -1)
        cv2.line(img, (u, v), tip, (255, 150, 40), 2)
    # scale bar (500mm)
    bar = int(500 * scale)
    cv2.line(img, (10, px - 12), (10 + bar, px - 12), (200, 200, 200), 2)
    cv2.putText(img, "500mm", (10, px - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                (200, 200, 200), 1)
    tag = bp.get("tag") or ""
    if tag:
        cv2.putText(img, tag, (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (200, 200, 255), 1)
    try:
        cv2.imwrite(str(MAP_PNG), img)
        return str(MAP_PNG)
    except Exception:
        return ""


def map_snapshot(tag: str = "") -> dict:
    """One-shot: open a control session, capture the current nav-map, close."""
    from brain import vector_action as va
    try:
        with va.control_session() as robot:
            return capture_map(robot, tag=tag)
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}
