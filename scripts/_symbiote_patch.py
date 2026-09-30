"""One-shot patcher: add the `symbiote` viz mode to scripts/lyric_viz.py.

Written 2026-09-18 by Iris for Zeke's ask: "black liquid, almost like the Venom
symbiote, reacting to music" for the deathstep single "yeah!". Kept as a file so
the patch is reviewable and re-runnable (it refuses to apply twice).
"""
from __future__ import annotations

import io
import py_compile
from pathlib import Path

P = Path(__file__).resolve().parent / "lyric_viz.py"
s = io.open(P, encoding="utf-8").read()
if "_viz_symbiote" in s:
    raise SystemExit("already applied")

# 1. register the mode
old = ('VIZ_MODES = ("radial", "bars_center", "bars", "wave", "tunnel", "supernova",\n'
       '             "kaleido", "ncs_ring", "tn_blob", "mcat_bars")')
assert s.count(old) == 1, "VIZ_MODES anchor"
s = s.replace(old, 'VIZ_MODES = ("radial", "bars_center", "bars", "wave", "tunnel", "supernova",\n'
                   '             "kaleido", "ncs_ring", "tn_blob", "mcat_bars", "symbiote")')

# 2. dispatch
old = '        elif v == "mcat_bars":\n            self._viz_mcat_bars(img, i, a)\n'
assert s.count(old) == 1, "dispatch anchor"
s = s.replace(old, old + '        elif v == "symbiote":\n            self._viz_symbiote(img, i, a)\n')

# 3. renderer state field
old = '    _shape_last: int = -1                   # last slot index, for swap logging\n'
assert s.count(old) == 1, "field anchor"
s = s.replace(old, old + '    _symb: "dict | None" = None             # symbiote viz state (droplets, tendrils, plate)\n')

