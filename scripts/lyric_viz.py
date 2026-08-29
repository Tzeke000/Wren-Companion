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
import json
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
            with logged_load(f"whisper:large-v3-turbo:{dev}:{compute}:lyric_viz"):
                model = WhisperModel("large-v3-turbo", device=dev,
                                     compute_type=compute)
            break
        except Exception as e:
            print(f"[lyric_viz] whisper on {dev} failed ({e!r}), trying next")
    if model is None:
        raise RuntimeError("no whisper backend loaded")
    # MUSIC settings (2026-08-26, the Chipped White Car sync bug): VAD is
    # speech-tuned and EATS sung vocals — distil+VAD heard 38 of 235 words;
    # large-v3-turbo with VAD off heard 233. Never VAD a song.
    segments, _info = model.transcribe(str(audio), word_timestamps=True,
                                       vad_filter=False, beam_size=5,
                                       condition_on_previous_text=False)
    words: list[Word] = []
    segs: list[tuple[float, float, list[Word]]] = []
    for seg in segments:
        sw = []
        for w in (seg.words or []):
            t = w.word.strip()
            if t:
                wd = Word(float(w.start), float(w.end), t)
                words.append(wd)
                sw.append(wd)
        if sw:
            segs.append((sw[0].start, sw[-1].end, sw))
    print(f"[lyric_viz] transcribed {len(words)} words / {len(segs)} segments")
    transcribe_words.last_segments = segs
    return words


_NORM_RE = re.compile(r"[^a-z0-9']+")


def _norm(s: str) -> str:
    return _NORM_RE.sub("", s.lower())


def align_to_lyrics(heard: list[Word], lyrics_text: str,
                    segments: "list | None" = None) -> list[list[Word]]:
    """Written lyrics = ground truth TEXT (and line breaks); whisper = TIME.

    v2 (2026-08-26, after Chipped White Car drifted): when whisper SEGMENTS
    are available, align at LINE level with a monotonic DP and gap penalties —
    repeated chorus lines ("(Late night lover)" x8) made the old global word
    matcher pin text to the WRONG chorus instance and drift whole sections.
    Line-level context + ordered path with skip costs kills that class.

    Section tags like [Verse] / [Hook] / [Chorus 2] are structure, not lyrics —
    dropped before alignment (Zeke's lyric PDFs use them)."""
    if segments:
        out = _align_lines_dp(segments, lyrics_text)
        if out is not None:
            return out
    # fallback: the v1 global word matcher
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


