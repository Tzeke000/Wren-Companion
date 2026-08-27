"""scripts/make_clip.py — cut a Discord-sized clip from a rendered video.

Why: Discord DM cap is ~10MB; full lyric_viz renders are 100-200MB. This trims
a segment (default: the LOUDEST 30s window, i.e. the drop) and re-encodes to
fit under the cap. PyAV only — no ffmpeg.exe on this machine.

Usage:
  .venv/Scripts/python.exe scripts/make_clip.py --in render.mp4 --out clip.mp4
      [--dur 30] [--start SECONDS] [--max-mb 9.0]

If --start is omitted, the highest-RMS window of the audio track is used.
"""
from __future__ import annotations

import argparse
import sys

import av
import numpy as np


def find_loudest_start(path: str, dur: float) -> float:
    """Return start time (s) of the loudest `dur`-second audio window."""
    con = av.open(path)
    astream = con.streams.audio[0]
    sr = astream.rate or 48000
    chunks = []
    for frame in con.decode(astream):
        arr = frame.to_ndarray()          # (ch, n) or (n,)
        if arr.ndim == 2:
            arr = arr.mean(axis=0)
        chunks.append(arr.astype(np.float32))
    con.close()
    audio = np.concatenate(chunks)
    hop = sr // 2                          # 0.5s resolution
    win = int(dur * sr)
    if len(audio) <= win:
        return 0.0
    # energy per hop, then rolling sum over the window
    n_hops = (len(audio) - win) // hop
    best_i, best_e = 0, -1.0
    sq = audio ** 2
    csum = np.concatenate([[0.0], np.cumsum(sq)])
    for i in range(n_hops + 1):
        s = i * hop
        e = float(csum[s + win] - csum[s])
        if e > best_e:
            best_e, best_i = e, s
    # start ~1s before the peak window so the drop has a breath of build
    return max(0.0, best_i / sr - 1.0)


def cut(src: str, dst: str, start: float, dur: float, max_mb: float) -> None:
    from fractions import Fraction

    end = start + dur
    probe = av.open(src)
    fps = float(probe.streams.video[0].average_rate)
    w, h = probe.streams.video[0].width, probe.streams.video[0].height
    a_rate = probe.streams.audio[0].rate
    probe.close()

    # budget: leave ~15% for audio+container
    v_bps = int(max_mb * 8 * 1024 * 1024 * 0.85 / dur)

    out = av.open(dst, "w")
    ov = out.add_stream("h264", rate=round(fps))
    ov.width, ov.height = w, h
    ov.pix_fmt = "yuv420p"
    ov.bit_rate = v_bps
    ov.options = {"preset": "medium", "profile": "high"}
    ov.codec_context.time_base = Fraction(1, round(fps))
    oa = out.add_stream("aac", rate=a_rate)
    oa.bit_rate = 128_000
    oa.codec_context.time_base = Fraction(1, a_rate)

    # pass 1: video, explicit frame-counter pts
    con = av.open(src)
    vs = con.streams.video[0]
    con.seek(int(start * av.time_base))
    got_v = 0
    for frame in con.decode(vs):
        t = frame.time
        if t is None or t < start:
            continue
        if t > end:
            break
        nf = frame.reformat(format="yuv420p")
        nf.pts = got_v
        nf.time_base = Fraction(1, round(fps))
        got_v += 1
        for p in ov.encode(nf):
            out.mux(p)
    for p in ov.encode():
        out.mux(p)
    con.close()

    # pass 2: audio, sample-counter pts
    con = av.open(src)
    ast = con.streams.audio[0]
    con.seek(int(start * av.time_base))
    resampler = av.AudioResampler(format="fltp", layout="stereo", rate=a_rate)
    samples = 0
    for frame in con.decode(ast):
        t = frame.time
        if t is None or t < start:
            continue
        if t > end:
            break
        for rf in resampler.resample(frame):
            rf.pts = samples
            rf.time_base = Fraction(1, a_rate)
            samples += rf.samples
            for p in oa.encode(rf):
                out.mux(p)
    for p in oa.encode():
        out.mux(p)
    con.close()
    out.close()
    print(f"[make_clip] {got_v} video frames, {samples} audio samples -> {dst}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--dur", type=float, default=30.0)
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--max-mb", type=float, default=9.0)
    a = ap.parse_args()
    start = a.start if a.start is not None else find_loudest_start(a.src, a.dur)
    print(f"[make_clip] start={start:.1f}s dur={a.dur:.0f}s")
    cut(a.src, a.dst, start, a.dur, a.max_mb)


if __name__ == "__main__":
    main()
