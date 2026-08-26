"""lyric_viz v1 — the lyric/logo audio-visualizer for Zeke's music videos.

THE PROMISE (Zeke, 2026-08-25 bedtime; "do the whole thing" 2026-08-26): song +
written lyrics + logo in → finished music video out. Word-synced lyrics
(Whisper word timestamps aligned to the WRITTEN lyrics — text is ground truth,
audio supplies time), audio-reactive visuals in the EDM visual language,
zero-spend, renders locally.

STYLE RESEARCH (2026-08-26, memory/edm_visualizer_style_research_2026-08-26.md):
Greyland Audio Visual Lab + EDM/dubstep/deep-house conventions. The grammar:
  * lows → big heavy motion; highs → shimmer; logo gets a GLOWING OUTLINE
  * follow the song's energy curve: calm intro → building tension → DROP
    (detonate: strobe, glitch slices, RGB split, shake, zoom punch, text
    scramble) → breakdown (breathe, desaturate)
  * kick is the pulse (low-band onsets), cuts land on the beat
  * drop detector = fast bass average vs slow baseline + running peak,
    engages only on SUSTAINED low end, warmup guard, hysteresis release
  * genre presets: dubstep (half-time, aggressive glitch, acid palette),
    deephouse (dark neon, smooth, no strobes), edm (festival saturated)

⚠ PHOTOSENSITIVITY: edm/dubstep styles strobe on the drop BY DESIGN. Renders
need a flash warning when published. deephouse style does not strobe.

Usage:
  .venv/Scripts/python.exe scripts/lyric_viz.py --audio song.wav
      [--lyrics lyrics.txt] [--logo logo.png] [--title "ARTIST"]
      [--style edm|dubstep|deephouse] [--out out.mp4] [--size 1280x720]
      [--fps 30] [--device cuda|cpu] [--no-vocals]

Encoding is PyAV (bundled FFmpeg libs) — no ffmpeg.exe needed on this machine.
"""
from __future__ import annotations

import argparse
import difflib
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 1. Words with timestamps (unchanged from v0 — proven)
# ---------------------------------------------------------------------------

@dataclass
class Word:
    start: float
    end: float
    text: str


def transcribe_words(audio: Path, device: str = "cuda") -> list[Word]:
    try:
        sys.path.insert(0, str(REPO))
        from brain.gpu_load_log import logged_load
    except Exception:
        from contextlib import nullcontext as logged_load
    from faster_whisper import WhisperModel
    chain = ([("cuda", "float16"), ("cpu", "int8")] if device == "cuda"
             else [("cpu", "int8")])
    model = None
    for dev, compute in chain:
        try:
            with logged_load(f"whisper:distil-large-v3:{dev}:{compute}:lyric_viz"):
                model = WhisperModel("distil-large-v3", device=dev,
                                     compute_type=compute)
            break
        except Exception as e:
            print(f"[lyric_viz] whisper on {dev} failed ({e!r}), trying next")
    if model is None:
        raise RuntimeError("no whisper backend loaded")
    segments, _info = model.transcribe(str(audio), word_timestamps=True,
                                       vad_filter=True)
    words: list[Word] = []
    for seg in segments:
        for w in (seg.words or []):
            t = w.word.strip()
            if t:
                words.append(Word(float(w.start), float(w.end), t))
    print(f"[lyric_viz] transcribed {len(words)} words")
    return words


_NORM_RE = re.compile(r"[^a-z0-9']+")


def _norm(s: str) -> str:
    return _NORM_RE.sub("", s.lower())


def align_to_lyrics(heard: list[Word], lyrics_text: str) -> list[list[Word]]:
    """Written lyrics = ground truth TEXT (and line breaks); whisper = TIME.

    Section tags like [Verse] / [Hook] / [Chorus 2] are structure, not lyrics —
    dropped before alignment (Zeke's lyric PDFs use them)."""
    lines_raw = [ln.strip() for ln in lyrics_text.splitlines()
                 if ln.strip() and not re.fullmatch(r"\[[^\]]+\]", ln.strip())]
    written: list[tuple[int, str]] = []
    for i, ln in enumerate(lines_raw):
        for w in ln.split():
            written.append((i, w))
    if not written or not heard:
        return [[w] for w in heard] if heard else []
    sm = difflib.SequenceMatcher(a=[_norm(w) for _, w in written],
                                 b=[_norm(w.text) for w in heard],
                                 autojunk=False)
    starts: list[float | None] = [None] * len(written)
    ends: list[float | None] = [None] * len(written)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            starts[blk.a + k] = heard[blk.b + k].start
            ends[blk.a + k] = heard[blk.b + k].end
    n = len(written)
    known = [i for i in range(n) if starts[i] is not None]
    if not known:
        return [[w] for w in heard]
    for i in range(n):
        if starts[i] is not None:
            continue
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        if prev is None:
            t0 = max(0.0, (starts[nxt] or 0.0) - 0.3 * (nxt - i))
            starts[i], ends[i] = t0, t0 + 0.25
        elif nxt is None:
            t0 = (ends[prev] or 0.0) + 0.3 * (i - prev - 1)
            starts[i], ends[i] = t0, t0 + 0.25
        else:
            span0, span1 = (ends[prev] or 0.0), (starts[nxt] or 0.0)
            frac = (i - prev) / (nxt - prev)
            t0 = span0 + (span1 - span0) * frac
            starts[i], ends[i] = t0, min(span1, t0 + 0.3)
    out: list[list[Word]] = [[] for _ in lines_raw]
    for (li, wtext), s, e in zip(written, starts, ends):
        out[li].append(Word(float(s), float(e), wtext))
    print(f"[lyric_viz] aligned {len(known)}/{n} written words "
          f"({len(out)} lines)")
    return [ln for ln in out if ln]


def lines_from_transcript(heard: list[Word], max_words: int = 6,
                          gap_s: float = 0.8) -> list[list[Word]]:
    lines: list[list[Word]] = []
    cur: list[Word] = []
    for w in heard:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > gap_s):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------------------
# 2. Audio analysis — the EDM grammar lives here
# ---------------------------------------------------------------------------

@dataclass
class Analysis:
    rms: np.ndarray        # 0..1 smoothed, per video frame
    bass: np.ndarray       # 0..1 smoothed
    high: np.ndarray       # 0..1 smoothed
    kick: np.ndarray       # bool — low-band onset (the pulse)
    snare: np.ndarray      # bool — mid-band onset
    drop: np.ndarray       # bool — sustained-bass drop mode engaged
    build: np.ndarray      # 0..1 — rising-energy tension proxy
    bars: np.ndarray       # (n_frames, NBARS) 0..1 — spectrum for bar display
    bpm: float


NBARS = 40


def _ema(x: np.ndarray, tau_s: float, fps: int) -> np.ndarray:
    a = 1.0 - np.exp(-1.0 / max(1e-6, tau_s * fps))
    out = np.empty_like(x)
    acc = x[0]
    for i, v in enumerate(x):
        acc += a * (v - acc)
        out[i] = acc
    return out


