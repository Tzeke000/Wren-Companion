"""lyric_viz — prototype v0 of the lyric/logo audio-visualizer.

THE PROMISE (Zeke, 2026-08-25 bedtime, Discord): an auto pipeline for his music
videos — song + written lyrics + logo in, finished video out. Word-synced
lyrics (karaoke-accurate, via Whisper word timestamps + alignment to the
WRITTEN lyrics), audio-reactive visuals (FFT energy drives glow/scale),
zero-spend. Style reference: Greyland Audio's Visual Lab Pro — but ours renders
finished videos and does word-sync, which theirs doesn't.

This v0 proves the whole chain end-to-end on the tower:
  audio → faster-whisper word timestamps → (optional) align to written lyrics
        → FFT energy envelope → PIL-rendered frames → PyAV h264+aac mp4.

No ffmpeg.exe on this machine — PyAV (bundled FFmpeg libs) does the encode and
the audio mux, so the only inputs are files and the only output is an .mp4.

Usage:
  .venv/Scripts/python.exe scripts/lyric_viz.py --audio song.wav
      [--lyrics lyrics.txt] [--logo logo.png] [--title "ARTIST"]
      [--out out.mp4] [--size 1280x720] [--fps 30] [--device cuda|cpu]

Prototype limits (v0, deliberate): single centered lyric line + active-word
highlight, one logo/title pulsing on bass, flat dark background. The Visual
Lab-style mode library comes later, on the server.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 1. Words with timestamps
# ---------------------------------------------------------------------------

@dataclass
class Word:
    start: float
    end: float
    text: str


def transcribe_words(audio: Path, device: str = "cuda") -> list[Word]:
    """faster-whisper word timestamps. GPU (~1.5GB, logged by gpu_load_log via
    stt_engine only in-runtime; this standalone load logs itself below)."""
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
    """Return LINES of written-lyric words with timing borrowed from whisper.

    The written lyrics are ground truth for TEXT (and line breaks); whisper is
    ground truth for TIME. difflib matches the two word sequences; unmatched
    written words get times interpolated between their matched neighbours.
    """
    lines_raw = [ln.strip() for ln in lyrics_text.splitlines() if ln.strip()]
    written: list[tuple[int, str]] = []          # (line_idx, word)
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
    # interpolate the gaps so every written word has a time
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
    matched = sum(1 for i in range(n) if i in set(known))
    print(f"[lyric_viz] aligned {matched}/{n} written words to audio "
          f"({len(out)} lines)")
    return [ln for ln in out if ln]


def lines_from_transcript(heard: list[Word], max_words: int = 6,
                          gap_s: float = 0.8) -> list[list[Word]]:
    """No written lyrics: group whisper words into display lines."""
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
# 2. Audio energy (the reactive part)
# ---------------------------------------------------------------------------

@dataclass
class Energy:
    rms: np.ndarray      # per video frame, 0..1
    bass: np.ndarray     # per video frame, 0..1


def energy_envelope(audio: Path, fps: int) -> tuple[Energy, np.ndarray, int]:
    import soundfile as sf
    y, sr = sf.read(str(audio), dtype="float32", always_2d=True)
    mono = y.mean(axis=1)
    hop = int(round(sr / fps))
    n_frames = max(1, int(np.ceil(len(mono) / hop)))
    rms = np.zeros(n_frames, dtype=np.float32)
    bass = np.zeros(n_frames, dtype=np.float32)
    win = 2048
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    bass_bins = freqs < 150.0
    for i in range(n_frames):
        seg = mono[i * hop:(i + 1) * hop]
        if len(seg):
            rms[i] = float(np.sqrt(np.mean(seg ** 2)))
        c = i * hop
        chunk = mono[max(0, c - win // 2):c + win // 2]
        if len(chunk) >= 256:
            spec = np.abs(np.fft.rfft(chunk, n=win))
            bass[i] = float(spec[bass_bins[:len(spec)]].sum())

    def norm_smooth(v: np.ndarray, decay: float = 0.85) -> np.ndarray:
        hi = np.percentile(v[v > 0], 95) if (v > 0).any() else 1.0
        v = np.clip(v / max(hi, 1e-9), 0.0, 1.0)
        out = np.zeros_like(v)
        prev = 0.0
        for i, x in enumerate(v):        # fast attack, slow decay
            prev = x if x > prev else prev * decay
            out[i] = prev
        return out

    return Energy(norm_smooth(rms), norm_smooth(bass, decay=0.75)), y, sr


# ---------------------------------------------------------------------------
# 3. Frame rendering
# ---------------------------------------------------------------------------

ACCENT = (120, 200, 255)      # active-word / glow colour (iris blue)
DIM = (150, 150, 160)         # inactive words
BG = (8, 8, 12)


def _font(size: int):
    from PIL import ImageFont
    for name in ("impact.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(rf"C:\Windows\Fonts\{name}", size)
        except Exception:
            continue
    return ImageFont.load_default()


@dataclass
class Renderer:
    W: int
    H: int
    lines: list[list[Word]]
    logo: "object | None" = None          # PIL.Image or None
    title: str = ""
    _fonts: dict = field(default_factory=dict)

    def font(self, size: int):
        if size not in self._fonts:
            self._fonts[size] = _font(size)
        return self._fonts[size]

    def current_line(self, t: float) -> tuple[list[Word] | None, int]:
        LEAD = 0.25
        for ln in self.lines:
            if ln[0].start - LEAD <= t <= ln[-1].end + 0.6:
                active = -1
                for i, w in enumerate(ln):
                    if w.start <= t:
                        active = i
                return ln, active
        return None, -1

    def frame(self, t: float, rms: float, bass: float) -> np.ndarray:
        from PIL import Image, ImageDraw, ImageFilter
        img = Image.new("RGB", (self.W, self.H), BG)
        draw = ImageDraw.Draw(img)

        # ---- logo / title, bass-pulsed ---------------------------------
        cy_logo = int(self.H * 0.30)
        if self.logo is not None:
            scale = 1.0 + 0.12 * bass
            lw = int(self.W * 0.28 * scale)
            lh = int(lw * self.logo.height / max(1, self.logo.width))
            logo = self.logo.resize((lw, lh))
            img.paste(logo, ((self.W - lw) // 2, cy_logo - lh // 2),
                      logo if logo.mode == "RGBA" else None)
        elif self.title:
            fs = int(self.H * 0.10 * (1.0 + 0.10 * bass))
            f = self.font(fs)
            tw = draw.textlength(self.title, font=f)
            col = tuple(int(90 + 120 * bass) for _ in range(3))
            draw.text(((self.W - tw) // 2, cy_logo - fs // 2), self.title,
                      font=f, fill=col)

        # ---- lyric line, active word accented --------------------------
        ln, active = self.current_line(t)
        if ln:
            fs = int(self.H * 0.075)
            f = self.font(fs)
            words = [w.text for w in ln]
            space = draw.textlength(" ", font=f)
            widths = [draw.textlength(w, font=f) for w in words]
            total = sum(widths) + space * (len(words) - 1)
            # shrink to fit
            while total > self.W * 0.92 and fs > 18:
                fs = int(fs * 0.9)
                f = self.font(fs)
                space = draw.textlength(" ", font=f)
                widths = [draw.textlength(w, font=f) for w in words]
                total = sum(widths) + space * (len(words) - 1)
            x = (self.W - total) / 2
            y = int(self.H * 0.68)
            # glow layer: active word only, blurred, energy-scaled
            glow = Image.new("RGB", (self.W, self.H), (0, 0, 0))
            gdraw = ImageDraw.Draw(glow)
            xx = x
            for i, (w, wd) in enumerate(zip(words, widths)):
                if i == active:
                    gdraw.text((xx, y), w, font=f, fill=ACCENT)
                xx += wd + space
            glow = glow.filter(ImageFilter.GaussianBlur(6 + 10 * rms))
            img = Image.blend(img, Image.blend(img, glow, 0.9),
                              min(1.0, 0.35 + 0.65 * rms))
            draw = ImageDraw.Draw(img)
            xx = x
            for i, (w, wd) in enumerate(zip(words, widths)):
                draw.text((xx, y), w, font=f,
                          fill=ACCENT if i == active else DIM)
                xx += wd + space

        # ---- baseline energy bar (subtle) ------------------------------
        bar_w = int(self.W * 0.6 * rms)
        draw.rectangle([(self.W - bar_w) // 2, self.H - 8,
                        (self.W + bar_w) // 2, self.H - 5],
                       fill=(40 + int(120 * rms),) * 3)
        return np.asarray(img)


# ---------------------------------------------------------------------------
# 4. Encode (PyAV — no external ffmpeg.exe needed)
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

    # audio: s16 → resampler feeds whatever aac wants
    pcm16 = (np.clip(audio_pcm, -1.0, 1.0) * 32767).astype(np.int16)
    channels = pcm16.shape[1]
    layout = "stereo" if channels == 2 else "mono"
    resampler = av.AudioResampler(format="fltp", layout=layout, rate=sr)
    CH = 4096
    pts = 0
    for i in range(0, len(pcm16), CH):
        chunk = pcm16[i:i + CH]
        # packed s16 wants shape (1, samples*channels), interleaved — which is
        # exactly soundfile's row-major (n, ch) flattened.
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
    ap.add_argument("--lyrics", type=Path, default=None,
                    help="written lyrics (text is ground truth; whisper only times it)")
    ap.add_argument("--logo", type=Path, default=None)
    ap.add_argument("--title", default="", help="text logo fallback")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    args = ap.parse_args()

    W, H = (int(v) for v in args.size.lower().split("x"))
    out = args.out or args.audio.with_suffix(".viz.mp4")

    t0 = time.time()
    heard = transcribe_words(args.audio, args.device)
    if args.lyrics and args.lyrics.is_file():
        lines = align_to_lyrics(heard, args.lyrics.read_text(encoding="utf-8"))
    else:
        lines = lines_from_transcript(heard)

    energy, pcm, sr = energy_envelope(args.audio, args.fps)
    duration = len(pcm) / sr
    n_frames = int(np.ceil(duration * args.fps))

    logo = None
    if args.logo and args.logo.is_file():
        from PIL import Image
        logo = Image.open(args.logo).convert("RGBA")

    r = Renderer(W, H, lines, logo=logo, title=args.title)

    def frames():
        for i in range(n_frames):
            t = i / args.fps
            e = min(i, len(energy.rms) - 1)
            yield r.frame(t, float(energy.rms[e]), float(energy.bass[e]))
            if i and i % (args.fps * 10) == 0:
                print(f"[lyric_viz] {i}/{n_frames} frames "
                      f"({i / args.fps:.0f}s/{duration:.0f}s)")

    encode(out, frames(), args.fps, pcm, sr, W, H)
    print(f"[lyric_viz] done in {time.time() - t0:.1f}s -> {out} "
          f"({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
