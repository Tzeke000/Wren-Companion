# SELF_ASSESSMENT: I am Iris's route PLANNER — A* over the room blueprint +
# hazard memory, so missions route AROUND known obstacles instead of only
# detouring on contact.
"""vector_planner.py — map-aware path planning (2026-07-17 round 2).

Zeke's directive ("all the research on maneuvering obstacles... build it and
wire it up") — the planning half. The nav-map daemon already builds a passive
room blueprint (state/vector/room_map.json, quad-tree leaves); the pilot
already journals hazards (state/vector/hazards.jsonl). This module turns both
into ROUTES:

    plan(start, goal)  ->  {ok, points: [[x,y],...], length_mm}

Pipeline: rasterize quad-tree leaves to a coarse grid (RES mm) → inflate
obstacles by the robot's radius → overlay recent same-frame hazards as
obstacles → A* (8-connected, unknown cells traversable but penalized) →
line-of-sight simplify to few waypoints (they become body_route legs, which
still carry the full L1/L2 safety net: prox-brake, stuck detect, detours).

Frame honesty: the blueprint + hazards live in the robot's CURRENT pose frame.
A frame reset (pickup/sleep/reboot) makes them alien — callers pass the current
origin_id for hazard filtering, and a stale map (ts too old) refuses rather
than confidently routing through a remembered room that moved.
"""
from __future__ import annotations

import contextlib
import heapq
import json
import math
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAP_JSON = REPO / "state" / "vector" / "room_map.json"

RES_MM = 20.0            # grid resolution
ROBOT_RADIUS_MM = 60.0   # half-width + margin — obstacle inflation
HAZARD_RADIUS_MM = 80.0  # hazard points inflate a touch wider (they were REAL)
START_CARVE_MM = 170.0   # start bubble: must pierce the charger's own obstacle
                         # ring (+inflation) when planning from the dock — the
                         # first real move is an undock that clears it anyway
UNKNOWN_COST = 2.5       # unknown cells: traversable, but prefer known-clear
MAP_MAX_AGE_S = 1800.0   # blueprint older than this = refuse (frame honesty)
OBSTACLE_LABELS = {"ObstacleProximity", "ObstacleProximityExplored",
                   "ObstacleUnrecognized", "ObstacleCube", "Cliff"}
EDGE_LABELS = {"InterestingEdge", "NonInterestingEdge"}


def _load_map() -> dict:
    d = json.loads(MAP_JSON.read_text(encoding="utf-8"))
    if not d.get("ok"):
        raise RuntimeError("blueprint not ok")
    age = time.time() - float(d.get("ts", 0))
    if age > MAP_MAX_AGE_S:
        raise RuntimeError(f"blueprint stale ({age/60:.0f}min) — scan/drive to refresh")
    return d


MARGIN_MM = 240.0        # planning room beyond the map/start/goal envelope
MAX_CELLS = 90000        # grid cap (~300x300) — refuse absurd envelopes


