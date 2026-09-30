"""Symbiote v3 — from the second contact sheet (2026-09-18 12:3x) + the profile.

Seen: a pale halo ring around the black core and grey-disc droplets — the
"fresnel rim" ((1-nz)^2.5) and the broad oily sheen (ndoth^10) paint the whole
outer slope of the dome, not an edge. The title/logo drawn before _fx was
buried under the post-pass re-occlusion. And cProfile said the frames are
cheap (~0.13 s at 540x960) while analyze() costs ~7.5 min per run — librosa's
numba gufuncs recompile every run and the wav read alone took 100 s — so
iteration is analysis-bound, not render-bound.

Fixes: rim = thin band around the silhouette threshold; sheen tighter/weaker;
the post pass keeps the PRE-fx pixels inside the mass (title survives, flash
does not); analysis results cached per (file, size, mtime, fps) on D:.
"""
from __future__ import annotations

import io
import py_compile
from pathlib import Path

P = Path(__file__).resolve().parent / "lyric_viz.py"
s = io.open(P, encoding="utf-8").read()
assert "symbiote_post" in s, "v2 not applied"
if "_analyze_cached" in s:
    raise SystemExit("v3 already applied")

# 1. thin rim band + tighter sheen
old = "        sheen = ndoth ** 10 * 0.22\n        rim = np.clip(1.0 - nz, 0, 1) ** 2.5 * (0.7 + 0.6 * rms)\n"
assert s.count(old) == 1, "rim/sheen anchor"
new = ("        sheen = ndoth ** 28 * 0.14\n"
       "        # rim = a THIN band around the silhouette threshold, not the whole\n"
       "        # outer slope (v2's (1-nz)^2.5 painted a pale halo ring)\n"
       "        rim = np.clip(1.0 - np.abs(hb - 0.24) / 0.09, 0, 1) ** 1.5 * (0.6 + 0.5 * rms)\n")
s = s.replace(old, new)

# 2. post pass: keep pre-fx pixels inside the mass (title survives, flash does not)
old = ("        img = self._fx(img, i, a)\n"
       "        if self._symb is not None and self._symb.get(\"post\") is not None:\n"
       "            # symbiote_post: the black mass must survive _fx's additive\n"
       "            # flashes; put it back, highlights included, before the bloom\n"
       "            S, shade = self._symb[\"post\"]\n"
       "            img *= (1.0 - S * 0.985)\n"
       "            img += S * shade\n"
       "            self._symb[\"post\"] = None\n")
assert s.count(old) == 1, "post anchor"
new = ("        _pre = None\n"
       "        if self._symb is not None and self._symb.get(\"post\") is not None:\n"
       "            _pre = img.copy()\n"
       "        img = self._fx(img, i, a)\n"
       "        if _pre is not None:\n"
       "            # symbiote_post: inside the mass keep the PRE-fx pixels (the black,\n"
       "            # the highlights AND the title drawn over it); outside keep the\n"
       "            # style's flash / glitch / rgb-split. A kick no longer paints the\n"
       "            # mass red, and the title is not buried.\n"
       "            S, _shade = self._symb[\"post\"]\n"
       "            img = _pre * S + img * (1.0 - S)\n"
       "            self._symb[\"post\"] = None\n")
s = s.replace(old, new)

# 3. analysis cache
old = "    analysis, pcm, sr = analyze(args.audio, args.fps)\n"
assert s.count(old) == 1, "analyze call anchor"
s = s.replace(old, "    analysis, pcm, sr = _analyze_cached(args.audio, args.fps)\n")
anchor = "\ndef main("
assert s.count(anchor) == 1, "main anchor"
helper = '''
def _analyze_cached(audio: Path, fps: int) -> tuple[Analysis, np.ndarray, int]:
    """analyze() with a per-track cache on D: — profiled 2026-09-18: a 2 s test
    render spent 456 of 519 s inside analyze() (librosa's numba gufuncs
    recompile every run; the wav read alone took 100 s) and ~0.13 s/frame on
    the actual frames. Iterating on a look was analysis-bound. Key = file
    name + size + mtime + fps, so a re-exported master invalidates itself."""
    try:
        st = audio.stat()
        cdir = REPO / "state" / "tzeke_songs" / ".analysis_cache"
        cdir.mkdir(parents=True, exist_ok=True)
        key = f"{audio.stem}_{st.st_size}_{int(st.st_mtime)}_{fps}"
        cpath = cdir / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".npz")
        if cpath.is_file():
            d = np.load(str(cpath), allow_pickle=False)
            fields = [f for f in Analysis.__dataclass_fields__]
            kw = {f: (float(d[f]) if f == "bpm" else d[f]) for f in fields}
            print(f"[lyric_viz] analysis: cache hit ({cpath.name})")
            return Analysis(**kw), d["_pcm"], int(d["_sr"])
    except Exception as e:  # cache is an optimisation, never a failure
        print(f"[lyric_viz] analysis cache read skipped: {e!r}")
        cpath = None
    analysis, pcm, sr = analyze(audio, fps)
    try:
        if cpath is not None:
            payload = {f: getattr(analysis, f) for f in Analysis.__dataclass_fields__}
            payload["bpm"] = np.array(analysis.bpm, np.float64)
            payload["_pcm"] = pcm
            payload["_sr"] = np.array(sr, np.int64)
            np.savez(str(cpath), **payload)
            print(f"[lyric_viz] analysis: cached -> {cpath.name}")
    except Exception as e:
        print(f"[lyric_viz] analysis cache write skipped: {e!r}")
    return analysis, pcm, sr

'''
s = s.replace(anchor, helper + anchor)

io.open(P, "w", encoding="utf-8", newline="\n").write(s)
py_compile.compile(str(P), doraise=True)
print("symbiote v3 applied; lyric_viz.py compiles")
