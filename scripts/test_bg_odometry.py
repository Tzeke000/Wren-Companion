"""Offline tests for the background-only camera-motion estimator
(_bg_camera_shift in tools/system/attention_smooth_tool.py, 2026-08-25,
modeled on BoT-SORT tracker/gmc.py).

Proves the property the referee test demanded: a person moving INSIDE the
masked region must not perturb the measured camera motion, and when no clean
background remains the answer is honestly None (never a guess).

Run: .venv/Scripts/python.exe scripts/test_bg_odometry.py
"""
from __future__ import annotations

import sys

import cv2
import numpy as np

sys.path.insert(0, ".")
from tools.system.attention_smooth_tool import (  # noqa: E402
    _ODO_H, _ODO_W, _bg_camera_shift)

RNG = np.random.default_rng(7)
# textured background (goodFeaturesToTrack needs corners, not noise soup):
# random blobs on a gradient.
BG = (np.outer(np.linspace(40, 120, _ODO_H),
               np.ones(_ODO_W)).astype(np.uint8))
for _ in range(220):
    x, y = int(RNG.integers(0, _ODO_W)), int(RNG.integers(0, _ODO_H))
    cv2.rectangle(BG, (x, y), (x + int(RNG.integers(4, 18)),
                               y + int(RNG.integers(4, 18))),
                  int(RNG.integers(0, 255)), -1)


def shifted(img, dx, dy):
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, M, (_ODO_W, _ODO_H), borderMode=cv2.BORDER_REFLECT)


def draw_person(img, x, y, w=90, h=170):
    out = img.copy()
    cv2.rectangle(out, (x, y), (x + w, y + h), 200, -1)
    cv2.rectangle(out, (x + 20, y + 20), (x + 60, y + 60), 90, -1)  # texture
    return out


def base_mask():
    m = np.full((_ODO_H, _ODO_W), 255, np.uint8)
    b = max(1, int(0.02 * _ODO_H))
    m[:b, :] = 0
    m[-b:, :] = 0
    m[:, :max(1, int(0.02 * _ODO_W))] = 0
    m[:, -max(1, int(0.02 * _ODO_W)):] = 0
    return m


def mask_box(m, x, y, w, h, pad=0.10):
    px, py = int(pad * w), int(pad * h)
    m2 = m.copy()
    m2[max(0, y - py):min(_ODO_H, y + h + py),
       max(0, x - px):min(_ODO_W, x + w + px)] = 0
    return m2


def test_pure_camera_shift():
    prev, cur = BG, shifted(BG, 6.0, -3.0)
    sx, sy, inl, feat = _bg_camera_shift(prev, cur, base_mask())
    assert sx is not None, "flat case must resolve"
    assert abs(sx - 6.0) < 0.5 and abs(sy + 3.0) < 0.5, (sx, sy)
    print(f"PASS pure shift: ({sx:+.2f},{sy:+.2f}) expect (+6,-3), "
          f"{inl}/{feat} inliers")


def test_moving_person_masked_out():
    # camera shifts (+6,-3); person walks the OPPOSITE way (-14,+8)
    prev = draw_person(BG, 110, 40)
    cur = draw_person(shifted(BG, 6.0, -3.0), 96, 48)
    m = mask_box(mask_box(base_mask(), 110, 40, 90, 170), 96, 48, 90, 170)
    sx, sy, inl, feat = _bg_camera_shift(prev, cur, m)
    assert sx is not None
    assert abs(sx - 6.0) < 0.5 and abs(sy + 3.0) < 0.5, \
        f"person motion leaked into camera estimate: ({sx:+.2f},{sy:+.2f})"
    # and the same scene WITHOUT camera motion must read ~zero while he moves
    prev2 = draw_person(BG, 110, 40)
    cur2 = draw_person(BG, 96, 48)
    sx2, sy2, *_ = _bg_camera_shift(prev2, cur2, m)
    assert sx2 is not None and abs(sx2) < 0.3 and abs(sy2) < 0.3, \
        f"gesture with parked head must read ~0, got ({sx2:+.2f},{sy2:+.2f})"
    print(f"PASS masked person: shift ({sx:+.2f},{sy:+.2f}) expect (+6,-3); "
          f"parked+gesture reads ({sx2:+.2f},{sy2:+.2f})")


def test_no_background_goes_none():
    # person fills the frame -> nothing clean to measure -> honest None
    m = mask_box(base_mask(), 0, 0, _ODO_W, _ODO_H, pad=0.0)
    sx, sy, inl, feat = _bg_camera_shift(BG, shifted(BG, 6.0, -3.0), m)
    assert sx is None, f"must refuse to guess, got ({sx},{sy})"
    print(f"PASS no-background: honestly None ({feat} features found)")


def test_external_hand_move_is_seen():
    # Zeke physically turns the head with the jog quiet: background shifts,
    # and the estimator MUST report it (the deleted quiet-gate ignored this —
    # that's why the head 'looked away' when he let go).
    prev, cur = BG, shifted(BG, -9.0, 4.0)
    sx, sy, *_ = _bg_camera_shift(prev, cur, base_mask())
    assert sx is not None and abs(sx + 9.0) < 0.5 and abs(sy - 4.0) < 0.5, (sx, sy)
    print(f"PASS hand-move seen: ({sx:+.2f},{sy:+.2f}) expect (-9,+4)")


if __name__ == "__main__":
    test_pure_camera_shift()
    test_moving_person_masked_out()
    test_no_background_goes_none()
    test_external_hand_move_is_seen()
    print("ALL PASS")