def _align_lines_dp(segments: list, lyrics_text: str) -> "list[list[Word]] | None":
    """Monotonic line↔segment alignment with gap penalties.

    Each written LINE matches at most one whisper segment, in order. Skipping
    a line (not sung / mumbled) or a segment (adlib, instrumental vocalizing)
    is allowed but costs — so mapping chorus-1 text onto chorus-2 audio now
    requires paying for a whole skipped chorus of segments, which the path
    won't do when the true instance is available."""
    lines_raw = [ln.strip() for ln in lyrics_text.splitlines()
                 if ln.strip() and not re.fullmatch(r"\[[^\]]+\]", ln.strip())]
    if not lines_raw or not segments:
        return None
    line_norm = [" ".join(_norm(w) for w in ln.split()) for ln in lines_raw]
    seg_norm = [" ".join(_norm(w.text) for w in sw) for _, _, sw in segments]
    L, S = len(lines_raw), len(segments)
    sim = np.zeros((L, S), np.float32)
    for i in range(L):
        for j in range(S):
            sim[i, j] = difflib.SequenceMatcher(
                a=line_norm[i], b=seg_norm[j], autojunk=False).ratio()
    SKIP_LINE, SKIP_SEG, MIN_SIM = -0.25, -0.06, 0.40
    # dp[i][j]: best score using lines[:i], segments[:j]
    dp = np.full((L + 1, S + 1), -1e9, np.float32)
    back = np.zeros((L + 1, S + 1), np.int8)     # 1=match 2=skip line 3=skip seg
    dp[0, :] = np.arange(S + 1) * SKIP_SEG
    dp[:, 0] = np.arange(L + 1) * SKIP_LINE
    for i in range(1, L + 1):
        for j in range(1, S + 1):
            m = dp[i - 1, j - 1] + (sim[i - 1, j - 1]
                                    if sim[i - 1, j - 1] >= MIN_SIM else -0.5)
            sl = dp[i - 1, j] + SKIP_LINE
            ss = dp[i, j - 1] + SKIP_SEG
            best = max(m, sl, ss)
            dp[i, j] = best
            back[i, j] = 1 if best == m else (2 if best == sl else 3)
    # walk back
    pairs: dict[int, int] = {}
    i, j = L, S
    while i > 0 and j > 0:
        b = back[i, j]
        if b == 1:
            if sim[i - 1, j - 1] >= MIN_SIM:
                pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif b == 2:
            i -= 1
        else:
            j -= 1
    if len(pairs) < max(2, L // 6):
        print(f"[lyric_viz] line-DP matched only {len(pairs)}/{L} lines — "
              f"falling back to global matcher")
        return None
    out: list[list[Word]] = []
    matched_words = 0
    for li, ln in enumerate(lines_raw):
        wtexts = ln.split()
        if li in pairs:
            s0, s1, sw = segments[pairs[li]]
            # word-level match inside the segment for precise times
            smw = difflib.SequenceMatcher(
                a=[_norm(w) for w in wtexts],
                b=[_norm(w.text) for w in sw], autojunk=False)
            starts: list[float | None] = [None] * len(wtexts)
            ends: list[float | None] = [None] * len(wtexts)
            for blk in smw.get_matching_blocks():
                for k in range(blk.size):
                    starts[blk.a + k] = sw[blk.b + k].start
                    ends[blk.a + k] = sw[blk.b + k].end
                    matched_words += 1
            # fill gaps linearly across the segment span
            span = max(0.2, s1 - s0)
            for k in range(len(wtexts)):
                if starts[k] is None:
                    frac = k / max(1, len(wtexts))
                    starts[k] = s0 + frac * span
                    ends[k] = min(s1, starts[k] + span / max(1, len(wtexts)))
            out.append([Word(float(starts[k]), float(ends[k]), wtexts[k])
                        for k in range(len(wtexts))])
        # unmatched lines are DROPPED from display (not sung ≠ on screen —
        # showing them at guessed times is exactly the drift Zeke saw)
    print(f"[lyric_viz] line-DP aligned {len(pairs)}/{L} lines "
          f"({matched_words} word anchors); unsung lines dropped")
    return out if out else None


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
    beat: np.ndarray       # bool — TRUE beat grid (librosa tracked, not kicks)
    beat_i: np.ndarray     # int — cumulative beat number at each frame
    beat_ph: np.ndarray    # 0..1 — phase within the current beat (0 = on it)


NBARS = 40

# every centrepiece visualization Renderer._viz can dispatch to. Single source
# of truth so --viz validation can't drift from what the renderer supports.
VIZ_MODES = ("radial", "bars_center", "bars", "wave", "tunnel", "supernova",
             "kaleido", "ncs_ring", "tn_blob", "mcat_bars")


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


def _beat_grid(mono: np.ndarray, sr: int, fps: int, n: int,
               kick: np.ndarray, bpm_fallback: float
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """A REAL beat grid, not a kick-onset proxy (Zeke 2026-08-28: "get the BPM
    ... switch every 8 beats").

    Why not reuse `kick`: low-band onsets are the *pulse we can see*, not the
    *beat we can count*. Kicks double up on fills, vanish in breakdowns, and
    fire on 808 slides — so "every 8 kicks" drifts off the bar within a verse
    and the swap stops landing musically. librosa's beat tracker fits a global
    tempo with a dynamic-programming phase, so beat 800 is still on the grid.

    Returns (beat_bool, beat_number_per_frame, phase_0_1, bpm). Falls back to a
    synthetic grid at `bpm_fallback` phase-locked to the first kick when librosa
    is unavailable or unsure — always returns a usable grid, never raises."""
    beat_f: np.ndarray | None = None
    bpm = float(bpm_fallback or 0.0)
    try:
        import librosa
        tempo, frames = librosa.beat.beat_track(y=mono, sr=sr, units="time")
        t = float(np.atleast_1d(tempo)[0])
        if len(frames) >= 4 and 50.0 <= t <= 200.0:
            beat_f = np.round(np.asarray(frames) * fps).astype(int)
            bpm = round(t, 1)
    except Exception as ex:
        print(f"[lyric_viz] librosa beat track unavailable ({ex!r}) — "
              f"synthesising grid from bpm~{bpm_fallback or 120}")
    if beat_f is None:
        # synthetic: uniform grid at the estimated tempo, phase-locked to the
        # first detected kick so downbeat-ish alignment survives the fallback.
        per = fps * 60.0 / max(60.0, bpm or 120.0)
        first = int(np.argmax(kick)) if kick.any() else 0
        beat_f = np.round(np.arange(first % per, n, per)).astype(int)
        bpm = bpm or 120.0
    beat_f = np.unique(beat_f[(beat_f >= 0) & (beat_f < n)])
    beat = np.zeros(n, dtype=bool)
    beat[beat_f] = True
    # beat_i: how many beats have elapsed. cumsum-1 so the frame ON beat 0 is
    # index 0 (a -1 would make the pre-roll swap a shape for a few frames).
    beat_i = np.maximum(0, np.cumsum(beat.astype(int)) - 1)
    # phase: 0 on the beat, ->1 just before the next one. Used for the nod.
    ph = np.ones(n, np.float32)
    if len(beat_f):
        nxt = np.append(beat_f[1:], n)
        for b, e in zip(beat_f, nxt):
            if e > b:
                ph[b:e] = np.linspace(0.0, 1.0, e - b, endpoint=False)
    return beat, beat_i.astype(int), ph, bpm


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


def pick_hook_window(a: Analysis, lines: list, duration: float, fps: int,
                     target_s: float = 27.0) -> tuple[float, float]:
    """Find the strongest ≤30s window (docs/tiktok_playbook.md): TikTok wants
    the interesting point IMMEDIATELY — completion rate is the ranking king,
    so the clip must open ~1s before the song's best moment.

    Score per candidate start = drop coverage + vocal density + energy."""
    n = len(a.rms)
    word_times = [w.start for ln in lines for w in ln]
    best_t, best_score = 0.0, -1.0
    for t0 in np.arange(0.0, max(1.0, duration - target_s), 1.0):
        i0, i1 = int(t0 * fps), int(min(n, (t0 + target_s) * fps))
        if i1 <= i0:
            break
        drop_cov = float(a.drop[i0:i1].mean())
        energy = float(a.rms[i0:i1].mean())
        words = sum(1 for t in word_times if t0 <= t < t0 + target_s)
        vocal = min(1.0, words / (target_s * 1.5))
        # hook lands EARLY bonus: drop/kick within the first 3s of the window
        early = float(a.drop[i0:i0 + 3 * fps].any() or
                      a.kick[i0:i0 + 3 * fps].any())
        score = 1.2 * drop_cov + 0.9 * vocal + 0.8 * energy + 0.5 * early
        if score > best_score:
            best_score, best_t = score, float(t0)
    # SNAP TO A BEAT (research 2026-08-28). This used to subtract a flat 1.0s
    # of pre-roll, which (a) spends the single most decisive second of a TikTok
    # on dead air and (b) lands the cut mid-bar, so the clip opens on an
    # offbeat. Back off by ~half a second, then snap to the nearest beat in the
    # grid we already computed — the cut then lands ON the pulse, and a
    # beat-aligned in/out point is also what makes a clip loop cleanly, which
    # matters because replays feed watch time.
    t0 = max(0.0, best_t - 0.5)
    beats = np.flatnonzero(a.beat) / float(fps) if a.beat.any() else None
    if beats is not None and len(beats):
        t0 = float(beats[int(np.argmin(np.abs(beats - t0)))])
    t1 = min(duration, t0 + target_s + 1.0)
    if beats is not None and len(beats):
        # end on a beat too, so first and last frame sit at the same phase
        cand = beats[(beats > t0 + target_s * 0.6) & (beats <= t1)]
        if len(cand):
            t1 = float(cand[-1])
    return t0, t1


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

    beat, beat_i, beat_ph, bpm = _beat_grid(mono, sr, fps, n, kick, bpm)

    print(f"[lyric_viz] analysis: bpm~{bpm or '?'} beats={int(beat.sum())} "
          f"kicks={int(kick.sum())} snares={int(snare.sum())} "
          f"drop_frames={int(drop.sum())} ({drop.mean() * 100:.0f}% of track)")
    return (Analysis(rms=np.clip(_ema(_norm95(rms), 0.08, fps), 0, 1),
                     bass=np.clip(_ema(bass_n, 0.08, fps), 0, 1),
                     high=np.clip(_ema(_norm95(high), 0.06, fps), 0, 1),
                     kick=kick, snare=snare, drop=drop, build=build,
                     bars=bars, bpm=bpm,
                     beat=beat, beat_i=beat_i, beat_ph=beat_ph),
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
    # future bass (Zeke's correction 08-26: "She broke it all" is future bass,
    # NOT dubstep — lyric-heavy + melodic = this lane; dubstep barely has
    # words). Bright pastel neon, bouncy zoom on chord stabs, soft glow,
    # NO scramble/glitch violence — Illenium/Flume language.
    "futurebass": Style("futurebass",
                        palette=[(255, 120, 200), (120, 220, 255),
                                 (190, 140, 255), (255, 235, 160)],
                        bg="plasma", viz="radial", particles=True,
                        font_title="Poppins-Bold.ttf",
                        font_lyrics="Poppins-Bold.ttf", caps=True,
                        tracking=0.10, logo_pos="center",
                        strobe=False, glitch=False, rgb_split=False,
                        shake=0.0, zoom_punch=0.05, scramble=False,
                        breakdown_desat=False, flicker=0.0,
                        text_col=(250, 245, 250)),
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
    _center_pocket: np.ndarray | None = None  # darkening behind centerpiece
    corner_logo: "tuple | None" = None      # (rgb, mask) small watermark
    shape: str = "none"                     # cube|pyramid|cylinder|orb|logo3d|none
                                            # — or a COMMA LIST to rotate through
    shape_every: int = 8                    # swap to the next shape every N beats
    nod: bool = False                       # bob/pitch the shape on each beat
    viz_deck: "list | None" = None          # rotate the CENTREPIECE viz on the beat
    viz_every: int = 16                     # ...every N beats (4 bars in 4/4)
    readable: bool = False                  # lyrics-first: fewer FX + contrast scrim
    bloom: float = 0.45                     # whole-frame glow amount (0 = off)
    layout: str = "row"                     # multi-object arrangement: row|nested
    gpu3d: str = "off"                      # off|wire|shaded|solid_wire (GPU path)
    _gpu_faces: dict = field(default_factory=dict)
    safe_margins: bool = False              # keep clear of TikTok's UI overlays
    safe_overlay: bool = False              # draw the safe box to verify on a phone
    _viz_last: int = -1                     # last viz slot, for transition flash
    _viz_flash: float = 0.0                 # decaying flash that hides the cut
    _rot: float = 0.0                       # accumulated 3D rotation
    _shapes: "list | None" = None           # parsed shape list (set in __post_init__)
    _shape_last: int = -1                   # last slot index, for swap logging

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
        # shape deck: "model:skull_kay,model:skull_quaternius,orb,cube" rotates
        # every `shape_every` beats. A single value = old behaviour, no swaps.
        self._shapes = [s.strip() for s in str(self.shape).split(",")
                        if s.strip() and s.strip() != "none"]
        # -- FBM flow background (bg="flow") --------------------------------
        # Octave lattices for value noise, built ONCE. Each is made PERIODIC in
        # x (last column == first) so scrolling can wrap without the straight
        # vertical SEAM that a plain np.roll of a random lattice produces.
        self._fbm_oct = []
        for k, (w, h) in enumerate(((4, 3), (8, 5), (16, 10), (32, 20))):
            g = np.random.default_rng(101 + k).random((h, w + 1)).astype(np.float32)
            g[:, -1] = g[:, 0]
            self._fbm_oct.append(g)
        self._fbm_lw, self._fbm_lh = 160, 90
        self._fbm_grid = np.mgrid[0:self._fbm_lh,
                                  0:self._fbm_lw].astype(np.float32)
        # 3-stop gradient LUT from the style palette — the background then
        # matches the rest of the frame BY CONSTRUCTION, same trick as duotone()
        lut = np.zeros((256, 1, 3), np.float32)
        pal = self.style.palette
        stops = (np.array(pal[0], np.float32) * 0.30,
                 np.array(pal[1 % len(pal)], np.float32) * 0.22,
                 np.array(pal[2 % len(pal)], np.float32) * 0.34)
        for k, c in enumerate(stops):
            lut[int(k * 85):int((k + 1) * 85) + 1] = c
        self._fbm_lut = cv2.GaussianBlur(lut, (1, 161), 0)

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
            # phrase boundaries via bgclip_idx. Per-clip normalization keeps
            # bright loops from washing out the centerpiece (caught by eye
            # 08-26: an orbital clip at full gain drowned the helmet).
            ci = (int(self.bgclip_idx[i]) % len(self.bgclips)
                  if self.bgclip_idx is not None else 0)
            clip = self.bgclips[ci]
            frame = clip[i % len(clip)].astype(np.float32)
            norm = 70.0 / max(25.0, float(frame.mean()))
            up = cv2.resize(frame, (self.W, self.H),
                            interpolation=cv2.INTER_LINEAR)
            gain = min(0.95, (0.30 + 0.55 * a.rms[i]
                              + (0.20 if a.kick[i] else 0.0)) * norm)
            up *= gain
            # darken a pocket behind the centerpiece so it always pops
            if self._center_pocket is None:
                cx, cy = self._logo_center()
                Y, X = np.ogrid[0:self.H, 0:self.W]
                d2 = ((X - cx) / (self.H * 0.36)) ** 2 + \
                     ((Y - cy) / (self.H * 0.36)) ** 2
                self._center_pocket = (1.0 - 0.45 * np.exp(-d2)) \
                    .astype(np.float32)[..., None]
            img += up * self._center_pocket
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
        elif self.style.bg == "flow":
            img += self._bg_flow(t, a, i)
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

    def _fbm(self, t: float, seed_shift: float = 0.0) -> np.ndarray:
        """4 octaves of value noise at 160x90, scrolled in x over time.

        The lattices are PERIODIC in x (built in __post_init__), and we scroll
        by a FLOAT offset through the cubic upsample rather than np.roll-ing
        integer columns — an integer roll of a non-periodic random lattice puts
        a straight vertical SEAM through the frame, which is glaringly visible
        on a smooth gradient (the prototype had exactly that)."""
        LW, LH = self._fbm_lw, self._fbm_lh
        acc = np.zeros((LH, LW), np.float32)
        amp = 0.5
        base_y = np.arange(LH, dtype=np.float32)[:, None]
        base_x = np.arange(LW, dtype=np.float32)[None, :]
        for k, g in enumerate(self._fbm_oct):
            h, w = g.shape[0], g.shape[1] - 1
            off = (t * (0.6 + 0.35 * k) + seed_shift) % w
            # ONE remap does the scroll AND the upsample: sample the periodic
            # lattice at fractional coords. Cubic gives the smooth field.
            mx = np.ascontiguousarray(
                ((base_x * (w / LW) + off) % w) + np.zeros((LH, 1), np.float32))
            my = np.ascontiguousarray(
                (base_y * ((h - 1) / max(1, LH - 1))) + np.zeros((1, LW), np.float32))
            acc += amp * cv2.remap(g, mx, my, cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_REFLECT)
            amp *= 0.5
        return acc

    def _bg_flow(self, t: float, a: Analysis, i: int) -> np.ndarray:
        """Domain-warped FBM gradient — the 'not flat black' background
        (Zeke 2026-08-28: "make the background change from just being steady
        black"). Everything is computed at 160x90 and cubic-upsampled, so it is
        smooth by construction and the low-res work is invisible.

        The domain warp (one fbm offsetting the lookup of another) is what
        turns 'clouds' into 'flowing liquid', and it costs nothing at this
        size. Colour comes from a LUT built off the style palette, so the
        background matches the rest of the frame by construction."""
        n = self._fbm(t * 0.5)
        n2 = self._fbm(t * 0.35, seed_shift=40.0)
        yy, xx = self._fbm_grid
        mx = np.clip(xx + (n2 - 0.5) * 26.0, 0, self._fbm_lw - 1)
        my = np.clip(yy + (n - 0.5) * 26.0, 0, self._fbm_lh - 1)
        wv = cv2.remap(n, mx, my, cv2.INTER_LINEAR)
        lo, hi = float(wv.min()), float(wv.max())
        wv = (wv - lo) / max(1e-6, hi - lo)
        small = self._fbm_lut[(wv * 255).astype(np.uint8), 0]
        big = cv2.resize(small, (self.W, self.H), interpolation=cv2.INTER_CUBIC)
        return big * (0.40 + 0.60 * float(a.rms[i]))

    def _bloom(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Quarter-res two-octave bloom over the WHOLE frame.

        Every reference channel (Trap Nation, NCS, Monstercat, UKF) is built on
        glow; before this only the lyrics glowed and the ring/bars/wireframes
        were hard-edged, which was the main thing separating this from the
        reference look. Full-res would be ~229ms; quarter-res is ~6ms and looks
        identical because bloom is by definition low-frequency.

        Threshold is fixed and the AMOUNT is what tracks energy — modulating
        the threshold instead makes the whole frame haze over.

        ⚠ HEADROOM WEIGHTING is load-bearing here, and it is a departure from
        the textbook recipe. Textbook bloom assumes the source is not already
        glowing; this renderer additively glows the wireframe, bars and lyrics
        *before* this runs, so plain additive bloom drove the skull from 2%
        blown-white pixels to 20% — a solid white blob with the mesh gone
        (measured, and visible in .tmp/v4.jpg). Scaling the bloom by the
        remaining headroom (1 - img/255) confines it to a HALO around bright
        things instead of filling their interiors: same frames went 2.2%->3.9%
        and 0.4%->0.6% while keeping the glow."""
        qh, qw = max(1, self.H // 4), max(1, self.W // 4)
        q = cv2.resize(img, (qw, qh), interpolation=cv2.INTER_AREA)
        cv2.threshold(q, 190, 0, cv2.THRESH_TOZERO, dst=q)
        # two octaves: a tight core plus a wide halo reads markedly better than
        # one blur, and both are cheap at this size. The core is weighted down
        # because at full weight it re-adds a near-copy of the bright pass and
        # fills the interior back in.
        bright = q.copy()
        near = cv2.GaussianBlur(q, (0, 0), 3)
        cv2.GaussianBlur(q, (0, 0), 10, dst=q)
        cv2.addWeighted(near, 0.5, q, 1.0, 0.0, dst=q)
        # HALO ONLY: subtract the unblurred bright pass. Without this the blur
        # still carries the source's own energy, so bloom fills the INTERIOR of
        # a wireframe skull and it reads as a milky blob with the mesh gone
        # (confirmed by eye in .tmp/bloomcmp.jpg — the headroom clamp alone
        # fixed the arithmetic but NOT the look). Subtracting the core leaves
        # only what spilled outside the source, which is what "glow" means.
        cv2.subtract(q, bright, dst=q)
        np.maximum(q, 0, out=q)
        big = cv2.resize(q, (self.W, self.H), interpolation=cv2.INTER_LINEAR)
        big *= self.bloom * (0.55 + 0.65 * float(a.rms[i]))
        # CLAMP TO THE REMAINING HEADROOM, exactly. A (1 - img/255) weight is
        # not enough: at bloom time `img` is unclipped float and routinely sits
        # at 200-250, where a tiny weight still multiplies a large bloom value
        # over the top (measured 13.7% blown-white with the weighted form vs a
        # 3.9% offline prediction — the offline proxy used ALREADY-CLIPPED
        # frames and so did not reproduce the live float path). Taking the
        # elementwise min against the true headroom makes it impossible for
        # bloom to create a single new saturated pixel.
        np.minimum(big, np.maximum(255.0 - img, 0.0), out=big)
        img += big

    def _viz_now(self, i: int, a: Analysis) -> str:
        """Which centrepiece visualization is drawn at frame i.

        Zeke 2026-08-28: *"change different audio visualizations instead of
        just the one that's across and straight like every 16 beats so it keeps
        it interesting for the viewer."*

        ⚠ Only the DRAWN viz rotates. `style.viz` still drives LAYOUT — lyric
        font size, lyric baseline, logo position — and those must not jump
        mid-song, so they stay pinned to the style (or to the first deck entry).
        Rotating layout too would make the text hop around every 4 bars."""
        if not self.viz_deck:
            return self.style.viz
        if len(self.viz_deck) == 1 or self.viz_every <= 0:
            return self.viz_deck[0]
        slot = int(a.beat_i[i]) // int(self.viz_every)
        if slot != self._viz_last:
            # TRANSITION: the deck used to HARD-CUT between modes, which reads
            # as a glitch rather than as a choice. A flash on the switch frame
            # that decays over ~5 frames hides the discontinuity completely —
            # the cheapest transition there is and the most effective.
            if self._viz_last >= 0:
                self._viz_flash = 1.0
            self._viz_last = slot
        return self.viz_deck[slot % len(self.viz_deck)]

    def _viz(self, img: np.ndarray, i: int, a: Analysis) -> None:
        v = self._viz_now(i, a)
        if v == "radial":
            self._viz_radial(img, i, a)
        elif v == "bars_center":
            self._viz_bars_center(img, i, a)
        elif v == "wave":
            self._viz_wave(img, i, a)
        elif v == "tunnel":
            self._viz_tunnel(img, i, a)
        elif v == "supernova":
            self._viz_supernova(img, i, a)
        elif v == "kaleido":
            self._viz_kaleido(img, i, a)
        elif v == "ncs_ring":
            self._viz_ncs_ring(img, i, a)
        elif v == "tn_blob":
            self._viz_tn_blob(img, i, a)
        elif v == "mcat_bars":
            self._viz_mcat_bars(img, i, a)
        else:
            self._viz_bars_bottom(img, i, a)
        if self.style.particles:
            self._viz_particles(img, i, a)

    # -- ARCHETYPES copied from the real channels (research 2026-08-28,
    #    docs/edm_visualizer_techniques_2026-08-28.md). A second agent pulled
    #    real in-video frames from 7 channels via YouTube storyboards rather
    #    than trusting tutorials, so these are what actually ships, not what
    #    blog posts say ships. ----------------------------------------------

    def _viz_ncs_ring(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """NCS's ring — the format they have shipped essentially unchanged for
        a decade. Two things at once, and it is the combination that reads:

        1. the ring's RADIUS is modulated at LOW ORDER (5-7 broad lobes), so
           bass deforms the circle into a slow wobble rather than into spikes;
        2. fine NEEDLES point INWARD from the ring toward the centre, dense on
           high-frequency content and vanishing entirely in quiet passages —
           verified clean perfect circles at breakdowns.

        Monochrome by construction: ONE flat accent, no gradient across the
        ring, ever. That restraint is most of why it looks expensive."""
        cx, cy = self.W // 2, int(self.H * 0.42)
        R = self.H * 0.24
        col = self._pal()
        n = 240
        th = np.linspace(0, 2 * np.pi, n, endpoint=False)
        # low-order radius wobble: 5 and 7 lobes, phase-drifting so it breathes
        ph = i * 0.013
        wob = (np.sin(th * 5 + ph * 1.7) * 0.55 + np.sin(th * 7 - ph * 1.1) * 0.45)
        rr = R * (1.0 + (0.05 + 0.16 * float(a.bass[i])) * wob
                  + 0.05 * float(a.rms[i]))
        pts = np.stack([cx + rr * np.cos(th), cy + rr * np.sin(th)], 1)
        layer = np.zeros_like(img)
        cv2.polylines(layer, [pts.astype(np.int32).reshape(-1, 1, 2)], True,
                      tuple(float(x) for x in col), 3, cv2.LINE_AA)
        # inward needles — length rides the spectrum, density rides the highs
        hi = float(a.high[i])
        if hi > 0.06:
            step = max(1, int(6 - 4 * hi))
            for k in range(0, n, step):
                mag = float(a.bars[i, (k * NBARS) // n])
                ln = rr[k] * (0.10 + 0.55 * mag) * (0.35 + 0.9 * hi)
                x0, y0 = pts[k]
                x1 = cx + (rr[k] - ln) * np.cos(th[k])
                y1 = cy + (rr[k] - ln) * np.sin(th[k])
                cv2.line(layer, (int(x0), int(y0)), (int(x1), int(y1)),
                         tuple(float(x) for x in col * 0.85), 1, cv2.LINE_AA)
        img += layer + cv2.GaussianBlur(layer, (0, 0), 6) * (0.6 + 0.9 * a.rms[i])

    def _viz_tn_blob(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Trap Nation / Bass Nation — a filled radial BLOB hugging a centre
        disc, not discrete bars. Two details make it theirs:

        * the spectrum is smoothed hard along the angular axis so neighbouring
          bins MERGE into liquid lobes and pointed 'ears'. Their AE template
          reaches this by using 2000 frequency bands at thickness 3 — far more
          bands than the FFT has real bins — so bars stop being bars. Cheaper
          here: few bins, heavy circular smoothing.
        * mirrored left/right about a shared centroid, so it is symmetric.

        The signature RGB split (offset scaling with bass) is applied by _fx's
        rgb_split on the drop, so it is deliberately not duplicated here."""
        cx, cy = self.W // 2, int(self.H * 0.42)
        R0 = self.H * 0.105
        n = 360
        half = n // 2
        # build HALF the spectrum then mirror — symmetry for free, and it is
        # how the reference does it
        idx = (np.arange(half) * NBARS) // half
        mag = a.bars[i, idx].astype(np.float32)
        # circular smoothing = the liquid merge
        k = np.hanning(21).astype(np.float32); k /= k.sum()
        mag = np.convolve(np.concatenate([mag[-20:], mag, mag[:20]]), k,
                          "same")[20:-20]
        mag = np.concatenate([mag, mag[::-1]])
        th = np.linspace(-np.pi / 2, 1.5 * np.pi, n, endpoint=False)
        # extent is a MODEST bump off the disc. v1 used H*(0.05+0.30*mag) with
        # an rms multiplier, which at a loud moment put the rim ~300px past the
        # disc on a 768-tall frame — the lobes left the screen and the whole
        # thing read as one white ball with a haze (verified by eye).
        rr = R0 * (1.0 + (0.14 + 1.35 * mag) * (0.55 + 0.55 * float(a.rms[i])))
        outer = np.stack([cx + rr * np.cos(th), cy + rr * np.sin(th)], 1)
        layer = np.zeros_like(img)
        col = self._pal()
        # FILLED, not stroked — the blob is a solid shape
        cv2.fillPoly(layer, [outer.astype(np.int32).reshape(-1, 1, 2)],
                     tuple(float(x) for x in col * 0.42))
        # the disc is bright but NOT pure white: at 255 it clips, and the bloom
        # pass then has no headroom to halo into, so it flattens to a blob
        cv2.circle(layer, (cx, cy), int(R0 * 0.94), (170.0, 170.0, 176.0), -1)
        cv2.circle(layer, (cx, cy), int(R0 * 0.94),
                   tuple(float(x) for x in col), 2, cv2.LINE_AA)
        img += layer + cv2.GaussianBlur(layer, (0, 0), 7) * (0.28 + 0.42 * a.rms[i])

    def _viz_mcat_bars(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Monstercat's CLASSIC format — the only one of these archetypes with
        published hard numbers (from a Rainmeter skin that replicates it):
        63 log-spaced bands 20Hz-16kHz, flat-topped rectangles on a SINGLE
        baseline rising upward only, **not mirrored**, **no peak-hold caps**,
        uniform fill with no gradient, attack 0ms / decay 50ms.

        ⚠ Deviation, stated rather than hidden: this renderer's `bars` array is
        NBARS=40 bins over 40Hz-10kHz, so the band count and span are not
        theirs. The band ENVELOPE already matches (analyze() applies instant
        attack with a slow decay). Widening the analysis would change every
        other viz, so the shape is copied and the bin count is not.

        Note their MODERN uploads do not use this at all — it is a retired
        format, kept because it is a clean, readable, un-busy option."""
        base = int(self.H * 0.72)
        maxh = self.H * 0.22
        n = NBARS
        gap = max(1.0, self.W * 0.004)
        bw = (self.W * 0.92 - gap * (n - 1)) / n
        x = self.W * 0.04
        col = self._pal() * 0.95
        for k in range(n):
            h = int(a.bars[i, k] * maxh)
            if h > 1:
                x0, x1 = int(x), int(x + bw)
                cv2.rectangle(img, (x0, base - h), (x1, base),
                              tuple(float(v) for v in col), -1)
            x += bw + gap

    # -- NEW CENTERPIECES (Zeke 08-27: "main visualization needs to be
    #    something different, more complicated") --------------------------

    def _viz_tunnel(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Bass-reactive 3D tunnel fly-through: spectrum-deformed polygon
        rings receding to a vanishing point; bass drives fly speed, highs
        spin it, the drop kicks both."""
        cx, cy = self.W // 2, int(self.H * 0.45)
        drop = bool(a.drop[i])
        ph = getattr(self, "_tun_phase", 0.0)
        rot = getattr(self, "_tun_rot", 0.0)
        ph += 0.10 + 0.55 * a.bass[i] + (0.35 if drop else 0.0)
        rot += 0.004 + 0.030 * a.high[i] + (0.012 if drop else 0.0)
        self._tun_phase, self._tun_rot = ph, rot
        n_r, n_v = 16, 40
        th = np.linspace(0, 2 * np.pi, n_v, endpoint=False)
        # spectrum wraps the ring, mirrored so bass sits top+bottom
        bi = (np.abs(((th / (2 * np.pi)) * 2 * NBARS) % (2 * NBARS)
                     - NBARS)).astype(int) % NBARS
        deform = 1.0 + a.bars[i, bi] * (0.30 + 0.45 * a.bass[i])
        frac = ph % 1.0
        for k in range(n_r, 0, -1):
            z = (k - frac) / n_r
            if z <= 0.03:
                continue
            r = (self.H * 0.085) / z
            if r > self.H * 1.6:
                continue
            wob = 0.10 * np.sin(k * 0.9 + ph * 0.7)
            thk = th + rot * (1.0 + 0.12 * k) + wob
            xs = cx + r * deform * np.cos(thk)
            ys = cy + r * deform * 0.82 * np.sin(thk)
            pts = np.stack([xs, ys], 1).astype(np.int32).reshape(-1, 1, 2)
            fade = np.clip(1.15 - z, 0, 1) ** 1.6
            col = self._pal(k % 4) * (0.20 + 0.80 * fade) \
                * (0.45 + 0.75 * a.rms[i])
            cv2.polylines(img, [pts], True,
                          tuple(float(x) for x in col),
                          max(1, int(1 + 2.5 * fade * (0.5 + a.bass[i]))),
                          cv2.LINE_AA)

    def _viz_supernova(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Orbiting particle cloud around the logo that DETONATES on the
        drop, with expanding shockwave rings on every kick."""
        if getattr(self, "_nova", None) is None:
            rng = np.random.default_rng(3)
            n = 420
            self._nova = np.stack([rng.uniform(0, 2 * np.pi, n),
                                   rng.uniform(0.45, 1.0, n),
                                   rng.uniform(0.25, 1.0, n)], 1) \
                .astype(np.float32)          # angle, radius-norm, size
            self._nova_burst = 0.0
            self._shock: list = []
        cx, cy = self._logo_center()
        drop = bool(a.drop[i])
        if a.kick[i]:
            self._shock.append([self.H * 0.10, 1.0 if drop else 0.45])
            if drop:
                self._nova_burst = min(1.6, self._nova_burst + 0.9)
        p = self._nova
        p[:, 0] += (0.012 + 0.055 * a.bass[i]) * (0.6 + p[:, 2])
        burst = self._nova_burst
        self._nova_burst *= 0.90
        # orbit OUTSIDE the logo (first cut hid everything behind the helmet)
        base_r = self.H * (0.30 + 0.06 * a.bass[i])
        rr = base_r * p[:, 1] * (1.0 + burst * p[:, 2] * 2.8)
        xs = (cx + rr * np.cos(p[:, 0])).astype(np.int32)
        ys = (cy + rr * 0.85 * np.sin(p[:, 0])).astype(np.int32)
        ok = ((xs >= 2) & (xs < self.W - 2) & (ys >= 2) & (ys < self.H - 2))
        col, col2 = self._pal(), self._pal(1)
        bright = 0.55 + 0.65 * a.rms[i] + 0.6 * burst
        layer = np.zeros_like(img)
        for j in np.where(ok)[0]:
            c = col if j % 2 else col2
            sz = 1 if p[j, 2] < 0.6 else 2
            layer[ys[j] - sz:ys[j] + sz + 1, xs[j] - sz:xs[j] + sz + 1] += \
                c * (p[j, 2] * bright)
        keep = []
        for s in self._shock:
            s[0] += self.H * 0.020 * (1.0 + a.bass[i])
            s[1] *= 0.90
            if s[1] > 0.04 and s[0] < self.H * 1.3:
                cv2.circle(layer, (cx, cy), int(s[0]),
                           tuple(float(x) for x in col * s[1] * 1.6),
                           max(1, int(1 + 5 * s[1])), cv2.LINE_AA)
                keep.append(s)
        self._shock = keep
        # glow pass so the cloud reads as light, not specks
        img += layer + cv2.GaussianBlur(layer, (0, 0), 4) * 1.4

    def _viz_kaleido(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Kaleidoscope: renders the radial spectrum, then folds the whole
        frame through a rotating 6-fold mirror — fractal cathedral look.
        Logo + lyrics stay crisp on top (drawn after _viz)."""
        # source content that always crosses the fold wedge: full-width
        # center bars + radial spokes (v1 folded empty starfield — the rings
        # sat behind the logo, out of the sampled wedge)
        self._viz_bars_center(img, i, a)
        self._viz_radial(img, i, a)
        q = 2
        h, w = self.H // q, self.W // q
        if getattr(self, "_kal_r", None) is None:
            Y, X = np.mgrid[0:h, 0:w].astype(np.float32)
            dx, dy = X - w / 2, Y - h / 2
            self._kal_r = np.sqrt(dx * dx + dy * dy)
            self._kal_th0 = np.arctan2(dy, dx)
        rot = getattr(self, "_kal_rot", 0.0) + 0.004 + 0.022 * a.bass[i] \
            + (0.010 if a.drop[i] else 0.0)
        self._kal_rot = rot
        small = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        seg = np.pi / 3.0
        theta = np.abs(((self._kal_th0 + rot) % (2 * seg)) - seg)
        zoom = 1.0 + 0.30 * a.bass[i]
        mx = (w / 2 + self._kal_r / zoom * np.cos(theta)).astype(np.float32)
        my = (h * 0.55 + self._kal_r / zoom * np.sin(theta) * 0.6) \
            .astype(np.float32)
        fold = cv2.remap(small, mx, my, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REFLECT)
        img[:] = cv2.resize(fold, (self.W, self.H),
                            interpolation=cv2.INTER_LINEAR) * 1.25

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
        if self.safe_margins:
            # bottom-right is the WORST spot on TikTok — the caption block and
            # the action rail overlap there, so this watermark was rendering
            # 100% invisible. Move it inside the safe box (upper-left).
            y0, x0 = int(self.H * 0.115), int(self.W * 0.06)
        else:
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

    def _shape_now(self, i: int, a: Analysis) -> str:
        """Which shape is on screen at frame i. Swaps every `shape_every` beats
        so the page changes with the music instead of one model reacting for
        the whole song (Zeke 2026-08-28)."""
        if not self._shapes:
            return "none"
        if len(self._shapes) == 1 or self.shape_every <= 0:
            return self._shapes[0]
        slot = int(a.beat_i[i]) // int(self.shape_every)
        return self._shapes[slot % len(self._shapes)]

    # nod shape constants — see _nod_pitch
    NOD_LIFT = 0.45      # how far it rides UP between beats, x the down peak
    NOD_SWING = 0.25     # fraction of a beat spent swinging down into the hit

    def _nod_pitch(self, i: int, a: Analysis) -> float:
        """Beat-synced NOD: a real head tilt whose **peak downward angle lands
        exactly ON the beat** (Zeke 2026-08-28: "he needs to actually tilt to
        nod... hits its peak downward angle on the beat").

        v1 was wrong twice over: it was mostly a translation (read as bobbing,
        not nodding) and its peak fell just AFTER the beat, because it decayed
        from zero at phase 0. A head that starts moving on the beat is already
        late — the swing has to be anticipatory, arriving at the bottom as the
        kick lands. So the curve runs:

            ph 0.00  ......  peak DOWN (the hit)
            ph 0.00->0.75    ease back up, overshooting to -LIFT (chin raised)
            ph 0.75->1.00    accelerate down, arriving at peak DOWN on the next

        Periodic and continuous at the wrap (both ends = +1), so there is no
        teleport frame.

        Returns radians of CHIN-DOWN (+ = chin toward chest). ⚠ `_project`'s rx
        runs the other way — verified by eye on a pitch sweep, -0.85 shows the
        top of the cranium, +0.85 shows the jaw — so call sites pass `-nod`."""
        if not self.nod:
            return 0.0
        ph = float(a.beat_ph[i])
        lift, sw = self.NOD_LIFT, self.NOD_SWING
        if ph >= 1.0 - sw:
            u = (ph - (1.0 - sw)) / sw          # 0..1, the downward strike
            val = -lift + (1.0 + lift) * u * u   # accelerating, hits +1 at u=1
        else:
            u = ph / (1.0 - sw)                  # 0..1, the recovery + windup
            val = 1.0 - (1.0 + lift) * (1.0 - (1.0 - u) ** 2)
        amp = 0.34 + 0.20 * float(a.bass[i]) + (0.10 if a.drop[i] else 0.0)
        return amp * val

    def _viz_shape(self, img: np.ndarray, i: int, a: Analysis) -> None:
        """Draw the current slot, which may hold SEVERAL objects at once.

        Zeke 2026-08-28: *"you can also have like more than 1 object up at a
        time, like 3 skulls nodding their heads to the beat, or put shapes
        inside each other."* A slot is `a+b+c` — '+' means simultaneous, comma
        (handled in _shape_now) still means change-over-time. --layout picks
        how a group is arranged."""
        slot_spec = self._shape_now(i, a)
        if slot_spec in ("none", ""):
            return
        if self._shapes and len(self._shapes) > 1:
            slot = int(a.beat_i[i]) // max(1, int(self.shape_every))
            if slot != self._shape_last:
                self._shape_last = slot
                # a swap resets the spin so each model enters facing forward
                self._rot = 0.0
        drop = bool(a.drop[i])
        self._rot += (0.012 + 0.05 * a.bass[i] + (0.03 if drop else 0.0))
        members = [s for s in slot_spec.split("+") if s]
        bx, by = self._logo_center()
        base = self.H * 0.14 * (1.0 + 0.22 * a.bass[i])
        n = len(members)
        for k, shape in enumerate(members):
            if n == 1:
                cx, cy, size = bx, by, base
            elif self.layout == "nested":
                # concentric: biggest outermost, each inner one smaller
                cx, cy = bx, by
                size = base * (1.0 - 0.26 * k)
            else:                                   # row
                # derive spacing FROM the projected width, don't guess it. A
                # model draws to roughly 2.9x `size` across (size*1.5 radius
                # plus perspective), so a fixed span made three skulls overlap
                # into one blob. This makes them always fit the frame.
                slot_w = self.W * 0.94 / n
                size = min(base, slot_w / 2.9)
                cx = int(bx + slot_w * (k - (n - 1) / 2.0))
                cy = by
            self._draw_shape(img, i, a, shape, cx, cy, size, drop)

    def _draw_shape(self, img: np.ndarray, i: int, a: Analysis, shape: str,
                    cx: int, cy: int, size: float, drop: bool) -> None:
        # NOD IS FOR HEADS ONLY (Zeke 08-28: "if it's not head shaped it
        # doesn't need a head nod"). Non-heads get a beat PULSE instead — the
        # object still answers the music, it just doesn't pretend to have a
        # chin.
        if self.nod and self.is_head(shape):
            nod = self._nod_pitch(i, a)
            cy = int(cy + nod * self.H * 0.028)
        else:
            nod = 0.0
            if self.nod:
                size *= 1.0 + 0.12 * max(0.0, 1.0 - float(a.beat_ph[i]) * 3.0)
        # flat models rock instead of revolving, so they never go edge-on
        spin = (0.62 * np.sin(self._rot * 0.55) if self.is_flat(shape)
                else self._rot)
        col = self._pal()
        layer = np.zeros_like(img)
        if shape == "orb":
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
        elif shape == "logo3d" and self._logo_rgb is not None:
            # TRUE continuous 360 spin (Zeke 08-26 "do a 360 on a loop"):
            # width follows cos(ang); when the back faces us the logo is
            # MIRRORED (like a card turning), slightly dimmed — so it reads
            # as one full rotation instead of a fold-and-return.
            mask = self._logo_mask
            lh, lw = mask.shape
            ang = self._rot % (2 * np.pi)
            c = np.cos(ang)
            back = c < 0
            fold = max(0.06, abs(c))
            src_rgb = self._logo_rgb[:, ::-1] if back else self._logo_rgb
            src_m = mask[:, ::-1] if back else mask
            wq = max(8, int(lw * fold * (size * 2 / lh)))
            hq = int(size * 2)
            sk = 0.18 * np.sin(ang)          # slight perspective skew
            dstq = np.float32([[cx - wq / 2, cy - hq / 2 + sk * wq],
                               [cx + wq / 2, cy - hq / 2 - sk * wq],
                               [cx + wq / 2, cy + hq / 2 + sk * wq],
                               [cx - wq / 2, cy + hq / 2 - sk * wq]])
            srcq = np.float32([[0, 0], [lw, 0], [lw, lh], [0, lh]])
            M = cv2.getPerspectiveTransform(srcq, dstq)
            wrgb = cv2.warpPerspective(np.ascontiguousarray(src_rgb), M,
                                       (self.W, self.H))
            wm = cv2.warpPerspective(np.ascontiguousarray(src_m), M,
                                     (self.W, self.H))[..., None]
            shade = (0.55 if back else 0.75) + 0.25 * a.rms[i]
            img[:] = img * (1 - wm) + wrgb * wm * shade
            edge = cv2.morphologyEx(wm[..., 0], cv2.MORPH_GRADIENT,
                                    np.ones((3, 3), np.uint8))
            layer += cv2.GaussianBlur(edge, (0, 0), 3)[..., None] * col
        elif shape.startswith("model:"):
            # GPU path first (Zeke 08-28 greenlit): shaded/lit geometry, and
            # density stops mattering. Falls through to the CPU line loop if GL
            # is unavailable — a missing centrepiece would be worse than a
            # slower one.
            if self.gpu3d != "off" and self._gpu_draw(
                    img, shape.split(":", 1)[1], size, cx, cy, spin, nod, col):
                return
            # GLB wireframe centerpiece (Zeke 08-27: "adding some 3-D reactive
            # shapes... like a skull"). Mesh from assets/models3d via trimesh,
            # same projection pipeline as the procedural shapes.
            mesh = self._model_mesh(shape.split(":", 1)[1])
            if mesh is None:
                return
            v, edges = mesh
            pts = self._project(v, size * 1.5, cx, cy, spin,
                                tumble=False, pitch=-nod)
            lw = 1 if len(edges) > 900 else 2
            for e0, e1 in edges:
                cv2.line(layer, tuple(pts[e0][0]), tuple(pts[e1][0]),
                         tuple(float(x) for x in col), lw, cv2.LINE_AA)
        else:
            mesh = self._shape_mesh().get(shape)
            if mesh is None:
                return
            v, edges = mesh
            pts = self._project(v, size, cx, cy, spin, pitch=-nod)
            for e0, e1 in edges:
                cv2.line(layer, tuple(pts[e0][0]), tuple(pts[e1][0]),
                         tuple(float(x) for x in col), 2, cv2.LINE_AA)
        glow = cv2.GaussianBlur(layer, (0, 0), 5 + 7 * a.bass[i])
        img += layer + glow * (0.5 + 0.8 * a.rms[i])

    _MODEL_CACHE: dict = None    # class-level cache, lazy dict (dataclass-safe)

    _POSES: dict = None

    @classmethod
    def poses(cls) -> dict:
        """Per-model pose data from assets/models3d/poses.json:
            {"heart": {"rot": [rx, ry, rz], "scale": 1.0, "head": false}}

        Zeke 2026-08-28: *"some of the 3d objects should be rotated like the
        heart and what not and the star, also if it's not head shaped it
        doesn't need a head nod."* A downloaded mesh arrives in whatever pose
        its author modelled it in — there is no convention — so orientation has
        to be per-model data, not a global guess.

        Kept SEPARATE from manifest.json on purpose: build_model_manifest
        rewrites that file wholesale, which would silently wipe hand-tuned
        poses."""
        if cls._POSES is None:
            p = (Path(__file__).resolve().parent.parent / "assets"
                 / "models3d" / "poses.json")
            try:
                cls._POSES = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                cls._POSES = {}
        return cls._POSES

    def is_head(self, spec: str) -> bool:
        """Only head-shaped models nod. A torus knot has no chin."""
        if not spec.startswith("model:"):
            return spec == "logo3d"
        return bool(self.poses().get(spec.split(":", 1)[1], {}).get("head"))

    def is_flat(self, spec: str) -> bool:
        """Essentially 2-D models (heart, star, bolt, disc). A continuous 360
        spin turns these EDGE-ON for half of every revolution, where they
        collapse to a line and effectively disappear — so they get a gentle
        oscillation instead. Found by rendering the heart at 0/90/180/270 and
        looking: two of the four angles were an invisible slab."""
        if not spec.startswith("model:"):
            return False
        return bool(self.poses().get(spec.split(":", 1)[1], {}).get("flat"))

    def _model_mesh(self, spec: str):
        """Load `model:<name-or-path>` -> (verts[-1..1, y-flipped], edges).
        Bare names resolve to assets/models3d/<name>.glb. Cached per path, so a
        multi-model rotation loads each mesh once and then swaps for free.
        A pose from poses.json is baked in at load time, so it costs nothing
        per frame."""
        if Renderer._MODEL_CACHE is None:
            Renderer._MODEL_CACHE = {}
        if spec in Renderer._MODEL_CACHE:
            return Renderer._MODEL_CACHE[spec]
        try:
            p = Path(spec)
            if not p.is_file():
                p = Path(__file__).resolve().parent.parent \
                    / "assets" / "models3d" / f"{spec}.glb"
            import trimesh
            m = trimesh.load(str(p), force="mesh")
            v = np.asarray(m.vertices, np.float32)
            v -= v.mean(axis=0)
            v /= max(1e-6, np.abs(v).max())          # unit cube like _SHAPES
            v[:, 1] *= -1.0                          # glTF Y-up -> screen Y-down
            # BAKE THE POSE (Zeke 08-28: "some of the 3d objects should be
            # rotated like the heart and what not and the star"). Authors model
            # in whatever orientation they like and glTF has no convention for
            # "upright", so the correction is per-model data. Applied once at
            # load and cached, so it is free per frame.
            pose = self.poses().get(spec, {})
            rot = pose.get("rot")
            if rot:
                rx, ry, rz = (np.radians(float(x)) for x in rot)
                for axis, ang in ((0, rx), (1, ry), (2, rz)):
                    if abs(ang) < 1e-9:
                        continue
                    c, s = np.cos(ang), np.sin(ang)
                    if axis == 0:
                        R = np.array([[1, 0, 0], [0, c, -s], [0, s, c]], np.float32)
                    elif axis == 1:
                        R = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], np.float32)
                    else:
                        R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], np.float32)
                    v = v @ R.T
                v = v / max(1e-6, float(np.abs(v).max()))
            f = np.asarray(m.faces, np.int64)
            e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]],
                                        f[:, [2, 0]]]), axis=1)
            edges = [tuple(x) for x in np.unique(e, axis=0)]
            print(f"[lyric_viz] model loaded: {p.name} "
                  f"({len(v)} verts, {len(edges)} edges)")
            out = (v, edges)
        except Exception as ex:
            print(f"[lyric_viz] model load FAILED ({spec}): {ex!r}")
            out = None
        Renderer._MODEL_CACHE[spec] = out
        return out

    def _model_faces(self, spec: str):
        """(verts, faces) for the GPU path. The CPU path only ever needed
        EDGES, but shaded rendering needs triangles and per-vertex normals, so
        this keeps faces separately rather than widening the existing tuple
        (which several call sites unpack)."""
        if spec in self._gpu_faces:
            return self._gpu_faces[spec]
        out = None
        try:
            p = Path(spec)
            if not p.is_file():
                p = (Path(__file__).resolve().parent.parent / "assets"
                     / "models3d" / f"{spec}.glb")
            import trimesh
            m = trimesh.load(str(p), force="mesh")
            v = np.asarray(m.vertices, np.float32)
            v -= v.mean(axis=0)
            v /= max(1e-6, float(np.abs(v).max()))
            v[:, 1] *= -1.0                      # glTF Y-up -> screen Y-down
            pose = self.poses().get(spec, {})
            rot = pose.get("rot")
            if rot:
                for axis, deg in enumerate(rot):
                    a = np.radians(float(deg))
                    if abs(a) < 1e-9:
                        continue
                    c, s = np.cos(a), np.sin(a)
                    R = ([[1, 0, 0], [0, c, -s], [0, s, c]] if axis == 0 else
                         [[c, 0, s], [0, 1, 0], [-s, 0, c]] if axis == 1 else
                         [[c, -s, 0], [s, c, 0], [0, 0, 1]])
                    v = v @ np.array(R, np.float32).T
                v /= max(1e-6, float(np.abs(v).max()))
            out = (v, np.asarray(m.faces, np.int64))
        except Exception as ex:
            print(f"[lyric_viz] gpu mesh load FAILED ({spec}): {ex!r}")
        self._gpu_faces[spec] = out
        return out

    def _gpu_draw(self, img, spec: str, size: float, cx: int, cy: int,
                  spin: float, pitch: float, col) -> bool:
        """Render a model on the GPU into `img`. Returns False if unavailable
        so the caller falls back to the CPU line loop rather than dropping the
        centrepiece."""
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import gpu_mesh
        except Exception:
            return False
        g = gpu_mesh.get_renderer(self.W, self.H)
        if g is None:
            return False
        vf = self._model_faces(spec)
        if vf is None:
            return False
        v, f = vf
        # ⚠ Both of these were wrong on the first pass and the render came back
        # as a white blob filling the frame (seen by eye). base*0.55 + rim*1.15
        # saturated BEFORE the bloom pass ran on top of it; and a 0.87 scale
        # factor put the model at ~50% of frame height where the CPU path sits
        # near 20%. Shaded geometry covers area that wireframe leaves empty, so
        # it needs to be SMALLER and DIMMER than the outline version, not equal.
        # Surface DARK, edges BRIGHT. v2 scaled both down together and the
        # models came back as pale grey patches — flatter and less legible than
        # the CPU wireframe they were meant to improve on. A low-poly skull lit
        # diffusely is nearly uniform, so the contrast has to come from the
        # edges, not the shading.
        # ★ Tuned to sit UNDER the bloom threshold (190). The bloom pass runs
        # AFTER this layer, so anything brighter gets re-brightened and washes
        # to a pale blob — which is exactly what happened twice while I tuned
        # against a black test background instead of a real frame. base 0.55 of
        # a palette colour lands near 140: solid and readable, and the bloom
        # then haloes the silhouette instead of flooding the interior.
        base = np.clip(np.asarray(col, np.float32) / 255.0 * 0.55, 0, 1)
        rim = np.clip(np.asarray(col, np.float32) / 255.0 * 0.75, 0, 1)
        gs = 0.62 * (size / max(1e-6, self.H * 0.14))
        try:
            lay = g.render((spec, self.gpu3d), v, f, rot=spin, pitch=-pitch,
                           scale=gs, mode=self.gpu3d,
                           base=tuple(base), rim=tuple(rim))
        except Exception as ex:
            print(f"[lyric_viz] gpu render failed ({spec}): {ex!r}")
            return False
        # GL renders centred; shift to the requested centre without re-rendering
        dx, dy = int(cx - self.W // 2), int(cy - self.H // 2)
        if dx or dy:
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            lay = cv2.warpAffine(lay, M, (self.W, self.H))
        img += lay
        return True

    def _project(self, v: np.ndarray, size: float, cx: int, cy: int,
                 rot: float, tumble: bool = True,
                 pitch: float = 0.0) -> np.ndarray:
        ry, rx = rot, (rot * 0.62 if tumble else 0.30) + pitch
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
            # scramble is the single biggest readability cost — it replaces the
            # actual characters. --readable kills it outright rather than
            # softening it; a half-scrambled word is still an unreadable word.
            if drop and self.style.scramble and k != active and not self.readable:
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
        # SAFE ZONE (research 2026-08-28). TikTok's own UI covers the bottom
        # caption block and the right-hand action rail, so a full-width line at
        # the old 0.92 cap put ~97px of every long line UNDER the rail, and the
        # 0.78 baseline pushed descenders into the caption block. Narrower and
        # higher in --tiktok. ⚠ There is no authoritative TikTok safe-zone spec
        # and the UI is responsive — these are a convergent envelope from
        # several third-party sources, so --safe-overlay draws the box to check
        # against a real phone rather than trusting the number.
        wcap = 0.74 if self.safe_margins else 0.92
        while total > self.W * wcap and fs > 18:
            fs = int(fs * 0.9)
            f = self.font(fs, self.style.font_lyrics)
            space = draw.textlength(" ", font=f)
            widths = [draw.textlength(w, font=f) for w in words]
            total = sum(widths) + space * (len(words) - 1)
        x = (self.W - total) / 2
        if self.safe_margins:
            y = int(self.H * 0.70)
        else:
            y = int(self.H * (0.78 if self.style.viz == "radial" else 0.66))
        accent = self.style.palette[self._pal_i % len(self.style.palette)]
        # REACTIVE LYRICS (Zeke 08-26: "the words also have audio
        # visualization on them"): every LETTER rides its own frequency band
        # (bounces with the spectrum), the active word swells with the bass.
        xx = x
        letter_j = 0
        for k, (w, wd) in enumerate(zip(words, widths)):
            col = (accent if k == active else
                   ((120, 120, 130) if not drop else accent))
            if k == active and a.bass[i] > 0.05:
                # bass-swollen active word (quantized size → font cache safe)
                fs_a = int(fs * (1.0 + round(0.30 * a.bass[i] * 6) / 6))
                fa = self.font(fs_a, self.style.font_lyrics)
                wa = draw.textlength(w, font=fa)
                cxw = xx + wd / 2
                xa = cxw - wa / 2
                for ch in w:
                    band = a.bars[i, (letter_j * 3) % NBARS]
                    dy = -band * fs * 0.35
                    draw.text((xa, y + dy - (fs_a - fs) / 2), ch,
                              font=fa, fill=col)
                    xa += draw.textlength(ch, font=fa)
                    letter_j += 1
            else:
                xc = xx
                for ch in w:
                    band = a.bars[i, (letter_j * 3) % NBARS]
                    dy = -band * fs * (0.35 if k == active or drop else 0.20)
                    draw.text((xc, y + dy), ch, font=f, fill=col)
                    xc += draw.textlength(ch, font=f)
                    letter_j += 1
            xx += wd + space
        lyr = np.asarray(layer, np.float32)
        # CONTRAST SCRIM — a mask grown from the GLYPHS, not a rectangle bar,
        # so it hugs the text and is invisible as a shape. Runs ALWAYS now
        # (softly), not just under --readable: busy frames are the norm, and
        # the governing principle is *protect the text by darkening the
        # background, never by brightening the text* — brightening blooms into
        # the backdrop and reduces contrast. --readable just deepens it.
        m = lyr.max(axis=2)
        cv2.dilate(m, np.ones((9, 9), np.uint8), dst=m)
        cv2.GaussianBlur(m, (0, 0), 13, dst=m)
        peak = float(m.max())
        if peak > 1e-6:
            np.multiply(m, 1.0 / peak, out=m)
            np.clip(m, 0, 1, out=m)
            img *= (1.0 - (0.72 if self.readable else 0.38) * m[..., None])
        # QUARTER-RES glow. A full-res GaussianBlur at sigma up to 12 measures
        # ~229ms; the same visual at quarter res is ~6ms. And scaleAdd writes
        # in place — the old `img += lyr*0.95 + glow*k` allocated three full-res
        # temporaries and measured 84ms against 25ms for these two calls.
        qh, qw = max(1, self.H // 4), max(1, self.W // 4)
        gq = cv2.resize(lyr, (qw, qh), interpolation=cv2.INTER_AREA)
        cv2.GaussianBlur(gq, (0, 0), 1.0 + 2.0 * a.rms[i], dst=gq)
        glow = cv2.resize(gq, (self.W, self.H), interpolation=cv2.INTER_LINEAR)
        cv2.scaleAdd(lyr, 0.95, img, dst=img)
        cv2.scaleAdd(glow, 0.35 + 0.75 * a.rms[i], img, dst=img)

    # -- drop FX ----------------------------------------------------------
    def _fx(self, img: np.ndarray, i: int, a: Analysis) -> np.ndarray:
        s = self.style
        drop = bool(a.drop[i])
        if a.kick[i]:
            self._pal_i += 1
            self._flash = max(self._flash, (1.0 if drop else 0.45))
            self._zoom = max(self._zoom, s.zoom_punch * (1.5 if drop else 1.0))
        # --readable turns the tearing DOWN, not off: the drop should still
        # feel violent, it just must not eat the words (Zeke 2026-08-28).
        # Gating on `i // 6` instead of `i // 2` also makes each tear last
        # ~3x longer, so it reads as a deliberate cut rather than as noise.
        if drop and s.glitch and not (self.readable and (i // 6) % 3):
            rng = random.Random(i // (6 if self.readable else 2))
            lo, hi = ((1, 2) if self.readable else (2, 5))
            amp = (0.4 if self.readable else 1.0) * (30 + 60 * a.bass[i])
            for _ in range(rng.randint(lo, hi)):
                y0 = rng.randrange(0, self.H - 12)
                h = rng.randrange(4, 26)
                off = rng.randrange(-int(amp), int(amp) or 1)
                img[y0:y0 + h] = np.roll(img[y0:y0 + h], off, axis=1)
        if drop and s.rgb_split:
            off = int((2 + 6 * a.bass[i]) * (0.45 if self.readable else 1.0))
            img[..., 0] = np.roll(img[..., 0], off, axis=1)
            img[..., 2] = np.roll(img[..., 2], -off, axis=1)
        # geometric: zoom punch + shake in one affine
        z = 1.0 + self._zoom
        dx = dy = 0.0
        if drop and s.shake > 0:
            rng = random.Random(i)
            # per-frame random shake is what makes text swim; halve it in
            # --readable rather than remove it, so the drop keeps its weight
            amp = s.shake * (0.4 + 0.6 * a.bass[i]) * (0.5 if self.readable
                                                       else 1.0)
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
        elif not self._shapes:
            self._logo(img, i, a)
        if self._shapes:
            self._viz_shape(img, i, a)
        self._corner(img, i, a)
        self._lyrics(img, i, t, a)
        img = self._fx(img, i, a)
        if self.bloom > 0:
            self._bloom(img, i, a)
        if self._viz_flash > 0.01:
            col = np.array(self.style.palette[self._pal_i
                                              % len(self.style.palette)],
                           np.float32)
            img += self._viz_flash * 0.30 * col
            self._viz_flash *= 0.60
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
        # clip IN PLACE then convertScaleAbs (12ms vs 38ms for .astype).
        # ⚠ convertScaleAbs takes the ABSOLUTE value, so the clip to >=0 above
        # is load-bearing, not tidiness — on unclipped data a -8 becomes 8.
        np.clip(img, 0, 255, out=img)
        out = cv2.convertScaleAbs(img)
        if self.safe_overlay:
            # DEBUG: the claimed TikTok-safe box. Render one, put it on a phone,
            # open the TikTok upload preview, and see what the UI actually
            # covers — the published figures are third-party guesses.
            x0, x1 = int(self.W * 0.056), int(self.W * 0.870)
            y0, y1 = int(self.H * 0.104), int(self.H * 0.823)
            cv2.rectangle(out, (x0, y0), (x1, y1), (0, 255, 0), 2)
            cv2.putText(out, "SAFE", (x0 + 6, y0 + 26),
                        cv2.FONT_HERSHEY_PLAIN, 1.6, (0, 255, 0), 2)
        return out


# ---------------------------------------------------------------------------
# 4b. Loop color-matching + procedural loop generation (Zeke 08-26: "the
#     colors don't really match" / "make some that will match style")
# ---------------------------------------------------------------------------

def duotone(clip: np.ndarray, style: Style) -> np.ndarray:
    """Re-color a loop through the style's palette: the loop becomes pure
    light/shadow, the COLORS come from the style — so any loop matches any
    song. Tritone: shadows→deep 3rd color, mids→2nd, highs→1st."""
    lum = clip.astype(np.float32).mean(axis=-1, keepdims=True) / 255.0
    c_hi = np.array(style.palette[0], np.float32)
    c_mid = np.array(style.palette[1], np.float32) * 0.55
    c_lo = np.array(style.palette[2 % len(style.palette)], np.float32) * 0.12
    lo_w = np.clip(1.0 - lum * 2.0, 0, 1)
    hi_w = np.clip(lum * 2.0 - 1.0, 0, 1)
    mid_w = 1.0 - lo_w - hi_w
    out = lo_w * c_lo + mid_w * c_mid + hi_w * c_hi
    return np.clip(out, 0, 255).astype(np.uint8)


def gen_loops(w: int, h: int, fps: int, bpm: float) -> list[np.ndarray]:
    """Procedural VJ loops, grayscale (duotone gives them the style's colors).
    Beat-locked: loop length = 4 beats so cuts always land clean."""
    T = max(int(fps * 4 * 60.0 / max(60.0, bpm or 120.0)), fps)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2, h / 2
    dx, dy = xx - cx, yy - cy
    r = np.sqrt(dx ** 2 + dy ** 2) / (h / 2)
    th = np.arctan2(dy, dx)
    loops = []

    # 1. laser tunnel: rotating spokes + rings expanding once per beat
    frames = []
    for t in range(T):
        ph = t / T * 2 * np.pi
        spokes = (np.sin(th * 12 + ph * 2) > 0.86).astype(np.float32)
        rings = np.clip(np.sin(r * 10 - ph * 4), 0, 1) ** 6
        g = np.clip((spokes * (1 - r * 0.7) + rings * 0.8), 0, 1)
        frames.append((g * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    # 2. starburst streaks: particles streaming outward, respawn each loop
    rng = np.random.default_rng(11)
    n = 90
    ang = rng.uniform(0, 2 * np.pi, n)
    spd = rng.uniform(0.3, 1.0, n)
    frames = []
    for t in range(T):
        img = np.zeros((h, w), np.float32)
        prog = ((t / T) + spd) % 1.0
        rad = prog * (h * 0.75)
        xs = (cx + np.cos(ang) * rad).astype(int)
        ys = (cy + np.sin(ang) * rad).astype(int)
        ok = (xs > 1) & (xs < w - 2) & (ys > 1) & (ys < h - 2)
        img[ys[ok], xs[ok]] = 1.0
        img = cv2.GaussianBlur(img, (0, 0), 1.2) * 3.0
        img += np.clip(1 - r * 2.2, 0, 1) * 0.35          # core glow
        frames.append((np.clip(img, 0, 1) * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    # 3. grid pulse: dot lattice, brightness waves sweep through per beat
    gx, gy = np.meshgrid(np.arange(8, w, 24), np.arange(8, h, 24))
    frames = []
    for t in range(T):
        ph = t / T * 2 * np.pi
        img = np.zeros((h, w), np.float32)
        b = 0.4 + 0.6 * np.sin(ph * 4 + (gx / w) * 6 + (gy / h) * 3) ** 2
        for X, Y, B in zip(gx.ravel(), gy.ravel(), b.ravel()):
            img[int(Y) - 1:int(Y) + 2, int(X) - 1:int(X) + 2] = B
        img = cv2.GaussianBlur(img, (0, 0), 1.0) * 2.2
        frames.append((np.clip(img, 0, 1) * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    # 4. warp rings: breathing concentric waves (melodic/deep sections)
    frames = []
    for t in range(T):
        ph = t / T * 2 * np.pi
        g = np.clip(np.sin(r * 6 - ph * 2 + np.sin(th * 3 + ph) * 0.5), 0, 1) ** 3
        g *= np.clip(1.1 - r, 0, 1)
        frames.append((g * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    # 5. oscilloscope wave: stacked traveling sine traces (Greyland scope look)
    frames = []
    for t in range(T):
        ph = t / T * 2 * np.pi
        img = np.zeros((h, w), np.float32)
        xs = np.arange(w)
        for k, (amp, fr_, off) in enumerate(((0.24, 2, 0.0), (0.14, 3, 2.1),
                                             (0.08, 5, 4.2))):
            ys = (h / 2 + np.sin(xs / w * fr_ * 2 * np.pi + ph * (k + 1))
                  * h * amp * (0.6 + 0.4 * np.sin(ph + off))).astype(int)
            ok = (ys > 0) & (ys < h - 1)
            img[ys[ok], xs[ok]] = 1.0
        img = cv2.GaussianBlur(img, (0, 0), 1.4) * 3.2
        frames.append((np.clip(img, 0, 1) * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    # 6. spectrum wall: full-frame bar field sweeping like an analyzer
    rng2 = np.random.default_rng(23)
    heights = rng2.uniform(0.15, 1.0, 48)
    frames = []
    for t in range(T):
        ph = t / T * 2 * np.pi
        img = np.zeros((h, w), np.float32)
        bw_ = w / 48
        for k in range(48):
            hk = heights[k] * (0.45 + 0.55 * np.sin(ph * 2 + k * 0.7) ** 2)
            y0 = int(h * (1 - hk))
            img[y0:, int(k * bw_) + 1:int((k + 1) * bw_) - 1] = \
                0.45 + 0.55 * hk
        frames.append((np.clip(img, 0, 1) * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    # 7. circular scope: polar waveform ring, wobbling
    frames = []
    for t in range(T):
        ph = t / T * 2 * np.pi
        img = np.zeros((h, w), np.float32)
        ths = np.linspace(0, 2 * np.pi, 240)
        rad = h * 0.30 * (1 + 0.18 * np.sin(ths * 6 + ph * 2)
                          + 0.08 * np.sin(ths * 13 - ph * 3))
        xs = (cx + np.cos(ths) * rad).astype(int)
        ys = (cy + np.sin(ths) * rad).astype(int)
        ok = (xs > 0) & (xs < w - 1) & (ys > 0) & (ys < h - 1)
        img[ys[ok], xs[ok]] = 1.0
        img = cv2.GaussianBlur(img, (0, 0), 1.4) * 3.0
        frames.append((np.clip(img, 0, 1) * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    # 8. particle vortex: spiral swarm orbiting the center
    rng3 = np.random.default_rng(31)
    n2 = 140
    baser = rng3.uniform(0.15, 1.0, n2)
    basea = rng3.uniform(0, 2 * np.pi, n2)
    frames = []
    for t in range(T):
        ph = t / T * 2 * np.pi
        img = np.zeros((h, w), np.float32)
        aa = basea + ph * (1.5 - baser)          # inner orbits faster
        rr2 = baser * h * 0.55 * (1 + 0.06 * np.sin(ph * 2))
        xs = (cx + np.cos(aa) * rr2).astype(int)
        ys = (cy + np.sin(aa) * rr2 * 0.6).astype(int)
        ok = (xs > 0) & (xs < w - 1) & (ys > 0) & (ys < h - 1)
        img[ys[ok], xs[ok]] = 0.5 + 0.5 * (1 - baser[ok])
        img = cv2.GaussianBlur(img, (0, 0), 1.1) * 2.6
        frames.append((np.clip(img, 0, 1) * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    # 9. lissajous: classic scope figures morphing through ratios
    frames = []
    for t in range(T):
        ph = t / T * 2 * np.pi
        img = np.zeros((h, w), np.float32)
        s = np.linspace(0, 2 * np.pi, 400)
        xs = (cx + np.sin(s * 3 + ph) * w * 0.32).astype(int)
        ys = (cy + np.sin(s * 2) * h * 0.32).astype(int)
        ok = (xs > 0) & (xs < w - 1) & (ys > 0) & (ys < h - 1)
        img[ys[ok], xs[ok]] = 1.0
        img = cv2.GaussianBlur(img, (0, 0), 1.3) * 3.0
        frames.append((np.clip(img, 0, 1) * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    # 10. glyph rain: falling character columns (the Code look, abstracted)
    rng4 = np.random.default_rng(41)
    ncol = w // 10
    speeds = rng4.uniform(0.4, 1.0, ncol)
    offs = rng4.uniform(0, 1, ncol)
    frames = []
    for t in range(T):
        img = np.zeros((h, w), np.float32)
        prog = t / T
        for k in range(ncol):
            head = ((prog * speeds[k] + offs[k]) % 1.0) * h
            trail = np.arange(0, h * 0.35, 6)
            ys = (head - trail).astype(int) % h
            fade = np.linspace(1.0, 0.1, len(ys))
            img[ys, k * 10 + 2:k * 10 + 7] = fade[:, None] * \
                (rng4.random(len(ys))[:, None] > 0.35)
        frames.append((np.clip(img, 0, 1) * 255).astype(np.uint8))
    loops.append(np.stack([cv2.merge([f, f, f]) for f in frames]))

    return loops


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
        # RATE CONTROL WAS ENTIRELY UNSET — no bitrate, GOP, or profile, so
        # output was whatever libx264's default happened to be (~6 Mbps at
        # 720p) and could silently change on a PyAV upgrade. Social platforms
        # re-encode from what you give them, so a soft master compounds into a
        # much worse final. Scale the target with pixel count.
        # ~0.19 bits/pixel/frame -> ~11.8 Mbps at 1080x1920p30. My first pass
        # used 0.11 and measured out at 6.5 Mbps, which is under what survives
        # a social re-encode; the research target was ~12.
        vstream.bit_rate = int(max(8e6, min(20e6, W * H * fps * 0.19)))
        vstream.options = {
            "preset": "slow",
            "profile": "high",
            "g": str(int(fps * 2)),      # keyframe every 2s — helps seeking
            "movflags": "+faststart",    # moov atom first: starts before full DL
        }
    except Exception:
        vstream = container.add_stream("mpeg4", rate=fps)
    vstream.width, vstream.height = W, H
    astream = container.add_stream("aac", rate=sr)
    astream.bit_rate = 192_000
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
                         "background (replaces starfield/plasma). 'gen' = "
                         "procedural loops made in the style's own palette")
    ap.add_argument("--no-tint", action="store_true",
                    help="keep loop videos' original colors (default: duotone "
                         "through the style palette so colors always match)")
    ap.add_argument("--shape", default="none",
                    help="rotating music-reactive 3D centerpiece: none|cube|"
                         "pyramid|cylinder|orb|logo3d|model:<name-or-glb-path>"
                         " (bare names resolve to assets/models3d/<name>.glb). "
                         "COMMA-LIST to cycle several, e.g. "
                         "'model:skull_kay,model:skull_quaternius,orb,cube'")
    ap.add_argument("--shape-every", type=int, default=8, metavar="BEATS",
                    help="with a comma-list --shape, swap to the next one every "
                         "N BEATS (default 8 = two bars in 4/4). 0 = never swap")
    ap.add_argument("--nod", action="store_true",
                    help="beat reaction for the 3D shape. HEAD-shaped models "
                         "(head:true in assets/models3d/poses.json) tilt "
                         "chin-down on the beat; everything else gets a size "
                         "pulse instead — a torus knot has no chin")
    ap.add_argument("--gpu3d", default="off",
                    choices=["off", "wire", "shaded", "solid_wire"],
                    help="render model: centrepieces on the GPU instead of the "
                         "CPU line loop. 'shaded' = lit surfaces, 'solid_wire' "
                         "= lit surface with its wireframe over it. Removes the "
                         "edge budget: dense meshes cost the same as sparse "
                         "ones. Falls back to CPU if GL is unavailable")
    ap.add_argument("--safe-overlay", action="store_true",
                    help="draw the claimed TikTok safe box over the frame so "
                         "it can be checked against a real phone (the "
                         "published figures are third-party, not official)")
    ap.add_argument("--layout", default="row", choices=["row", "nested"],
                    help="how a multi-object slot is arranged. Use '+' in "
                         "--shape for simultaneous objects, e.g. "
                         "'model:skull_kay+model:skull_2+model:skull_3'")
    ap.add_argument("--viz-every", type=int, default=16, metavar="BEATS",
                    help="with a comma-list --viz, rotate the centrepiece "
                         "visualization every N BEATS (default 16 = four bars). "
                         "0 = never rotate")
    ap.add_argument("--bg", default="", choices=["", "flow", "starfield",
                                                 "plasma", "flat"],
                    help="override the style's background. 'flow' = "
                         "domain-warped FBM gradient in the style palette — "
                         "the fix for flat black")
    ap.add_argument("--bloom", type=float, default=0.45, metavar="AMOUNT",
                    help="whole-frame halo glow amount (default 0.45, 0 = off). "
                         "Quarter-res two-octave halo-only bloom. Textbook "
                         "amounts (0.8-1.4) assume the source is not already "
                         "glowing; this renderer pre-glows its elements, and "
                         "dense wireframes wash out above ~0.5")
    ap.add_argument("--readable", action="store_true",
                    help="LYRICS FIRST: no character scramble, glitch tearing "
                         "and rgb-split dialled down, drop-shake halved, and a "
                         "contrast scrim grown from the glyphs so words stay "
                         "legible over a busy centrepiece")
    ap.add_argument("--no-vocals", action="store_true",
                    help="skip transcription (instrumental track)")
    ap.add_argument("--tiktok", action="store_true",
                    help="one-flag TikTok cut: 9:16, spinning logo, auto-hook "
                         "start, <=30s (see docs/tiktok_playbook.md)")
    ap.add_argument("--no-strobe", action="store_true",
                    help="disable the hard white kick-flash on the drop "
                         "(photosensitivity-safe; everything else unchanged)")
    ap.add_argument("--viz", default="",
                    help="override the style's centerpiece visualization: "
                         "radial|bars_center|bars|wave|tunnel|supernova|kaleido"
                         ". COMMA-LIST to ROTATE through several on the beat "
                         "grid (see --viz-every), e.g. "
                         "'radial,tunnel,kaleido,bars_center'")
    ap.add_argument("--window", default="",
                    help="render only START:END seconds (e.g. 55:85) — "
                         "cheap test renders")
    args = ap.parse_args()
    if args.tiktok:
        args.aspect = "9:16"
        if args.shape == "none":
            args.shape = "logo3d"
        # ★ --tiktok used to inherit the 9:16 default of 720x1280 — 2.25x FEWER
        # PIXELS than TikTok's recommended 1080x1920 source, on a platform that
        # re-encodes everything anyway, so we were handing it a degraded master
        # and letting it degrade that further. Explicit unless --size is given.
        if not args.size:
            args.size = "1080x1920"
        # TikTok is watched on a phone at arm's length: legibility beats FX.
        # --tiktok now implies --readable (no character scramble, tearing and
        # shake dialled down, contrast scrim deepened).
        args.readable = True

    if args.size:
        W, H = (int(v) for v in args.size.lower().split("x"))
    else:
        W, H = {"16:9": (1280, 720), "9:16": (720, 1280),
                "1:1": (1080, 1080)}[args.aspect]
    out = args.out or args.audio.with_suffix(f".{args.style}.mp4")
    style = STYLES[args.style]
    from dataclasses import replace as _dc_replace
    if args.no_strobe and style.strobe:
        style = _dc_replace(style, strobe=False)
        print("[lyric_viz] strobe DISABLED (--no-strobe); rest of style unchanged")
    viz_deck = None
    if args.viz:
        viz_deck = [v.strip() for v in args.viz.split(",") if v.strip()]
        bad = [v for v in viz_deck if v not in VIZ_MODES]
        if bad:
            ap.error(f"unknown --viz mode(s) {bad} — pick from "
                     f"{sorted(VIZ_MODES)}")
        # layout (lyric size/baseline, logo position) pins to the FIRST entry so
        # it stays put; only the drawn viz rotates. See Renderer._viz_now.
        style = _dc_replace(style, viz=viz_deck[0])
        if len(viz_deck) > 1:
            print(f"[lyric_viz] viz deck: {len(viz_deck)} modes "
                  f"({', '.join(viz_deck)}) rotating every {args.viz_every} "
                  f"beats; layout pinned to '{viz_deck[0]}'")
        else:
            print(f"[lyric_viz] centerpiece OVERRIDE: viz={viz_deck[0]}")
    if args.bg:
        style = _dc_replace(style, bg=args.bg)
        print(f"[lyric_viz] background OVERRIDE: bg={args.bg}")
    if args.readable:
        print("[lyric_viz] READABLE mode: scramble off, glitch/rgb-split/shake "
              "reduced, contrast scrim behind lyrics")
    t0 = time.time()

    if args.no_vocals:
        lines: list[list[Word]] = []
    else:
        heard = transcribe_words(args.audio, args.device)
        segs = getattr(transcribe_words, "last_segments", None)
        if args.lyrics and args.lyrics.is_file():
            lines = align_to_lyrics(heard,
                                    args.lyrics.read_text(encoding="utf-8"),
                                    segments=segs)
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
        bw, bh = max(2, W // 4), max(2, H // 4)
        if args.bgvideo.strip().lower() == "gen":
            bgclips = gen_loops(bw, bh, args.fps, analysis.bpm)
            print(f"[lyric_viz] generated {len(bgclips)} procedural loops "
                  f"(beat-locked to bpm~{analysis.bpm or 120})")
        else:
            import av as _av
            p = Path(args.bgvideo)
            vpaths = (sorted([q for q in p.rglob("*")
                              if q.suffix.lower() in (".mp4", ".mov", ".m4v",
                                                      ".webm", ".avi")])
                      if p.is_dir() else [Path(s) for s in args.bgvideo.split(",")])
            if len(vpaths) > 14:
                # 209-clip library would pre-decode to ~8.6GB RAM — sample a
                # song-seeded subset instead (same song = same picks, rerunnable)
                seed = sum(args.audio.name.encode())
                idx = np.random.default_rng(seed).permutation(len(vpaths))[:14]
                vpaths = [vpaths[k] for k in sorted(idx)]
                print(f"[lyric_viz] sampled 14 of the library's clips "
                      f"(seed from song name)")
            bgclips = []
            for q in vpaths:
                if not q.is_file():
                    continue
                try:
                    cont = _av.open(str(q))
                    frames = []
                    for fr in cont.decode(video=0):
                        frames.append(cv2.resize(
                            fr.to_ndarray(format="rgb24"), (bw, bh),
                            interpolation=cv2.INTER_AREA))
                        if len(frames) >= args.fps * 8:
                            break
                    if len(frames) >= args.fps:      # at least 1s of loop
                        bgclips.append(np.stack(frames))
                except Exception as e:
                    print(f"[lyric_viz] bgvideo {q.name} skipped: {e!r}")
        if bgclips and not args.no_tint:
            # colors always match: loops become light/shadow in the style's
            # own palette (Zeke 08-26: "the colors don't really match")
            bgclips = [duotone(c, style) for c in bgclips]
            print("[lyric_viz] loops duotoned to style palette")
        if bgclips:
            kicks = np.cumsum(analysis.kick.astype(int))
            bgclip_idx = (kicks // 16).astype(int)   # new loop every ~4 bars
            print(f"[lyric_viz] bg loops: {len(bgclips)} clip(s) loaded")
        else:
            bgclips = None

    r = Renderer(W, H, style, lines, args.fps, logo=logo, title=args.title,
                 wave_env=wave_env.astype(np.float32),
                 deck=deck, deck_idx=deck_idx, corner_logo=corner,
                 shape=args.shape, bgclips=bgclips, bgclip_idx=bgclip_idx,
                 shape_every=args.shape_every, nod=args.nod,
                 viz_deck=viz_deck, viz_every=args.viz_every,
                 readable=args.readable, bloom=args.bloom, layout=args.layout,
                 safe_margins=bool(args.tiktok), safe_overlay=args.safe_overlay,
                 gpu3d=args.gpu3d)
    if r._shapes and len(r._shapes) > 1:
        beats_per = max(1, args.shape_every)
        print(f"[lyric_viz] shape deck: {len(r._shapes)} shapes "
              f"({', '.join(r._shapes)}) swapping every {beats_per} beats "
              f"~= {int(analysis.beat.sum()) // beats_per} swaps"
              + (", nodding to the beat" if args.nod else ""))
    elif args.nod:
        print("[lyric_viz] nod: ON (dip-and-settle on every beat)")

    f0, f1 = 0, n_frames
    if args.tiktok:
        h0, h1 = pick_hook_window(analysis, lines, duration, args.fps)
        f0, f1 = int(h0 * args.fps), int(h1 * args.fps)
        pcm = pcm[int(h0 * sr):int(h1 * sr)]
        print(f"[lyric_viz] TIKTOK hook window: {h0:.1f}s -> {h1:.1f}s "
              f"({h1 - h0:.0f}s clip)")
    elif args.window:
        w0, w1 = (float(v) for v in args.window.split(":"))
        w1 = min(w1, duration)
        f0, f1 = int(w0 * args.fps), int(w1 * args.fps)
        pcm = pcm[int(w0 * sr):int(w1 * sr)]
        print(f"[lyric_viz] TEST window: {w0:.1f}s -> {w1:.1f}s")

    def frames():
        for i in range(f0, f1):
            yield r.frame(i, i / args.fps, analysis)
            if i > f0 and (i - f0) % (args.fps * 10) == 0:
                print(f"[lyric_viz] {i - f0}/{f1 - f0} frames")

    encode(out, frames(), args.fps, pcm, sr, W, H)
    if style.strobe:
        print("[lyric_viz] WARNING: this style strobes on the drop - publish "
              "with a flash/photosensitivity warning in the caption.")
    print(f"[lyric_viz] done in {time.time() - t0:.1f}s -> {out} "
          f"({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
