"""Measure how far the head ACTUALLY moved between two saved frames.

Phase-correlate two eyes_debug frames and convert the scene shift to degrees,
using the same constant the servo's visual odometry uses. This is the only
honest check on 'did the commanded absolute move happen' — the device's own
bearing() readback echoes the register, not the world.
"""
import sys
import cv2
import numpy as np

HFOV_DEG = 68.0

a_path, b_path = sys.argv[1], sys.argv[2]
cmd_pan = float(sys.argv[3]) if len(sys.argv) > 3 else None
cmd_tilt = float(sys.argv[4]) if len(sys.argv) > 4 else None

A = cv2.imread(a_path)
B = cv2.imread(b_path)
if A is None or B is None:
    raise SystemExit("could not read one of the frames")

# crop the status banner off the top so text doesn't anchor the correlation
A, B = A[24:, :], B[24:, :]
h, w = A.shape[:2]
deg_per_px = HFOV_DEG / float(w)

ga = np.float32(cv2.cvtColor(A, cv2.COLOR_BGR2GRAY)) / 255.0
gb = np.float32(cv2.cvtColor(B, cv2.COLOR_BGR2GRAY)) / 255.0
win = cv2.createHanningWindow((w, h), cv2.CV_32F)
(sx, sy), resp = cv2.phaseCorrelate(ga, gb, win)

print(f"frame size      : {w}x{h}   deg/px = {deg_per_px:.4f}")
print(f"scene shift     : dx={sx:+.1f}px  dy={sy:+.1f}px   response={resp:.3f}")
print(f"measured motion : pan={sx*deg_per_px:+.1f} deg   tilt={sy*deg_per_px:+.1f} deg")
if cmd_pan is not None:
    print(f"commanded       : pan={cmd_pan:+.1f} deg   tilt={cmd_tilt:+.1f} deg")
    ep = abs(abs(sx * deg_per_px) - abs(cmd_pan))
    print(f"pan |error|     : {ep:.1f} deg")
    if resp < 0.10:
        print("VERDICT         : correlation too weak — measurement not trustworthy")
    elif ep <= max(2.0, abs(cmd_pan) * 0.30):
        print("VERDICT         : head MOVED about as commanded")
    elif abs(sx * deg_per_px) < 1.0:
        print("VERDICT         : head DID NOT MOVE (command had no physical effect)")
    else:
        print("VERDICT         : head moved, but NOT by the commanded amount")