def _norm95(v: np.ndarray) -> np.ndarray:
    pos = v[v > 0]
    hi = np.percentile(pos, 95) if len(pos) else 1.0
    return np.clip(v / max(hi, 1e-9), 0.0, 1.5)


def _onsets(env: np.ndarray, fps: int, refractory_s: float = 0.12,
            k: float = 1.4) -> np.ndarray:
    """Adaptive-threshold flux onsets on a band envelope."""
    flux = np.maximum(0.0, np.diff(env, prepend=env[0]))
    n = len(flux)
    win = max(3, fps)                       # ±1s adaptive window
    on = np.zeros(n, dtype=bool)
    last = -10 ** 9
    for i in range(n):
        a, b = max(0, i - win), min(n, i + win)
        seg = flux[a:b]
        thr = seg.mean() + k * seg.std()
        if flux[i] > thr and flux[i] > 0.02 and (i - last) >= refractory_s * fps:
            on[i] = True
            last = i
    return on


def _estimate_bpm(onset_strength: np.ndarray, fps: int) -> float:
    """Autocorrelation of onset strength, 60–180 BPM; 0.0 if unclear."""
    x = onset_strength - onset_strength.mean()
    if not x.any():
        return 0.0
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]
    lo, hi = int(fps * 60 / 180), int(fps * 60 / 60)
    if hi >= len(ac):
        return 0.0
    lag = lo + int(np.argmax(ac[lo:hi]))
    return round(60.0 * fps / lag, 1) if ac[lag] > 0 else 0.0


def _detect_drop(bass_raw: np.ndarray, fps: int) -> np.ndarray:
    """Fast bass average vs slow baseline + running peak; sustained engage,
    hysteresis release, warmup guard. Straight from the research notes."""
    fast = _ema(bass_raw, 0.25, fps)
    slow = _ema(bass_raw, 4.0, fps)
    n = len(bass_raw)
    drop = np.zeros(n, dtype=bool)
    peak = 1e-9
    engaged = False
    candidate = 0
    warmup = int(2.5 * fps)
    need = int(0.4 * fps)                  # sustained, not a single transient
    for i in range(n):
        peak = max(peak, fast[i])
        hot = fast[i] > (1.30 * slow[i] + 0.04) and fast[i] > 0.45 * peak
        if i < warmup:
            candidate = 0
            continue
        if not engaged:
            candidate = candidate + 1 if hot else 0
            if candidate >= need:
                engaged = True
        else:
            if fast[i] < (1.05 * slow[i]) and fast[i] < 0.35 * peak:
                engaged = False
                candidate = 0
        drop[i] = engaged
    return drop


