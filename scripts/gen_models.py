"""Generate ORIGINAL low-poly .glb models procedurally — no source mesh touched.

    python scripts/gen_models.py            # write all into assets/models3d
    python scripts/gen_models.py --list     # names + edge counts, write nothing

Why these exist (Zeke 2026-08-28): most of the downloaded library is CC-BY 3.0,
which legally requires naming the author wherever a render is published. These
are generated from mathematics in this file, so they are ours — **no
attribution, ever**.

⚠ The legal point that shapes the whole approach: *deriving* a mesh from a
CC-BY model produces a DERIVATIVE WORK and still requires crediting the
original author. Editing does not launder a licence. So nothing here reads,
imports, or measures any downloaded model — same subjects, independent
geometry.

Being procedural is also a genuine advantage rather than a fallback: these
render as WIREFRAMES, where readability is governed by edge count (verified by
eye — low-poly reads as the object, dense meshes collapse into a ball of
lines). Here the edge count is a parameter, not a lottery. Everything is built
to sit in the 40-4000 budget.
"""
import argparse
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "assets" / "models3d"
MANIFEST = DEST / "manifest.json"
TAU = 2.0 * np.pi


# --- primitives -----------------------------------------------------------

def _revolve(profile, seg):
    """Surface of revolution: profile [(r,z)] spun about Z into a mesh."""
    n = len(profile)
    v, f = [], []
    for s in range(seg):
        a = TAU * s / seg
        ca, sa = np.cos(a), np.sin(a)
        for (r, z) in profile:
            v.append((r * ca, r * sa, z))
    for s in range(seg):
        s2 = (s + 1) % seg
        for k in range(n - 1):
            a, b = s * n + k, s * n + k + 1
            c, d = s2 * n + k, s2 * n + k + 1
            f.append((a, b, d))
            f.append((a, d, c))
    return np.array(v, np.float32), np.array(f, np.int64)


