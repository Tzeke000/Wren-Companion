"""Shrink a video under Discord's attachment cap. PyAV only (no ffmpeg.exe here).

    python scripts/shrink_for_discord.py in.mp4 out.mp4 [--mb 9.5] [--width 405]

Why this exists: renders routinely land at 20-30MB and the DM cap is ~10MB, and
this machine has no ffmpeg binary — only PyAV. Re-rendering smaller costs
minutes; a transcode costs seconds. Retries at progressively higher CRF until
it fits, so it never silently hands back an over-cap file.
"""
import argparse
import os
import sys

import av
import cv2


def transcode(src: str, dst: str, width: int, crf: int) -> int:
    inp = av.open(src)
    out = av.open(dst, "w")
    v_in = inp.streams.video[0]
    a_in = inp.streams.audio[0] if inp.streams.audio else None

    sw, sh = v_in.codec_context.width, v_in.codec_context.height
    w = width - (width % 2)
    h = int(round(sh * (w / sw)))
    h -= h % 2

    v_out = out.add_stream("h264", rate=v_in.average_rate)
    v_out.width, v_out.height = w, h
    v_out.pix_fmt = "yuv420p"
    v_out.options = {"crf": str(crf), "preset": "slow"}
    a_out = out.add_stream("aac", rate=a_in.rate) if a_in else None
    if a_out is not None:
        a_out.bit_rate = 96_000

    for packet in inp.demux():
        if packet.dts is None:
            continue
        if packet.stream == v_in:
            for frame in packet.decode():
                img = cv2.resize(frame.to_ndarray(format="bgr24"), (w, h),
                                 interpolation=cv2.INTER_AREA)
                nf = av.VideoFrame.from_ndarray(img, format="bgr24")
                nf.pts = None
                for p in v_out.encode(nf):
                    out.mux(p)
        elif a_in is not None and packet.stream == a_in:
            for frame in packet.decode():
                frame.pts = None
                for p in a_out.encode(frame):
                    out.mux(p)

    for p in v_out.encode():
        out.mux(p)
    if a_out is not None:
        for p in a_out.encode():
            out.mux(p)
    out.close()
    inp.close()
    return os.path.getsize(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--mb", type=float, default=9.5, help="target cap in MB")
    ap.add_argument("--width", type=int, default=405)
    args = ap.parse_args()

    cap = args.mb * 1024 * 1024
    for crf in (24, 28, 32, 36):
        size = transcode(args.src, args.dst, args.width, crf)
        print(f"[shrink] crf={crf} -> {size / 1024 / 1024:.2f} MB")
        if size <= cap:
            print(f"[shrink] OK: {args.dst} ({size / 1024 / 1024:.2f} MB, "
                  f"cap {args.mb} MB)")
            return 0
    print(f"[shrink] STILL OVER CAP at crf=36 — lower --width", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