class _Grid:
    """Grid over the BOUNDING BOX of (blueprint ∪ start ∪ goal) + margin —
    the nav-map root GROWS with exploration (it can be a 26cm postage stamp
    early on), so the plannable world must not be clipped to it. Anything the
    map doesn't cover is unknown-cost: traversable, penalized, and the route
    legs still carry the full detour/reflex net when reality disagrees."""

    def __init__(self, bp: dict, hazards: list, extra_pts: list):
        size = float(bp.get("root_size_mm", 2048.0))
        cen = bp.get("center") or {"x": 0.0, "y": 0.0}
        xs = [float(cen["x"]) - size / 2.0, float(cen["x"]) + size / 2.0]
        ys = [float(cen["y"]) - size / 2.0, float(cen["y"]) + size / 2.0]
        for (px, py) in extra_pts:
            xs.append(float(px))
            ys.append(float(py))
        self.x0 = min(xs) - MARGIN_MM
        self.y0 = min(ys) - MARGIN_MM
        self.nx = max(8, int((max(xs) + MARGIN_MM - self.x0) / RES_MM) + 1)
        self.ny = max(8, int((max(ys) + MARGIN_MM - self.y0) / RES_MM) + 1)
        if self.nx * self.ny > MAX_CELLS:
            raise RuntimeError(
                f"planning envelope too large ({self.nx}x{self.ny} cells)")
        # cost layers: 1.0=known-clear / UNKNOWN_COST / inf(obstacle)
        self.cost = [[UNKNOWN_COST] * self.ny for _ in range(self.nx)]
        inf = float("inf")
        infl = int(math.ceil(ROBOT_RADIUS_MM / RES_MM))
        obstacles = []
        for c in bp.get("cells", []):
            label = c.get("content")
            half = float(c.get("size", RES_MM)) / 2.0
            i0, j0 = self._cell(c["cx"] - half, c["cy"] - half)
            i1, j1 = self._cell(c["cx"] + half, c["cy"] + half)
            if label in OBSTACLE_LABELS or label in EDGE_LABELS:
                obstacles.append((i0, j0, i1, j1))
            elif label in ("ClearOfObstacle", "ClearOfCliff"):
                for i in range(max(0, i0), min(self.nx, i1 + 1)):
                    row = self.cost[i]
                    for j in range(max(0, j0), min(self.ny, j1 + 1)):
                        if row[j] != inf:
                            row[j] = 1.0
        for (i0, j0, i1, j1) in obstacles:      # inflate after clear pass
            for i in range(max(0, i0 - infl), min(self.nx, i1 + infl + 1)):
                row = self.cost[i]
                for j in range(max(0, j0 - infl), min(self.ny, j1 + infl + 1)):
                    row[j] = inf
        hinfl = int(math.ceil(HAZARD_RADIUS_MM / RES_MM))
        for h in hazards:
            with contextlib.suppress(Exception):
                ci, cj = self._cell(float(h["x"]), float(h["y"]))
                for i in range(max(0, ci - hinfl), min(self.nx, ci + hinfl + 1)):
                    row = self.cost[i]
                    for j in range(max(0, cj - hinfl), min(self.ny, cj + hinfl + 1)):
                        row[j] = inf

    def _cell(self, x: float, y: float):
        return (int((x - self.x0) / RES_MM), int((y - self.y0) / RES_MM))

    def _xy(self, i: int, j: int):
        return (self.x0 + (i + 0.5) * RES_MM, self.y0 + (j + 0.5) * RES_MM)

    def passable(self, i: int, j: int) -> bool:
        return (0 <= i < self.nx and 0 <= j < self.ny
                and self.cost[i][j] != float("inf"))

    def carve(self, i: int, j: int, r_cells: int) -> None:
        """Force-passable a bubble (used at the START: the robot physically
        occupies that space — e.g. docked INSIDE the charger's own obstacle
        footprint, which the engine marks as an ObstacleCube ring)."""
        inf = float("inf")
        for a in range(max(0, i - r_cells), min(self.nx, i + r_cells + 1)):
            row = self.cost[a]
            for b in range(max(0, j - r_cells), min(self.ny, j + r_cells + 1)):
                if row[b] == inf:
                    row[b] = UNKNOWN_COST

    def nearest_passable(self, i: int, j: int, max_r: int = 12):
        """Start/goal may sit inside an inflated zone (I'm parked against the
        wall I mapped) — snap to the nearest passable cell within max_r."""
        if self.passable(i, j):
            return (i, j)
        for r in range(1, max_r + 1):
            for di in range(-r, r + 1):
                for dj in (-r, r):
                    if self.passable(i + di, j + dj):
                        return (i + di, j + dj)
                    if self.passable(i + dj, j + di):
                        return (i + dj, j + di)
        return None

    def line_free(self, a, b) -> bool:
        """Every RES/2 step along a->b passable (for path simplify)."""
        (x0, y0), (x1, y1) = self._xy(*a), self._xy(*b)
        dist = math.hypot(x1 - x0, y1 - y0)
        steps = max(2, int(dist / (RES_MM / 2.0)))
        for k in range(steps + 1):
            t = k / steps
            i, j = self._cell(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            if not self.passable(i, j):
                return False
        return True


_SQRT2 = math.sqrt(2.0)


def _astar(g: _Grid, start, goal):
    def h(a):
        return math.hypot(a[0] - goal[0], a[1] - goal[1])
    open_q = [(h(start), 0.0, start)]
    came: dict = {start: None}
    best: dict = {start: 0.0}
    nbrs = [(-1, -1, _SQRT2), (-1, 0, 1.0), (-1, 1, _SQRT2), (0, -1, 1.0),
            (0, 1, 1.0), (1, -1, _SQRT2), (1, 0, 1.0), (1, 1, _SQRT2)]
    while open_q:
        _, gc, cur = heapq.heappop(open_q)
        if cur == goal:
            path = []
            while cur is not None:
                path.append(cur)
                cur = came[cur]
            return path[::-1]
        if gc > best.get(cur, float("inf")):
            continue
        for di, dj, w in nbrs:
            nxt = (cur[0] + di, cur[1] + dj)
            if not g.passable(*nxt):
                continue
            ng = gc + w * max(g.cost[nxt[0]][nxt[1]], g.cost[cur[0]][cur[1]])
            if ng < best.get(nxt, float("inf")):
                best[nxt] = ng
                came[nxt] = cur
                heapq.heappush(open_q, (ng + h(nxt), ng, nxt))
    return None


def _simplify(g: _Grid, path: list) -> list:
    """Greedy line-of-sight shortcutting: keep the fewest waypoints whose
    connecting segments stay in passable space."""
    if len(path) <= 2:
        return path
    out = [path[0]]
    anchor = 0
    for k in range(2, len(path)):
        if not g.line_free(path[anchor], path[k]):
            out.append(path[k - 1])
            anchor = k - 1
    out.append(path[-1])
    return out


def frontiers(start_xy, hazards: list = None, min_sep_mm: float = 200.0) -> dict:
    """FRONTIER targets for exploration (2026-07-17 — research found nobody
    has built this for Vector; the blueprint makes it nearly free): known-CLEAR
    cells adjacent to UNKNOWN space are where driving gains information.
    Returns cluster representatives sorted nearest-first (they're already
    passable by construction — no snapping needed)."""
    try:
        bp = _load_map()
    except Exception as e:
        return {"ok": False, "error": f"no usable blueprint: {e}"[:200]}
    try:
        g = _Grid(bp, hazards or [], [start_xy])
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    sx, sy = float(start_xy[0]), float(start_xy[1])
    cands = []
    for i in range(g.nx):
        row = g.cost[i]
        for j in range(g.ny):
            if row[j] != 1.0:            # frontier seeds are KNOWN-clear...
                continue
            edge = False                 # ...touching unknown
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                a, b = i + di, j + dj
                if (0 <= a < g.nx and 0 <= b < g.ny
                        and g.cost[a][b] == UNKNOWN_COST):
                    edge = True
                    break
            if edge:
                x, y = g._xy(i, j)
                cands.append((math.hypot(x - sx, y - sy), x, y))
    cands.sort()
    picked = []
    for d, x, y in cands:
        if d < 120.0:                    # already standing at this frontier
            continue
        if all(math.hypot(x - px, y - py) >= min_sep_mm for (px, py) in picked):
            picked.append((x, y))
        if len(picked) >= 8:
            break
    return {"ok": True,
            "targets": [[round(x, 1), round(y, 1)] for (x, y) in picked],
            "n_candidates": len(cands),
            "map_age_s": round(time.time() - float(bp.get("ts", 0)), 1)}


def plan(start_xy, goal_xy, hazards: list = None) -> dict:
    """Route start->goal through the blueprint. Returns waypoints in mm
    (excluding the start point), ready for body_route legs."""
    try:
        bp = _load_map()
    except Exception as e:
        return {"ok": False, "error": f"no usable blueprint: {e}"[:200],
                "fallback": "direct servo with detours"}
    try:
        g = _Grid(bp, hazards or [], [start_xy, goal_xy])
    except Exception as e:
        return {"ok": False, "error": str(e)[:200],
                "fallback": "direct servo with detours"}
    si, sj = g._cell(float(start_xy[0]), float(start_xy[1]))
    g.carve(si, sj, int(math.ceil(START_CARVE_MM / RES_MM)))
    s = g.nearest_passable(si, sj)
    t = g.nearest_passable(*g._cell(float(goal_xy[0]), float(goal_xy[1])))
    if s is None or t is None:
        return {"ok": False, "error": "start or goal unreachable even after "
                                      "snapping (inside inflated obstacle)",
                "fallback": "direct servo with detours"}
    t0 = time.time()
    path = _astar(g, s, t)
    if path is None:
        return {"ok": False, "error": "no path through the known map",
                "fallback": "direct servo with detours"}
    pts = [g._xy(i, j) for (i, j) in _simplify(g, path)]
    length = sum(math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1])
                 for k in range(len(pts) - 1))
    out_pts = [[round(x, 1), round(y, 1)] for (x, y) in pts[1:]]
    if not out_pts:
        out_pts = [[round(float(goal_xy[0]), 1), round(float(goal_xy[1]), 1)]]
    return {"ok": True, "points": out_pts, "length_mm": round(length, 0),
            "grid": [g.nx, g.ny], "raw_cells": len(path),
            "plan_ms": round((time.time() - t0) * 1000, 1),
            "map_age_s": round(time.time() - float(bp.get("ts", 0)), 1),
            "hazards_used": len(hazards or [])}