def _icosahedron():
    p = (1 + 5 ** 0.5) / 2
    v = np.array([(-1, p, 0), (1, p, 0), (-1, -p, 0), (1, -p, 0),
                  (0, -1, p), (0, 1, p), (0, -1, -p), (0, 1, -p),
                  (p, 0, -1), (p, 0, 1), (-p, 0, -1), (-p, 0, 1)], np.float32)
    f = np.array([(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
                  (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
                  (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
                  (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)],
                 np.int64)
    return v / np.linalg.norm(v[0]), f


def _subdivide(v, f, rounds, project=True):
    v = list(map(tuple, v))
    for _ in range(rounds):
        idx, nf = {}, []
        for a, b, c in f:
            m = []
            for x, y in ((a, b), (b, c), (c, a)):
                k = (min(x, y), max(x, y))
                if k not in idx:
                    p = (np.array(v[x]) + np.array(v[y])) / 2.0
                    if project:
                        p = p / np.linalg.norm(p)
                    idx[k] = len(v)
                    v.append(tuple(p))
                m.append(idx[k])
            ab, bc, ca = m
            nf += [(a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)]
        f = np.array(nf, np.int64)
    return np.array(v, np.float32), f


# --- the shapes -----------------------------------------------------------

def geodesic(rounds=1):
    v, f = _icosahedron()
    return _subdivide(v, f, rounds)


def gem(facets=10):
    """Brilliant-cut style: crown + pavilion. Reads instantly in wireframe."""
    prof = [(0.0, 1.0), (0.62, 0.42), (1.0, 0.10), (0.72, -0.30), (0.0, -1.15)]
    return _revolve(prof, facets)


def crystal_shard(sides=6):
    prof = [(0.0, 1.25), (0.34, 0.55), (0.40, -0.55), (0.0, -1.2)]
    return _revolve(prof, sides)


def spike_ball(spikes_lat=8, seg=16, spike=0.55):
    """Sea-urchin: alternating radii on a revolve — pure spectrum energy."""
    prof = []
    for k in range(spikes_lat * 2 + 1):
        t = np.pi * k / (spikes_lat * 2)
        r = 1.0 + (spike if k % 2 else 0.0)
        prof.append((r * np.sin(t), r * np.cos(t)))
    return _revolve(prof, seg)


def torus(R=1.0, r=0.38, seg=24, ring=12):
    v, f = [], []
    for i in range(seg):
        a = TAU * i / seg
        for j in range(ring):
            b = TAU * j / ring
            v.append(((R + r * np.cos(b)) * np.cos(a),
                      (R + r * np.cos(b)) * np.sin(a), r * np.sin(b)))
    for i in range(seg):
        i2 = (i + 1) % seg
        for j in range(ring):
            j2 = (j + 1) % ring
            a, b = i * ring + j, i * ring + j2
            c, d = i2 * ring + j, i2 * ring + j2
            f += [(a, b, d), (a, d, c)]
    return np.array(v, np.float32), np.array(f, np.int64)


def torus_knot(p=2, q=3, seg=120, ring=8, r=0.22):
    """(p,q) knot swept with a tube — the most 'visualizer' object there is."""
    t = np.linspace(0, TAU, seg, endpoint=False)
    cx = np.cos(p * t) * (2 + np.cos(q * t))
    cy = np.sin(p * t) * (2 + np.cos(q * t))
    cz = np.sin(q * t)
    C = np.stack([cx, cy, cz], 1) / 3.0
    T = np.gradient(C, axis=0)
    T /= np.linalg.norm(T, axis=1, keepdims=True) + 1e-9
    up = np.array([0.0, 0.0, 1.0])
    N = np.cross(T, up)
    N /= np.linalg.norm(N, axis=1, keepdims=True) + 1e-9
    B = np.cross(T, N)
    v, f = [], []
    for i in range(seg):
        for j in range(ring):
            a = TAU * j / ring
            v.append(C[i] + r * (np.cos(a) * N[i] + np.sin(a) * B[i]))
    for i in range(seg):
        i2 = (i + 1) % seg
        for j in range(ring):
            j2 = (j + 1) % ring
            a, b = i * ring + j, i * ring + j2
            c, d = i2 * ring + j, i2 * ring + j2
            f += [(a, b, d), (a, d, c)]
    return np.array(v, np.float32), np.array(f, np.int64)


def helix(turns=3.0, seg=140, ring=7, r=0.16, R=0.75):
    t = np.linspace(0, TAU * turns, seg)
    C = np.stack([R * np.cos(t), R * np.sin(t),
                  np.linspace(-1, 1, seg)], 1)
    T = np.gradient(C, axis=0)
    T /= np.linalg.norm(T, axis=1, keepdims=True) + 1e-9
    N = np.cross(T, np.array([0.0, 0.0, 1.0]))
    N /= np.linalg.norm(N, axis=1, keepdims=True) + 1e-9
    B = np.cross(T, N)
    v, f = [], []
    for i in range(seg):
        for j in range(ring):
            a = TAU * j / ring
            v.append(C[i] + r * (np.cos(a) * N[i] + np.sin(a) * B[i]))
    for i in range(seg - 1):
        for j in range(ring):
            j2 = (j + 1) % ring
            a, b = i * ring + j, i * ring + j2
            c, d = (i + 1) * ring + j, (i + 1) * ring + j2
            f += [(a, b, d), (a, d, c)]
    return np.array(v, np.float32), np.array(f, np.int64)


def supershape(m=7, n1=0.2, n2=1.7, n3=1.7, seg=48, ring=24):
    """Gielis superformula — one equation, endless distinct solids."""
    def sf(a):
        t1 = np.abs(np.cos(m * a / 4.0)) ** n2
        t2 = np.abs(np.sin(m * a / 4.0)) ** n3
        return (t1 + t2) ** (-1.0 / n1)
    th = np.linspace(-np.pi / 2, np.pi / 2, ring)
    ph = np.linspace(-np.pi, np.pi, seg, endpoint=False)
    r2 = sf(th)
    v, f = [], []
    for i, p in enumerate(ph):
        r1 = sf(p)
        for j, t in enumerate(th):
            v.append((r1 * np.cos(p) * r2[j] * np.cos(t),
                      r1 * np.sin(p) * r2[j] * np.cos(t),
                      r2[j] * np.sin(t)))
    for i in range(seg):
        i2 = (i + 1) % seg
        for j in range(ring - 1):
            a, b = i * ring + j, i * ring + j + 1
            c, d = i2 * ring + j, i2 * ring + j + 1
            f += [(a, b, d), (a, d, c)]
    return np.array(v, np.float32), np.array(f, np.int64)


def star3d(points=5, depth=0.42):
    """A real 3-D star: bipyramid over an alternating-radius polygon."""
    v = [(0, 0, depth), (0, 0, -depth)]
    n = points * 2
    for k in range(n):
        a = TAU * k / n
        r = 1.0 if k % 2 == 0 else 0.42
        v.append((r * np.cos(a), r * np.sin(a), 0.0))
    f = []
    for k in range(n):
        a, b = 2 + k, 2 + (k + 1) % n
        f += [(0, a, b), (1, b, a)]
    return np.array(v, np.float32), np.array(f, np.int64)


def heart(seg=40, depth=0.42):
    """EXTRUDED 2-D heart curve — a cookie-cutter solid.

    v1 revolved the curve about an axis, which is geometrically wrong: a heart
    is not a surface of revolution, and it rendered as a lumpy shell that read
    as a blob (confirmed by eye). Extruding the outline keeps the silhouette,
    which is the entire thing that makes a heart legible in wireframe."""
    t = np.linspace(0, TAU, seg, endpoint=False)
    x = 16 * np.sin(t) ** 3
    y = (13 * np.cos(t) - 5 * np.cos(2 * t)
         - 2 * np.cos(3 * t) - np.cos(4 * t))
    x, y = x / 17.0, y / 17.0
    v = [(xi, -depth, yi) for xi, yi in zip(x, y)]
    v += [(xi, depth, yi) for xi, yi in zip(x, y)]
    f = []
    for k in range(seg):
        k2 = (k + 1) % seg
        a, b, c, d = k, k2, seg + k, seg + k2
        f += [(a, b, d), (a, d, c)]
    # flat caps so the outline is closed on both faces
    for k in range(1, seg - 1):
        f.append((0, k, k + 1))
        f.append((seg, seg + k + 1, seg + k))
    return np.array(v, np.float32), np.array(f, np.int64)


def prism(sides=8, twist=0.55):
    v, f = [], []
    for lvl, z in enumerate((-1.0, 1.0)):
        for k in range(sides):
            a = TAU * k / sides + (twist if lvl else 0.0)
            v.append((np.cos(a), np.sin(a), z))
    for k in range(sides):
        k2 = (k + 1) % sides
        a, b = k, k2
        c, d = sides + k, sides + k2
        f += [(a, b, d), (a, d, c)]
    return np.array(v, np.float32), np.array(f, np.int64)


def diamond_lattice(n=3):
    """Cubic lattice of struts — a 'grid cube' that reads great in wireframe."""
    v, f = [], []
    for i in range(n + 1):
        for j in range(n + 1):
            for k in range(n + 1):
                if (i in (0, n)) + (j in (0, n)) + (k in (0, n)) >= 2:
                    v.append((i / n * 2 - 1, j / n * 2 - 1, k / n * 2 - 1))
    v = np.array(v, np.float32)
    for a in range(len(v)):
        for b in range(a + 1, len(v)):
            if abs(np.linalg.norm(v[a] - v[b]) - 2.0 / n) < 1e-4:
                f.append((a, b, a))          # degenerate tri == a pure edge
    return v, np.array(f, np.int64)


# ★ THE READABLE CEILING IS ~900 EDGES, not the 4000 the download filter allows.
# Verified by eye 2026-08-28: rendered as wireframes at phone scale, everything
# above ~900 filled in solid — the torus knots, helix, supershapes, spike balls
# and heart all came out as luminous blobs at 1600-3400 edges, while the
# 30-900 shapes read instantly. The download budget is permissive because a
# recognisable SILHOUETTE (a skull) survives density; abstract geometry does
# not. Being procedural means this is a parameter, so every shape below is
# tuned to land in that range instead of hoping.
SHAPES = {
    "iris_geodesic": lambda: geodesic(1),
    "iris_geodesic_hi": lambda: geodesic(2),
    "iris_gem": lambda: gem(10),
    "iris_gem_wide": lambda: gem(16),
    "iris_crystal": lambda: crystal_shard(6),
    "iris_crystal_8": lambda: crystal_shard(8),
    "iris_spikeball": lambda: spike_ball(5, 10, 0.55),
    "iris_spikeball_fine": lambda: spike_ball(7, 14, 0.40),
    "iris_torus": lambda: torus(seg=18, ring=9),
    "iris_torusknot_23": lambda: torus_knot(2, 3, seg=44, ring=5),
    "iris_torusknot_35": lambda: torus_knot(3, 5, seg=56, ring=5),
    "iris_helix": lambda: helix(seg=56, ring=5),
    "iris_supershape_7": lambda: supershape(7, 0.2, 1.7, 1.7, seg=22, ring=11),
    # n1=0.3 with n2=n3=0.5 collapses to a flat sliver with no volume (seen by
    # eye). These params give a rounded, ribbed solid instead.
    "iris_supershape_12": lambda: supershape(6, 1.0, 1.0, 1.0, seg=26, ring=13),
    "iris_supershape_star": lambda: supershape(5, 0.1, 1.7, 1.7, seg=20, ring=10),
    "iris_star": lambda: star3d(5),
    "iris_star_8": lambda: star3d(8, 0.3),
    "iris_heart": lambda: heart(seg=40),
    "iris_prism": lambda: prism(8, 0.55),
    "iris_prism_12": lambda: prism(12, 0.30),
    "iris_lattice": lambda: diamond_lattice(3),
}


def edge_count(v, f) -> int:
    e = np.unique(np.sort(np.concatenate(
        [f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1), axis=0)
    return len([1 for a, b in e if a != b])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    import trimesh
    DEST.mkdir(parents=True, exist_ok=True)
    man = {}
    if MANIFEST.exists():
        man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    made = 0
    for name, fn in SHAPES.items():
        v, f = fn()
        v = v - v.mean(axis=0)
        v = v / max(1e-6, np.abs(v).max())
        ec = edge_count(v, f)
        flag = "" if 40 <= ec <= 4000 else "   <-- OUTSIDE BUDGET"
        print(f"  {name:24s} {len(v):5d} verts {ec:5d} edges{flag}")
        if args.list:
            continue
        trimesh.Trimesh(vertices=v, faces=f, process=False).export(
            str(DEST / f"{name}.glb"))
        man[name] = {"id": None, "author": "Iris (procedural)",
                     "license": "ORIGINAL - no attribution required"}
        made += 1
    if not args.list:
        MANIFEST.write_text(json.dumps(man, indent=2, sort_keys=True),
                            encoding="utf-8")
        print(f"\nwrote {made} original models + manifest entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
