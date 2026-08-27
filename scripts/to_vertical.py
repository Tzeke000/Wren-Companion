"""Convert a landscape clip to 720x1280 vertical with blurred background pad.

Usage: python scripts/to_vertical.py in.mp4 out.mp4
PyAV-based (no ffmpeg.exe on this machine).
"""
import sys
import av
import cv2
import numpy as np

W, H = 720, 1280


def convert(src, dst):
    inp = av.open(src)
    out = av.open(dst, 'w')
    v_in = inp.streams.video[0]
    a_in = inp.streams.audio[0] if inp.streams.audio else None

    fps = v_in.average_rate
    v_out = out.add_stream('h264', rate=fps)
    v_out.width, v_out.height = W, H
    v_out.pix_fmt = 'yuv420p'
    v_out.options = {'crf': '21', 'preset': 'fast'}
    a_out = out.add_stream('aac', rate=a_in.rate) if a_in else None

    for packet in inp.demux():
        if packet.dts is None:
            continue
        if packet.stream == v_in:
            for frame in packet.decode():
                img = frame.to_ndarray(format='bgr24')
                fh, fw = img.shape[:2]
                # foreground: fit width
                scale = W / fw
                nh = int(fh * scale)
                fg = cv2.resize(img, (W, nh), interpolation=cv2.INTER_AREA)
                # background: fill 720x1280, heavy blur + darken
                bscale = max(W / fw, H / fh)
                bw, bh = int(fw * bscale), int(fh * bscale)
                bg = cv2.resize(img, (bw, bh))
                x0, y0 = (bw - W) // 2, (bh - H) // 2
                bg = bg[y0:y0 + H, x0:x0 + W]
                bg = cv2.GaussianBlur(bg, (0, 0), 25)
                bg = (bg * 0.45).astype(np.uint8)
                y = (H - nh) // 2
                bg[y:y + nh] = fg
                nf = av.VideoFrame.from_ndarray(bg, format='bgr24')
                nf.pts = None
                for p in v_out.encode(nf):
                    out.mux(p)
        elif a_in and packet.stream == a_in:
            for frame in packet.decode():
                frame.pts = None
                for p in a_out.encode(frame):
                    out.mux(p)

    for p in v_out.encode():
        out.mux(p)
    if a_out:
        for p in a_out.encode():
            out.mux(p)
    out.close()
    inp.close()


if __name__ == '__main__':
    convert(sys.argv[1], sys.argv[2])
    print('done:', sys.argv[2])