def analyze(audio: Path, fps: int) -> tuple[Analysis, np.ndarray, int]:
    import soundfile as sf
    y, sr = sf.read(str(audio), dtype="float32", always_2d=True)
    mono = y.mean(axis=1)
    hop = int(round(sr / fps))
    n = max(1, int(np.ceil(len(mono) / hop)))
    win = 2048
    hann = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    m_bass = freqs < 150.0
    m_mid = (freqs >= 150.0) & (freqs < 4000.0)
    m_high = freqs >= 4000.0
    # log-spaced bar bins 40Hz..10kHz
    edges = np.geomspace(40.0, 10000.0, NBARS + 1)
    bar_masks = [(freqs >= edges[k]) & (freqs < edges[k + 1])
                 for k in range(NBARS)]

    rms = np.zeros(n, np.float32)
    bass = np.zeros(n, np.float32)
    mid = np.zeros(n, np.float32)
    high = np.zeros(n, np.float32)
    bars = np.zeros((n, NBARS), np.float32)
    for i in range(n):
        c = i * hop
        seg = mono[c:c + hop]
        if len(seg):
            rms[i] = np.sqrt(np.mean(seg ** 2))
        a = max(0, c - win // 2)
        chunk = mono[a:a + win]
        if len(chunk) < 256:
            continue
        if len(chunk) < win:
            chunk = np.pad(chunk, (0, win - len(chunk)))
        mag = np.abs(np.fft.rfft(chunk * hann))
        bass[i] = mag[m_bass].sum()
        mid[i] = mag[m_mid].sum()
        high[i] = mag[m_high].sum()
        for k, bm in enumerate(bar_masks):
            if bm.any():
                bars[i, k] = mag[bm].mean()

    bass_n = _norm95(bass)
    kick = _onsets(bass_n, fps)
    snare = _onsets(_norm95(mid), fps, refractory_s=0.10, k=1.6)
    drop = _detect_drop(bass_n, fps)
    bpm = _estimate_bpm(np.maximum(0, np.diff(bass_n, prepend=bass_n[0])), fps)

    rms_s = np.clip(_ema(_norm95(rms), 0.15, fps), 0, 1)
    slope_w = int(2.0 * fps)
    build = np.zeros(n, np.float32)
    for i in range(n):
        j = max(0, i - slope_w)
        build[i] = max(0.0, rms_s[i] - rms_s[j]) * 2.5
    build = np.clip(build, 0, 1) * (~drop)

    bars = np.clip(bars / np.maximum(
        np.percentile(bars[bars > 0], 95) if (bars > 0).any() else 1.0, 1e-9),
        0, 1)
    # per-bar smoothing: fast attack, slow decay
    for k in range(NBARS):
        prev = 0.0
        col = bars[:, k]
        for i in range(n):
            prev = col[i] if col[i] > prev else prev * 0.82
            col[i] = prev

    print(f"[lyric_viz] analysis: bpm~{bpm or '?'} kicks={int(kick.sum())} "
          f"snares={int(snare.sum())} drop_frames={int(drop.sum())} "
          f"({drop.mean() * 100:.0f}% of track)")
    return (Analysis(rms=np.clip(_ema(_norm95(rms), 0.08, fps), 0, 1),
                     bass=np.clip(_ema(bass_n, 0.08, fps), 0, 1),
                     high=np.clip(_ema(_norm95(high), 0.06, fps), 0, 1),
                     kick=kick, snare=snare, drop=drop, build=build,
                     bars=bars, bpm=bpm),
            y, sr)


# ---------------------------------------------------------------------------
# 3. Styles — the genre presets from the research
# ---------------------------------------------------------------------------

FONTS_DIR = REPO / "assets" / "fonts"


@dataclass
class Style:
    """A genre FORMAT, not a recolor (Zeke directive 2026-08-26): the viz
    geometry, the letterforms, and the layout differ per genre — per the
    tapedit genre guide (four families: bars / waveform / radial / particle)
    and the typography research (festival = bold condensed caps; dubstep =
    heavy + procedurally glitched; deep house = thin minimal letterspaced)."""
    name: str
    palette: list[tuple[int, int, int]]     # RGB accents, rotated per flash
    bg: str                                 # starfield | plasma | flat
    viz: str                                # radial | bars_center | bars | wave
    particles: bool                         # kick-burst particles (radial EDM)
    font_title: str                         # ttf in assets/fonts (or "")
    font_lyrics: str
    caps: bool                              # uppercase lyrics
    tracking: float                         # title letterspacing, em fraction
    logo_pos: str                           # center (inside ring) | top
    strobe: bool                            # hard white flash on kick in drop
    glitch: bool                            # slice-tear on drop
    rgb_split: bool                         # chromatic split on drop
    shake: float                            # px of drop shake (0 = none)
    zoom_punch: float                       # extra zoom per kick (0 = none)
    scramble: bool                          # lyric chars scramble on drop
    breakdown_desat: bool                   # desaturate quiet sections
    flicker: float                          # persistent unease flicker 0..1
    text_col: tuple[int, int, int] = (235, 235, 240)


STYLES: dict[str, Style] = {
    # festival EDM — the Trap Nation family: RADIAL ring around a centered
    # logo, particle bursts on the kick, bold condensed caps (Anton).
    "edm": Style("edm",
                 palette=[(255, 70, 130), (70, 170, 255), (170, 90, 255),
                          (255, 210, 70)],
                 bg="starfield", viz="radial", particles=True,
                 font_title="Anton-Regular.ttf",
                 font_lyrics="BebasNeue-Regular.ttf", caps=True,
                 tracking=0.06, logo_pos="center",
                 strobe=True, glitch=False, rgb_split=True,
                 shake=6.0, zoom_punch=0.035, scramble=True,
                 breakdown_desat=True, flicker=0.04),
    # dubstep/riddim — bold CENTERED MIRRORED bars, heavy glitched type
    # (ArchivoBlack + procedural tearing), acid palette, half-time weight.
    "dubstep": Style("dubstep",
                     palette=[(150, 255, 60), (190, 70, 255), (255, 50, 90),
                              (60, 255, 220)],
                     bg="starfield", viz="bars_center", particles=False,
                     font_title="RubikGlitch-Regular.ttf",
                     font_lyrics="ArchivoBlack-Regular.ttf", caps=True,
                     tracking=0.02, logo_pos="top",
                     strobe=True, glitch=True, rgb_split=True,
                     shake=10.0, zoom_punch=0.03, scramble=True,
                     breakdown_desat=True, flicker=0.07),
    # DARK dubstep (Zeke's ask 08-26) — near-black, blood red + void purple,
    # heavier menace: more flicker, more glitch, slower palette rotation.
    "darkdubstep": Style("darkdubstep",
                         palette=[(190, 30, 45), (110, 40, 170), (60, 60, 80),
                                  (220, 200, 190)],
                         bg="starfield", viz="bars_center", particles=False,
                         font_title="RubikGlitch-Regular.ttf",
                         font_lyrics="ArchivoBlack-Regular.ttf", caps=True,
                         tracking=0.04, logo_pos="top",
                         strobe=True, glitch=True, rgb_split=True,
                         shake=13.0, zoom_punch=0.04, scramble=True,
                         breakdown_desat=True, flicker=0.10,
                         text_col=(200, 190, 190)),
    # deep house — soft horizontal WAVEFORM, thin minimal letterspaced
    # lowercase (Poppins Light), dark neon plasma, NO strobes.
    "deephouse": Style("deephouse",
                       palette=[(255, 90, 190), (90, 255, 235), (150, 100, 255),
                                (255, 160, 90)],
                       bg="plasma", viz="wave", particles=False,
                       font_title="Poppins-Light.ttf",
                       font_lyrics="Poppins-Light.ttf", caps=False,
                       tracking=0.32, logo_pos="top",
                       strobe=False, glitch=False, rgb_split=False,
                       shake=0.0, zoom_punch=0.015, scramble=False,
                       breakdown_desat=False, flicker=0.0),
}


# ---------------------------------------------------------------------------
# 4. Renderer
# ---------------------------------------------------------------------------

def _font(size: int, name: str = ""):
    from PIL import ImageFont
    if name:
        try:
            return ImageFont.truetype(str(FONTS_DIR / name), size)
        except Exception:
            pass
    for fallback in ("impact.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(rf"C:\Windows\Fonts\{fallback}", size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_tracked(draw, xy, text: str, font, fill, tracking_px: float) -> float:
    """Char-by-char draw with letterspacing; returns total width."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking_px
    return x - xy[0] - tracking_px


def _tracked_width(draw, text: str, font, tracking_px: float) -> float:
    return (sum(draw.textlength(c, font=font) for c in text)
            + tracking_px * max(0, len(text) - 1))


_SCRAMBLE_SET = "!<>-_\\/[]{}=+*^?#$%&0123456789"


@dataclass
class Renderer:
    W: int
    H: int
    style: Style
    lines: list[list[Word]]
    fps: int
    logo: "object | None" = None            # PIL RGBA or None
    title: str = ""
    wave_env: np.ndarray | None = None      # |pcm| envelope @ WAVE_HZ, for viz=wave
    _fonts: dict = field(default_factory=dict)
    _stars: np.ndarray | None = None
    _plasma_xy: tuple | None = None
    _vignette: np.ndarray | None = None
    _logo_mask: np.ndarray | None = None    # logo/title alpha at base size
    _flash: float = 0.0                     # decaying kick flash
    _zoom: float = 0.0                      # decaying zoom punch
    _pal_i: int = 0
    _parts: np.ndarray | None = None        # particles [n,5]: x y vx vy life
    _logo_rgb: np.ndarray | None = None     # logo colors, when a real image
    deck: "list | None" = None              # [(rgb,f32 HxWx3)] centerpiece images
    deck_idx: np.ndarray | None = None      # per-frame index into deck
    bgclips: "list | None" = None           # [T x h x w x 3 uint8] VJ loops (1/4 res)
    bgclip_idx: np.ndarray | None = None    # per-frame clip index
    corner_logo: "tuple | None" = None      # (rgb, mask) small watermark
    shape: str = "none"                     # cube|pyramid|cylinder|orb|logo3d|none
    _rot: float = 0.0                       # accumulated 3D rotation

    # -- setup ------------------------------------------------------------
    def __post_init__(self):
        rng = np.random.default_rng(7)
        n = 380
        self._stars = np.stack([rng.uniform(0, self.W, n),
                                rng.uniform(0, self.H, n),
                                rng.uniform(0.25, 1.0, n)], axis=1)
        q = 4
        yy, xx = np.mgrid[0:self.H // q, 0:self.W // q].astype(np.float32)
        self._plasma_xy = (xx / (self.W // q), yy / (self.H // q))
        Y, X = np.ogrid[0:self.H, 0:self.W]
        d = np.sqrt(((X - self.W / 2) / (self.W / 2)) ** 2
                    + ((Y - self.H / 2) / (self.H / 2)) ** 2)
        self._vignette = np.clip(1.0 - 0.55 * np.clip(d - 0.55, 0, 1) ** 1.5,
                                 0, 1).astype(np.float32)[..., None]
        self._logo_mask = self._make_logo_mask()

    def font(self, size: int, name: str = ""):
        key = (size, name)
        if key not in self._fonts:
            self._fonts[key] = _font(size, name)
        return self._fonts[key]

    def _make_logo_mask(self) -> np.ndarray | None:
        """Alpha mask (H',W') float 0..1 of the logo image or title text."""
        from PIL import Image, ImageDraw
        if self.logo is not None:
            lw = int(self.W * (0.22 if self.style.viz == "radial" else 0.30))
            lh = max(1, int(lw * self.logo.height / max(1, self.logo.width)))
            logo = self.logo.resize((lw, lh))
            rgb = np.asarray(logo.convert("RGB"), np.float32)
            alpha = np.asarray(logo.split()[-1], np.float32) / 255.0
            if alpha.std() < 0.02:
                # No real alpha (white background baked in — Zeke's
                # 'Tzeke000 symble.PNG'): mask = distance from white.
                whiteness = rgb.min(axis=2) / 255.0
                alpha = np.clip((0.92 - whiteness) * 4.0, 0.0, 1.0)
            self._logo_rgb = rgb
            return alpha
        if not self.title:
            return None
        text = self.title.upper() if self.style.caps else self.title
        fs = int(self.H * (0.085 if self.style.viz == "radial" else 0.11))
        f = self.font(fs, self.style.font_title)
        track = self.style.tracking * fs
        probe = Image.new("L", (4, 4))
        dr = ImageDraw.Draw(probe)
        box = dr.textbbox((0, 0), text, font=f)
        wpx = int(_tracked_width(dr, text, f, track)) + 12
        hpx = box[3] - box[1] + 12
        img = Image.new("L", (wpx, hpx), 0)
        _draw_tracked(ImageDraw.Draw(img), (6, 6 - box[1]), text, f, 255, track)
        return np.asarray(img, np.float32) / 255.0

    def _logo_center(self) -> tuple[int, int]:
        if self.style.logo_pos == "center":
            return self.W // 2, int(self.H * 0.42)
        return self.W // 2, int(self.H * 0.30)

    # -- pieces -----------------------------------------------------------
    def _bg(self, i: int, t: float, a: Analysis) -> np.ndarray:
        img = np.zeros((self.H, self.W, 3), np.float32)
        img[:] = (8, 8, 14)
        if self.bgclips:
            # music-reactive VJ loop background (Beeple CC loops etc):
            # brightness rides the energy, kick flashes push it, loops cut on
            # phrase boundaries via bgclip_idx.
            ci = (int(self.bgclip_idx[i]) % len(self.bgclips)
                  if self.bgclip_idx is not None else 0)
            clip = self.bgclips[ci]
            frame = clip[i % len(clip)].astype(np.float32)
            up = cv2.resize(frame, (self.W, self.H),
                            interpolation=cv2.INTER_LINEAR)
            gain = 0.28 + 0.62 * a.rms[i] + (0.25 if a.kick[i] else 0.0)
            img += up * gain
            return img
        if self.style.bg == "starfield":
            drop = bool(a.drop[i])
            speed = 0.4 + 2.2 * a.bass[i] + (2.0 if drop else 0.0)
            self._stars[:, 0] -= speed * self._stars[:, 2]
            self._stars[:, 0] %= self.W
            xs = self._stars[:, 0].astype(int)
            ys = self._stars[:, 1].astype(int)
            bright = (60 + 180 * self._stars[:, 2]
                      * (0.35 + 0.65 * a.high[i]))
            img[ys, xs] += bright[:, None]
            near = self._stars[:, 2] > 0.7
            img[ys[near], np.minimum(xs[near] + 1, self.W - 1)] += \
                (bright[near] * 0.6)[:, None]
        elif self.style.bg == "plasma":
            xx, yy = self._plasma_xy
            e = 0.25 + 0.75 * a.rms[i]
            z = (np.sin(xx * 6.3 + t * 0.9) + np.sin(yy * 5.1 - t * 0.7)
                 + np.sin((xx + yy) * 4.2 + t * 0.5)
                 + np.sin(np.sqrt((xx - 0.5) ** 2 + (yy - 0.5) ** 2) * 11
                          - t * (1.1 + a.bass[i])))
            z = (z + 4.0) / 8.0
            c1 = np.array(self.style.palette[0], np.float32)
            c2 = np.array(self.style.palette[1], np.float32)
            small = (z[..., None] * c1 + (1 - z[..., None]) * c2) * 0.45 * e
            img += cv2.resize(small, (self.W, self.H),
                              interpolation=cv2.INTER_LINEAR)
        return img

    def _viz(self, img: np.ndarray, i: int, a: Analysis) -> None:
        v = self.style.viz
        if v == "radial":
            self._viz_radial(img, i, a)
        elif v == "bars_center":
            self._viz_bars_center(img, i, a)
        elif v == "wave":
            self._viz_wave(img, i, a)
        else:
            self._viz_bars_bottom(img, i, a)
        if self.style.particles:
            self._viz_particles(img, i, a)

    def _pal(self, off: int = 0) -> np.ndarray:
        p = self.style.palette
        return np.array(p[(self._pal_i + off) % len(p)], np.float32)

    def _viz_bars_bottom(self, img: np.ndarray, i: int, a: Analysis) -> None:
        col = self._pal()
        bw = self.W / NBARS
        base = self.H - 6
        maxh = self.H * 0.16
        for k in range(NBARS):
            h = int(a.bars[i, k] * maxh)
            if h <= 0:
                continue
            x0, x1 = int(k * bw + 2), int((k + 1) * bw - 2)
            img[base - h:base, x0:x1] += col * (0.20 + 0.5 * a.bars[i, k])

    def _viz_bars_center(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Dubstep format: bold bars mirrored about a center line — 'the bass
        and 808s deserve bars that punch' (genre guide)."""
        col = self._pal()
        bw = self.W / NBARS
        cy = int(self.H * 0.55)
        maxh = self.H * 0.16 * (1.0 + 0.5 * a.bass[i])
        for k in range(NBARS):
            h = int(a.bars[i, k] * maxh)
            if h <= 0:
                continue
            x0, x1 = int(k * bw + 3), int((k + 1) * bw - 3)
            img[cy - h:cy + h, x0:x1] += col * (0.25 + 0.55 * a.bars[i, k])

    def _viz_radial(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """EDM format — the Trap Nation family: spectrum spokes around a
        centered logo, ring radius breathing with the bass."""
        cx, cy = self._logo_center()
        r0 = self.H * 0.155 * (1.0 + 0.12 * a.bass[i])
        maxlen = self.H * 0.13
        col = self._pal()
        col2 = self._pal(1)
        n = NBARS
        for k in range(n):
            mag = a.bars[i, k]
            if mag <= 0.02:
                continue
            # mirrored: same band on both sides, bass at the bottom
            for sign in (1, -1):
                th = np.pi / 2 + sign * np.pi * (k + 0.5) / n
                c0 = (int(cx + r0 * np.cos(th)), int(cy + r0 * np.sin(th)))
                c1 = (int(cx + (r0 + mag * maxlen) * np.cos(th)),
                      int(cy + (r0 + mag * maxlen) * np.sin(th)))
                cv2.line(img, c0, c1,
                         tuple(float(x) for x in (col if k % 2 else col2)
                               * (0.35 + 0.65 * mag)),
                         thickness=3, lineType=cv2.LINE_AA)
        # thin base ring
        cv2.circle(img, (cx, cy), int(r0),
                   tuple(float(x) for x in col * (0.25 + 0.5 * a.rms[i])),
                   thickness=2, lineType=cv2.LINE_AA)

    WAVE_HZ = 100

    def _viz_wave(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Deep house format: a soft horizontal waveform tracing amplitude —
        'the visualizer should breathe, not compete' (genre guide)."""
        env = self.wave_env
        if env is None:
            return self._viz_bars_bottom(img, i, a)
        cy = int(self.H * 0.80)
        span = int(2.0 * self.WAVE_HZ)               # 2s window
        c = int(i / self.fps * self.WAVE_HZ)
        a0 = max(0, c - span // 2)
        seg = env[a0:a0 + span]
        if len(seg) < 8:
            return
        xs = np.linspace(0, self.W - 1, len(seg)).astype(np.int32)
        amp = self.H * 0.075 * (0.4 + 0.6 * a.rms[i])
        mod = np.sin(np.linspace(0, 6.28, len(seg)) + i * 0.06)
        ys = (cy - seg / max(1e-6, env.max()) * amp
              * (0.75 + 0.25 * mod)).astype(np.int32)
        pts = np.stack([xs, ys], axis=1).reshape(-1, 1, 2)
        col = self._pal()
        cv2.polylines(img, [pts], False,
                      tuple(float(x) for x in col * 0.85),
                      thickness=2, lineType=cv2.LINE_AA)
        # mirrored faint reflection
        pts2 = np.stack([xs, (2 * cy - ys).astype(np.int32)], axis=1).reshape(-1, 1, 2)
        cv2.polylines(img, [pts2], False,
                      tuple(float(x) for x in col * 0.25),
                      thickness=1, lineType=cv2.LINE_AA)

    def _viz_particles(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Kick bursts from the ring edge — 'radial with particles' (EDM)."""
        if self._parts is None:
            self._parts = np.zeros((0, 5), np.float32)
        if a.kick[i]:
            rng = np.random.default_rng(i)
            n = 26 if a.drop[i] else 12
            th = rng.uniform(0, 2 * np.pi, n)
            cx, cy = self._logo_center()
            r0 = self.H * 0.16
            speed = rng.uniform(2.0, 6.0, n) * (1.0 + 1.2 * a.bass[i])
            new = np.stack([cx + r0 * np.cos(th), cy + r0 * np.sin(th),
                            speed * np.cos(th), speed * np.sin(th),
                            np.full(n, 1.0)], axis=1).astype(np.float32)
            self._parts = np.concatenate([self._parts, new])
        if not len(self._parts):
            return
        p = self._parts
        p[:, 0] += p[:, 2]
        p[:, 1] += p[:, 3]
        p[:, 4] -= 1.0 / (self.fps * 1.2)
        self._parts = p = p[p[:, 4] > 0]
        col = self._pal()
        for x, y, _, _, life in p:
            xi, yi = int(x), int(y)
            if 1 <= xi < self.W - 1 and 1 <= yi < self.H - 1:
                img[yi - 1:yi + 2, xi - 1:xi + 2] += col * life * 0.8

    def _logo(self, img: np.ndarray, i: int, a: Analysis) -> None:
        mask = self._logo_mask
        if mask is None:
            return
        drop = bool(a.drop[i])
        scale = 1.0 + (0.10 + 0.06 * (1 if drop else 0)) * a.bass[i]
        mh, mw = mask.shape
        sw, sh = max(2, int(mw * scale)), max(2, int(mh * scale))
        m = cv2.resize(mask, (sw, sh), interpolation=cv2.INTER_LINEAR)
        cx, cy = self._logo_center()
        x0, y0 = cx - sw // 2, cy - sh // 2
        xa, ya = max(0, x0), max(0, y0)
        xb, yb = min(self.W, x0 + sw), min(self.H, y0 + sh)
        if xb <= xa or yb <= ya:
            return
        sub = m[ya - y0:yb - y0, xa - x0:xb - x0]
        # glowing OUTLINE (the Greyland treatment): edge of the mask, blurred,
        # in palette color scaled by bass; fill = the logo's REAL colors when
        # we have an image, else a dim flat fill for text titles.
        edge = cv2.morphologyEx(sub, cv2.MORPH_GRADIENT,
                                np.ones((3, 3), np.uint8))
        glow = cv2.GaussianBlur(edge, (0, 0), 3 + 6 * a.bass[i])
        col = np.array(self.style.palette[self._pal_i % len(self.style.palette)],
                       np.float32)
        region = img[ya:yb, xa:xb]
        if self._logo_rgb is not None:
            lrgb = cv2.resize(self._logo_rgb, (sw, sh),
                              interpolation=cv2.INTER_LINEAR)[
                ya - y0:yb - y0, xa - x0:xb - x0]
            m3 = sub[..., None]
            region[:] = region * (1 - m3) + lrgb * m3 * (0.75 + 0.25 * a.rms[i])
        else:
            region += sub[..., None] * np.array((70, 70, 78), np.float32)
        region += glow[..., None] * col * (0.9 + 1.3 * a.bass[i])
        region += edge[..., None] * col * 0.8

    # -- image deck: album cover(s) as the centerpiece, flipping on the beat --
    def _deck(self, img: np.ndarray, i: int, a: Analysis) -> None:
        if not self.deck:
            return
        idx = int(self.deck_idx[i]) % len(self.deck) if self.deck_idx is not None else 0
        art = self.deck[idx]
        drop = bool(a.drop[i])
        maxh = self.H * (0.34 if self.style.viz == "radial" else 0.38)
        scale = (1.0 + (0.06 + 0.05 * (1 if drop else 0)) * a.bass[i])
        ah, aw = art.shape[:2]
        s = min(maxh / ah, (self.W * 0.55) / aw) * scale
        sw, sh = max(2, int(aw * s)), max(2, int(ah * s))
        card = cv2.resize(art, (sw, sh), interpolation=cv2.INTER_AREA)
        cx, cy = self._logo_center()
        x0, y0 = cx - sw // 2, cy - sh // 2
        xa, ya = max(0, x0), max(0, y0)
        xb, yb = min(self.W, x0 + sw), min(self.H, y0 + sh)
        if xb <= xa or yb <= ya:
            return
        sub = card[ya - y0:yb - y0, xa - x0:xb - x0]
        region = img[ya:yb, xa:xb]
        region[:] = region * 0.15 + sub * (0.80 + 0.20 * a.rms[i])
        # glow border in palette color, bass-scaled (the outline treatment)
        col = self._pal()
        border = np.zeros(sub.shape[:2], np.float32)
        t = max(2, int(2 + 4 * a.bass[i]))
        border[:t], border[-t:], border[:, :t], border[:, -t:] = 1, 1, 1, 1
        glow = cv2.GaussianBlur(border, (0, 0), 4 + 8 * a.bass[i])
        region += glow[..., None] * col * (0.6 + 1.0 * a.bass[i])

    def _corner(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Small logo watermark, bottom-right, when the deck owns the center."""
        if self.corner_logo is None:
            return
        rgb, mask = self.corner_logo
        lh, lw = mask.shape
        pad = int(self.H * 0.03)
        y0, x0 = self.H - lh - pad, self.W - lw - pad
        m3 = mask[..., None] * (0.75 + 0.25 * a.bass[i])
        region = img[y0:y0 + lh, x0:x0 + lw]
        region[:] = region * (1 - m3) + rgb * m3

    # -- 3D layer: rotating wireframe shapes reacting to the music ----------
    _SHAPES: dict = None

    def _shape_mesh(self):
        if Renderer._SHAPES is None:
            c = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1)
                          for z in (-1, 1)], np.float32)
            cube_e = [(a, b) for a in range(8) for b in range(a + 1, 8)
                      if np.sum(np.abs(c[a] - c[b]) > 1e-6) == 1]
            pyr_v = np.array([[-1, 1, -1], [1, 1, -1], [1, 1, 1], [-1, 1, 1],
                              [0, -1.2, 0]], np.float32)
            pyr_e = [(0, 1), (1, 2), (2, 3), (3, 0),
                     (0, 4), (1, 4), (2, 4), (3, 4)]
            n = 12
            th = np.linspace(0, 2 * np.pi, n, endpoint=False)
            top = np.stack([np.cos(th), np.full(n, -1.0), np.sin(th)], 1)
            bot = np.stack([np.cos(th), np.full(n, 1.0), np.sin(th)], 1)
            cyl_v = np.concatenate([top, bot]).astype(np.float32)
            cyl_e = ([(k, (k + 1) % n) for k in range(n)]
                     + [(n + k, n + (k + 1) % n) for k in range(n)]
                     + [(k, n + k) for k in range(0, n, 2)])
            Renderer._SHAPES = {"cube": (c, cube_e), "pyramid": (pyr_v, pyr_e),
                                "cylinder": (cyl_v, cyl_e)}
        return Renderer._SHAPES

    def _viz_shape(self, img: np.ndarray, i: int, a: Analysis) -> None:
        if self.shape in ("none", ""):
            return
        cx, cy = self._logo_center()
        drop = bool(a.drop[i])
        self._rot += (0.012 + 0.05 * a.bass[i] + (0.03 if drop else 0.0))
        size = self.H * 0.14 * (1.0 + 0.22 * a.bass[i])
        col = self._pal()
        layer = np.zeros_like(img)
        if self.shape == "orb":
            # glowing sphere: radial falloff core + two rotating rings
            rr = int(size)
            yy, xx = np.ogrid[-rr:rr, -rr:rr]
            d = np.sqrt(xx ** 2 + yy ** 2) / rr
            core = np.clip(1.0 - d, 0, 1) ** 2.2 * (0.5 + 0.8 * a.rms[i])
            ya, xa = cy - rr, cx - rr
            if 0 <= ya and ya + 2 * rr <= self.H and 0 <= xa and xa + 2 * rr <= self.W:
                layer[ya:ya + 2 * rr, xa:xa + 2 * rr] += core[..., None] * col
            for k, tilt in ((0, 0.5), (1, -0.35)):
                th = np.linspace(0, 2 * np.pi, 60)
                r3 = np.stack([np.cos(th), np.sin(th) * tilt,
                               np.sin(th) * 0.4], 1)
                pts = self._project(r3, size * 1.25, cx, cy,
                                    self._rot * (1.3 if k else 1.0))
                cv2.polylines(layer, [pts], True,
                              tuple(float(x) for x in col * 0.8), 2, cv2.LINE_AA)
        elif self.shape == "logo3d" and self._logo_rgb is not None:
            # pseudo-3D spin: perspective-warp the logo about its vertical axis
            mask = self._logo_mask
            lh, lw = mask.shape
            ang = self._rot
            fold = abs(np.cos(ang))
            wq = max(8, int(lw * fold * (size * 2 / lh)))
            hq = int(size * 2)
            sk = 0.18 * np.sin(ang)          # slight perspective skew
            dstq = np.float32([[cx - wq / 2, cy - hq / 2 + sk * wq],
                               [cx + wq / 2, cy - hq / 2 - sk * wq],
                               [cx + wq / 2, cy + hq / 2 + sk * wq],
                               [cx - wq / 2, cy + hq / 2 - sk * wq]])
            srcq = np.float32([[0, 0], [lw, 0], [lw, lh], [0, lh]])
            M = cv2.getPerspectiveTransform(srcq, dstq)
            wrgb = cv2.warpPerspective(self._logo_rgb, M, (self.W, self.H))
            wm = cv2.warpPerspective(mask, M, (self.W, self.H))[..., None]
            img[:] = img * (1 - wm) + wrgb * wm * (0.75 + 0.25 * a.rms[i])
            edge = cv2.morphologyEx(wm[..., 0], cv2.MORPH_GRADIENT,
                                    np.ones((3, 3), np.uint8))
            layer += cv2.GaussianBlur(edge, (0, 0), 3)[..., None] * col
        else:
            mesh = self._shape_mesh().get(self.shape)
            if mesh is None:
                return
            v, edges = mesh
            pts = self._project(v, size, cx, cy, self._rot)
            for e0, e1 in edges:
                cv2.line(layer, tuple(pts[e0][0]), tuple(pts[e1][0]),
                         tuple(float(x) for x in col), 2, cv2.LINE_AA)
        glow = cv2.GaussianBlur(layer, (0, 0), 5 + 7 * a.bass[i])
        img += layer + glow * (0.5 + 0.8 * a.rms[i])

    def _project(self, v: np.ndarray, size: float, cx: int, cy: int,
                 rot: float) -> np.ndarray:
        ry, rx = rot, rot * 0.62
        cyr, syr = np.cos(ry), np.sin(ry)
        cxr, sxr = np.cos(rx), np.sin(rx)
        Ry = np.array([[cyr, 0, syr], [0, 1, 0], [-syr, 0, cyr]], np.float32)
        Rx = np.array([[1, 0, 0], [0, cxr, -sxr], [0, sxr, cxr]], np.float32)
        p = v @ Ry.T @ Rx.T
        z = p[:, 2] + 4.0
        f = 3.2 / z
        out = np.stack([cx + p[:, 0] * f * size, cy + p[:, 1] * f * size], 1)
        return out.astype(np.int32).reshape(-1, 1, 2)

    def _current_line(self, t: float) -> tuple[list[Word] | None, int]:
        LEAD = 0.25
        for ln in self.lines:
            if ln[0].start - LEAD <= t <= ln[-1].end + 0.6:
                active = -1
                for k, w in enumerate(ln):
                    if w.start <= t:
                        active = k
                return ln, active
        return None, -1

    def _lyrics(self, img: np.ndarray, i: int, t: float, a: Analysis) -> None:
        from PIL import Image, ImageDraw
        ln, active = self._current_line(t)
        if not ln:
            return
        drop = bool(a.drop[i])
        words = []
        for k, w in enumerate(ln):
            txt = w.text.upper() if self.style.caps else w.text
            if drop and self.style.scramble and k != active:
                rng = random.Random(hash((i // 3, k, len(txt))))
                txt = "".join(c if c == " " or rng.random() < 0.35
                              else rng.choice(_SCRAMBLE_SET) for c in txt)
            words.append(txt)
        fs = int(self.H * (0.062 if self.style.viz == "radial" else 0.075))
        f = self.font(fs, self.style.font_lyrics)
        layer = Image.new("RGB", (self.W, self.H), (0, 0, 0))
        draw = ImageDraw.Draw(layer)
        space = draw.textlength(" ", font=f)
        widths = [draw.textlength(w, font=f) for w in words]
        total = sum(widths) + space * (len(words) - 1)
        while total > self.W * 0.92 and fs > 18:
            fs = int(fs * 0.9)
            f = self.font(fs, self.style.font_lyrics)
            space = draw.textlength(" ", font=f)
            widths = [draw.textlength(w, font=f) for w in words]
            total = sum(widths) + space * (len(words) - 1)
        x = (self.W - total) / 2
        y = int(self.H * (0.78 if self.style.viz == "radial" else 0.66))
        accent = self.style.palette[self._pal_i % len(self.style.palette)]
        xx = x
        for k, (w, wd) in enumerate(zip(words, widths)):
            draw.text((xx, y), w, font=f,
                      fill=accent if k == active else
                      ((120, 120, 130) if not drop else accent))
            xx += wd + space
        lyr = np.asarray(layer, np.float32)
        glow = cv2.GaussianBlur(lyr, (0, 0), 4 + 8 * a.rms[i])
        img += lyr * 0.95 + glow * (0.35 + 0.75 * a.rms[i])

    # -- drop FX ----------------------------------------------------------
    def _fx(self, img: np.ndarray, i: int, a: Analysis) -> np.ndarray:
        s = self.style
        drop = bool(a.drop[i])
        if a.kick[i]:
            self._pal_i += 1
            self._flash = max(self._flash, (1.0 if drop else 0.45))
            self._zoom = max(self._zoom, s.zoom_punch * (1.5 if drop else 1.0))
        if drop and s.glitch:
            rng = random.Random(i // 2)
            for _ in range(rng.randint(2, 5)):
                y0 = rng.randrange(0, self.H - 12)
                h = rng.randrange(4, 26)
                off = rng.randrange(-int(30 + 60 * a.bass[i]),
                                    int(30 + 60 * a.bass[i]) or 1)
                img[y0:y0 + h] = np.roll(img[y0:y0 + h], off, axis=1)
        if drop and s.rgb_split:
            off = int(2 + 6 * a.bass[i])
            img[..., 0] = np.roll(img[..., 0], off, axis=1)
            img[..., 2] = np.roll(img[..., 2], -off, axis=1)
        # geometric: zoom punch + shake in one affine
        z = 1.0 + self._zoom
        dx = dy = 0.0
        if drop and s.shake > 0:
            rng = random.Random(i)
            amp = s.shake * (0.4 + 0.6 * a.bass[i])
            dx, dy = rng.uniform(-amp, amp), rng.uniform(-amp, amp)
        if z > 1.0005 or abs(dx) + abs(dy) > 0.5:
            M = np.array([[z, 0, (1 - z) * self.W / 2 + dx],
                          [0, z, (1 - z) * self.H / 2 + dy]], np.float32)
            img = cv2.warpAffine(img, M, (self.W, self.H),
                                 flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REFLECT)
        # strobe/kick flash
        if self._flash > 0.02:
            if s.strobe and drop:
                img += self._flash * 175.0
            else:
                col = np.array(s.palette[self._pal_i % len(s.palette)],
                               np.float32)
                img += self._flash * 0.35 * col
        self._flash *= 0.62
        self._zoom *= 0.80
        # persistent flicker (build unease)
        if s.flicker > 0:
            img *= 1.0 + s.flicker * (0.5 + a.build[i]) * np.sin(i * 2.7)
        return img

    # -- one frame --------------------------------------------------------
    def frame(self, i: int, t: float, a: Analysis) -> np.ndarray:
        img = self._bg(i, t, a)
        self._viz(img, i, a)
        if self.deck:
            self._deck(img, i, a)
        elif self.shape in ("none", ""):
            self._logo(img, i, a)
        if self.shape not in ("none", ""):
            self._viz_shape(img, i, a)
        self._corner(img, i, a)
        self._lyrics(img, i, t, a)
        img = self._fx(img, i, a)
        img *= self._vignette
        if self.style.breakdown_desat:
            sat = 0.45 + 0.55 * max(a.rms[i], 1.0 if a.drop[i] else 0.0)
            if sat < 0.99:
                gray = img.mean(axis=2, keepdims=True)
                img = gray + (img - gray) * sat
        noise = np.random.default_rng(i).standard_normal(
            (self.H // 2, self.W // 2, 1)).astype(np.float32)
        img += cv2.resize(noise, (self.W, self.H))[..., None] * \
            (2.0 + 5.0 * a.rms[i])
        return np.clip(img, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# 5. Encode (PyAV) — proven in v0
# ---------------------------------------------------------------------------

def encode(out: Path, frames_iter, fps: int, audio_pcm: np.ndarray, sr: int,
           W: int, H: int) -> None:
    import av
    container = av.open(str(out), mode="w")
    try:
        vstream = container.add_stream("h264", rate=fps)
        vstream.pix_fmt = "yuv420p"
    except Exception:
        vstream = container.add_stream("mpeg4", rate=fps)
    vstream.width, vstream.height = W, H
    astream = container.add_stream("aac", rate=sr)
    n = 0
    for rgb in frames_iter:
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        for pkt in vstream.encode(frame):
            container.mux(pkt)
        n += 1
    for pkt in vstream.encode():
        container.mux(pkt)
    pcm16 = (np.clip(audio_pcm, -1.0, 1.0) * 32767).astype(np.int16)
    channels = pcm16.shape[1]
    layout = "stereo" if channels == 2 else "mono"
    resampler = av.AudioResampler(format="fltp", layout=layout, rate=sr)
    CH = 4096
    pts = 0
    for i in range(0, len(pcm16), CH):
        chunk = pcm16[i:i + CH]
        af = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(chunk.reshape(1, -1)),
            format="s16", layout=layout)
        af.sample_rate = sr
        af.pts = pts
        pts += chunk.shape[0]
        for rf in resampler.resample(af):
            for pkt in astream.encode(rf):
                container.mux(pkt)
    for rf in resampler.resample(None):
        for pkt in astream.encode(rf):
            container.mux(pkt)
    for pkt in astream.encode():
        container.mux(pkt)
    container.close()
    print(f"[lyric_viz] encoded {n} frames -> {out}")


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True, type=Path)
    ap.add_argument("--lyrics", type=Path, default=None)
    ap.add_argument("--logo", type=Path, default=None)
    ap.add_argument("--title", default="")
    ap.add_argument("--style", default="edm", choices=sorted(STYLES))
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--size", default="")
    ap.add_argument("--aspect", default="16:9", choices=["16:9", "9:16", "1:1"],
                    help="16:9 YouTube, 9:16 TikTok/Shorts/Reels, 1:1 square")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--images", default="",
                    help="dir or comma-list: centerpiece deck (album covers), "
                         "flips on the beat")
    ap.add_argument("--bgvideo", default="",
                    help="dir or comma-list of VJ loop videos — reactive "
                         "background (replaces starfield/plasma)")
    ap.add_argument("--shape", default="none",
                    choices=["none", "cube", "pyramid", "cylinder", "orb",
                             "logo3d"],
                    help="rotating music-reactive 3D centerpiece")
    ap.add_argument("--no-vocals", action="store_true",
                    help="skip transcription (instrumental track)")
    args = ap.parse_args()

    if args.size:
        W, H = (int(v) for v in args.size.lower().split("x"))
    else:
        W, H = {"16:9": (1280, 720), "9:16": (720, 1280),
                "1:1": (1080, 1080)}[args.aspect]
    out = args.out or args.audio.with_suffix(f".{args.style}.mp4")
    style = STYLES[args.style]
    t0 = time.time()

    if args.no_vocals:
        lines: list[list[Word]] = []
    else:
        heard = transcribe_words(args.audio, args.device)
        if args.lyrics and args.lyrics.is_file():
            lines = align_to_lyrics(heard,
                                    args.lyrics.read_text(encoding="utf-8"))
        else:
            lines = lines_from_transcript(heard)

    analysis, pcm, sr = analyze(args.audio, args.fps)
    if sr > 48000:
        # hi-res masters (Zeke's love the mirror.wav is 192kHz float) break the
        # AAC encoder (avcodec_open2 err 22, caps at 96k) — resample to 48k.
        from scipy.signal import resample_poly
        import math
        g = math.gcd(48000, sr)
        pcm = resample_poly(pcm, 48000 // g, sr // g, axis=0).astype(np.float32)
        print(f"[lyric_viz] resampled {sr} -> 48000 Hz for AAC")
        sr = 48000
    duration = len(pcm) / sr
    n_frames = int(np.ceil(duration * args.fps))

    logo = None
    if args.logo and args.logo.is_file():
        from PIL import Image
        logo = Image.open(args.logo).convert("RGBA")

    # |pcm| envelope for the wave viz (deep house)
    mono_abs = np.abs(pcm.mean(axis=1))
    step = max(1, sr // Renderer.WAVE_HZ)
    n_env = len(mono_abs) // step
    wave_env = mono_abs[:n_env * step].reshape(n_env, step).mean(axis=1)
    k = max(1, Renderer.WAVE_HZ // 8)
    wave_env = np.convolve(wave_env, np.ones(k) / k, mode="same")

    # image deck: album covers as the flipping centerpiece
    deck = None
    deck_idx = None
    corner = None
    if args.images:
        from PIL import Image
        p = Path(args.images)
        paths = (sorted([q for q in p.iterdir()
                         if q.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")])
                 if p.is_dir() else [Path(s) for s in args.images.split(",")])
        deck = [np.asarray(Image.open(q).convert("RGB"), np.float32)
                for q in paths if q.is_file()]
        if deck:
            # flip on the beat: every 4 kicks normally, every 2 in a drop
            kicks = np.cumsum(analysis.kick.astype(int)
                              + analysis.kick.astype(int) * analysis.drop)
            deck_idx = (kicks // 4).astype(int)
            print(f"[lyric_viz] deck: {len(deck)} image(s), "
                  f"{int(deck_idx[-1]) + 1} flips")
        if logo is not None:
            # deck owns the center — logo becomes a corner watermark
            lw = int(W * 0.09)
            lh = max(1, int(lw * logo.height / max(1, logo.width)))
            small = logo.resize((lw, lh))
            srgb = np.asarray(small.convert("RGB"), np.float32)
            sa = np.asarray(small.split()[-1], np.float32) / 255.0
            if sa.std() < 0.02:
                sa = np.clip((0.92 - srgb.min(axis=2) / 255.0) * 4.0, 0, 1)
            corner = (srgb, sa)

    # VJ loop background clips (pre-decoded at 1/4 res, capped 8s each)
    bgclips = None
    bgclip_idx = None
    if args.bgvideo:
        import av as _av
        p = Path(args.bgvideo)
        vpaths = (sorted([q for q in p.iterdir()
                          if q.suffix.lower() in (".mp4", ".mov", ".m4v",
                                                  ".webm", ".avi")])
                  if p.is_dir() else [Path(s) for s in args.bgvideo.split(",")])
        bgclips = []
        for q in vpaths:
            if not q.is_file():
                continue
            try:
                cont = _av.open(str(q))
                frames = []
                for fr in cont.decode(video=0):
                    frames.append(cv2.resize(
                        fr.to_ndarray(format="rgb24"),
                        (max(2, W // 4), max(2, H // 4)),
                        interpolation=cv2.INTER_AREA))
                    if len(frames) >= args.fps * 8:
                        break
                if len(frames) >= args.fps:      # at least 1s of loop
                    bgclips.append(np.stack(frames))
            except Exception as e:
                print(f"[lyric_viz] bgvideo {q.name} skipped: {e!r}")
        if bgclips:
            kicks = np.cumsum(analysis.kick.astype(int))
            bgclip_idx = (kicks // 16).astype(int)   # new loop every ~4 bars
            print(f"[lyric_viz] bg loops: {len(bgclips)} clip(s) loaded")
        else:
            bgclips = None

    r = Renderer(W, H, style, lines, args.fps, logo=logo, title=args.title,
                 wave_env=wave_env.astype(np.float32),
                 deck=deck, deck_idx=deck_idx, corner_logo=corner,
                 shape=args.shape, bgclips=bgclips, bgclip_idx=bgclip_idx)

    def frames():
        for i in range(n_frames):
            yield r.frame(i, i / args.fps, analysis)
            if i and i % (args.fps * 10) == 0:
                print(f"[lyric_viz] {i}/{n_frames} frames")

    encode(out, frames(), args.fps, pcm, sr, W, H)
    if style.strobe:
        print("[lyric_viz] WARNING: this style strobes on the drop - publish "
              "with a flash/photosensitivity warning in the caption.")
    print(f"[lyric_viz] done in {time.time() - t0:.1f}s -> {out} "
          f"({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
