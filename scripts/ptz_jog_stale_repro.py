"""Reproduce the 10-02 stale-register condition ON PURPOSE (for ptz_predict's live proof).

Jogs the PIXY head with a short HID vector burst — the same mechanism the servo uses —
then sends ONE zero (a real stop; never a stream of zeros, see the attention_smooth
scar). Jog streams do NOT update the WinRT position registers, so afterwards the
registers still read home while the view is off. ptz_predict check_home must say
off_home; resync must bring it back.

Run (servo STOPPED first):  .venv\\Scripts\\python.exe scripts\\ptz_jog_stale_repro.py [x] [seconds]
Default x=10 for 0.8 s  (~0.85 deg/s per unit => ~7 deg).
"""
import struct
import sys
import time

import hid

VID, PID, REPORT = 0x328F, 0x00C0, 32


def vec(x, y, z=0.0):
    payload = [0x09, 0x63, 0x01, 0x20, 0x00, 0x0C, 0x00, 0x0C, *struct.pack("<fff", x, y, z)]
    return bytes(list(payload) + [0] * (REPORT - len(payload)))


x = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 0.8
d = hid.device()
d.open(VID, PID)
try:
    t0 = time.time()
    n = 0
    while time.time() - t0 < secs:
        d.write(vec(x, 0.0))
        n += 1
        time.sleep(0.04)
    d.write(vec(0.0, 0.0))  # ONE zero = a real stop
    print(f"jogged x={x} for {secs}s ({n} writes), one stop sent")
finally:
    d.close()