# 4. the viz itself, inserted before _viz_mcat_bars
anchor = '    def _viz_mcat_bars(self, img: np.ndarray, i: int, a: Analysis) -> None:\n'
assert s.count(anchor) == 1, "method anchor"
METHOD = '''    def _viz_symbiote(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """SYMBIOTE -- Zeke 2026-09-18: "black liquid, almost like the Venom
        symbiote, reacting to music" for the deathstep single "yeah!".

        A black thing cannot be drawn ADDITIVELY on a black frame, so this viz
        breaks the renderer's usual rule in two places: it lays a dim plate
        under itself (a black mass needs something to be darker THAN), and the
        mass OCCLUDES (img *= 1-S) before its highlights are added. Everything
        that reads as "wet black" is the highlights, not the black:

          body      = smoothed polar silhouette: spectrum lobes (as tn_blob)
                      + a slow organic wobble + 7 drifting TENDRILS whose
                      length rides the bass (and the drop);
          droplets  = spawned on kicks, thrown outward, pulled back by a
                      spring, merged into the body through a BLURRED HEIGHT
                      FIELD -- the blur radius is the viscosity, so blobs
                      neck and re-join like goo instead of popping;
          shading   = normals from the height gradient -> Blinn specular
                      (tight white-blue streak = wet), a broad oily sheen in
                      the style's 2nd colour, and a fresnel RIM in the style's
                      accent so the edge is always drawn.

        Field is computed at 1/3 res and cubic-upsampled; the plate is cached."""
        q = 3
        w, h = self.W // q, self.H // q
        st = self._symb
        if st is None:
            rng = np.random.default_rng(3)
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
            Y, X = np.ogrid[0:self.H, 0:self.W]
            d = np.sqrt(((X - self.W / 2) / (self.W / 2)) ** 2
                        + ((Y - self.H * 0.45) / (self.H / 2)) ** 2)
            g = np.clip(1.0 - d / 1.25, 0, 1)[..., None].astype(np.float32) ** 1.4
            c_in = np.array((52.0, 46.0, 60.0), np.float32)
            c_out = np.array((9.0, 8.0, 12.0), np.float32)
            st = self._symb = {
                "rng": rng, "xx": xx, "yy": yy, "drops": [],
                "ang": rng.uniform(0, 2 * np.pi, 7),
                "spin": rng.uniform(-0.7, 0.7, 7),
                "last_kick": -99, "kick_env": 0.0,
                "plate": c_out + (c_in - c_out) * g,
            }
        xx, yy = st["xx"], st["yy"]
        rng = st["rng"]
        rms, bass, high = float(a.rms[i]), float(a.bass[i]), float(a.high[i])
        drop, kick = bool(a.drop[i]), bool(a.kick[i])
        t = i / self.fps
        st["kick_env"] = max(st["kick_env"] * 0.80, 1.0 if kick else 0.0)
        ke = st["kick_env"]
        cx, cy = w / 2.0, h * 0.45
        R0 = h * 0.155 * (1.0 + 0.30 * bass + 0.12 * rms + 0.10 * ke)

        # -- silhouette: spectrum lobes + wobble + tendrils ------------------
        n = 180
        half = n // 2
        idx = (np.arange(half) * NBARS) // half
        mag = a.bars[i, idx].astype(np.float32)
        k = np.hanning(15).astype(np.float32); k /= k.sum()
        mag = np.convolve(np.concatenate([mag[-14:], mag, mag[:14]]), k,
                          "same")[14:-14]
        mag = np.concatenate([mag, mag[::-1]])
        th = np.linspace(-np.pi / 2, 1.5 * np.pi, n, endpoint=False)
        wob = 0.09 * np.sin(3 * th + t * 1.3) + 0.05 * np.sin(5 * th - t * 0.9)
        rr = R0 * (1.0 + wob + 0.85 * mag * (0.35 + 0.65 * rms))
        st["ang"] += st["spin"] * (1.0 / self.fps) * (0.25 + 1.6 * bass)
        tend = np.zeros(n, np.float32)
        for ang in st["ang"]:
            dth = np.angle(np.exp(1j * (th - ang)))
            tend += np.exp(-(dth / 0.12) ** 2)
        L = R0 * (0.95 * bass + (0.55 if drop else 0.0) + 0.30 * high + 0.35 * ke)
        rr = rr + L * np.clip(tend, 0, 1) ** 1.3
        poly = np.stack([cx + rr * np.cos(th), cy + rr * np.sin(th)], 1)
        mask = np.zeros((h, w), np.uint8)
        cv2.fillPoly(mask, [poly.astype(np.int32).reshape(-1, 1, 2)], 255)
        hfield = cv2.distanceTransform(mask, cv2.DIST_L2, 3) / max(1.0, R0 * 0.55)
        np.clip(hfield, 0, 1, out=hfield)

        # -- droplets: thrown on kicks, sprung back, merged through the blur --
        if kick and i - st["last_kick"] > 3:
            st["last_kick"] = i
            for _ in range(2 + int(4 * bass)):
                ang = rng.uniform(0, 2 * np.pi)
                sp = (0.9 + 1.6 * bass) * R0 * 0.075
                st["drops"].append([cx + np.cos(ang) * R0 * 0.9,
                                    cy + np.sin(ang) * R0 * 0.9,
                                    np.cos(ang) * sp, np.sin(ang) * sp, 0.0,
                                    R0 * rng.uniform(0.16, 0.34)])
        keep = []
        for dr in st["drops"]:
            dr[4] += 1
            dr[0] += dr[2]; dr[1] += dr[3]
            dr[2] += (cx - dr[0]) * 0.0055; dr[3] += (cy - dr[1]) * 0.0055
            dr[2] *= 0.986; dr[3] *= 0.986
            r = dr[5]
            hfield += 1.15 * np.exp(-((xx - dr[0]) ** 2 + (yy - dr[1]) ** 2)
                                    / (2.0 * (r * 0.7) ** 2))
            if dr[4] < 110 and np.hypot(dr[0] - cx, dr[1] - cy) > R0 * 0.55:
                keep.append(dr)
        st["drops"] = keep[-40:]

        # -- viscosity + shading ---------------------------------------------
        hb = cv2.GaussianBlur(hfield, (0, 0), 2.4)
        sil = np.clip((hb - 0.13) * 9.0, 0, 1)
        gx = cv2.Sobel(hb, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(hb, cv2.CV_32F, 0, 1, ksize=3)
        s_ = 9.0
        nz = 1.0 / np.sqrt(1.0 + (gx * s_) ** 2 + (gy * s_) ** 2)
        nx, ny = -gx * s_ * nz, -gy * s_ * nz
        lx, ly, lz = -0.42, -0.62, 0.66
        ln = float(np.sqrt(lx * lx + ly * ly + lz * lz)); lx, ly, lz = lx / ln, ly / ln, lz / ln
        hx, hy, hz = lx, ly, lz + 1.0
        hn = float(np.sqrt(hx * hx + hy * hy + hz * hz)); hx, hy, hz = hx / hn, hy / hn, hz / hn
        ndotl = np.clip(nx * lx + ny * ly + nz * lz, 0, 1)
        ndoth = np.clip(nx * hx + ny * hy + nz * hz, 0, 1)
        spec = ndoth ** 70 * (0.75 + 0.55 * high + 0.4 * ke)
        sheen = ndoth ** 10 * 0.22
        rim = np.clip(1.0 - nz, 0, 1) ** 2.5 * (0.7 + 0.6 * rms)

        def up(z):
            return cv2.resize(z, (self.W, self.H), interpolation=cv2.INTER_CUBIC)
        S = up(sil)[..., None]
        pal0 = self._pal(); pal1 = self._pal(1)
        img *= 0.30                                   # dim whatever bg was drawn
        img += st["plate"] * (0.75 + 0.45 * rms + 0.30 * ke)
        img *= (1.0 - S * 0.985)                      # the mass occludes
        shade = (up(ndotl)[..., None] * np.array((24.0, 20.0, 30.0), np.float32)
                 + up(spec)[..., None] * np.array((225.0, 232.0, 255.0), np.float32)
                 + up(sheen)[..., None] * pal1 * 0.9
                 + up(rim)[..., None] * pal0 * 0.85)
        img += S * shade

'''
s = s.replace(anchor, METHOD + anchor)

# 5. help text
old = '"radial|bars_center|bars|wave|tunnel|supernova|kaleido"'
assert s.count(old) == 1, "help anchor"
s = s.replace(old, '"radial|bars_center|bars|wave|tunnel|supernova|kaleido|symbiote"')

io.open(P, "w", encoding="utf-8", newline="\n").write(s)
py_compile.compile(str(P), doraise=True)
print("symbiote viz added; lyric_viz.py compiles")
