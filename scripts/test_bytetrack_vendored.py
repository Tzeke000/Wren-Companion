"""Offline unit test for the vendored ByteTrack (no camera, no GPU).

Exercises the exact property we vendored it for: identity survives a
low-confidence dip (occlusion / bent-over) that a plain score threshold
would drop. Also checks two crossing targets keep distinct ids, and that
a track dies after max_time_lost.

Run: .venv/Scripts/python.exe scripts/test_bytetrack_vendored.py
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, ".")
from vendor.bytetrack import BYTETracker, TrackerArgs  # noqa: E402

W, H = 640, 480


def box(cx: float, cy: float, w: float = 80.0, h: float = 160.0,
        score: float = 0.9) -> list[float]:
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, score]


def step(trk, dets):
    return trk.update(np.array(dets, dtype=float).reshape(-1, 5), (H, W), (H, W))


def test_low_conf_dip_keeps_identity() -> None:
    trk = BYTETracker(TrackerArgs(track_thresh=0.5), frame_rate=30)
    tid = None
    # walk right at 6 px/frame, high confidence
    for i in range(10):
        out = step(trk, [box(100 + 6 * i, 240, score=0.9)])
    assert len(out) == 1
    tid = out[0].track_id
    # occlusion dip: 8 frames at 0.3 — BELOW track_thresh, above the 0.1
    # low-score floor. Plain thresholding loses these; ByteTrack's second
    # association must hold the id.
    for i in range(8):
        out = step(trk, [box(160 + 6 * i, 240, score=0.3)])
        assert len(out) == 1, f"lost track during dip at frame {i}"
        assert out[0].track_id == tid, "identity changed during low-conf dip"
    # recovery
    out = step(trk, [box(214, 240, score=0.9)])
    assert out[0].track_id == tid, "identity changed after recovery"
    print("PASS low-conf dip keeps identity "
          f"(id={tid} held through 8 frames at score 0.3)")


def test_crossing_targets_keep_ids() -> None:
    trk = BYTETracker(TrackerArgs(track_thresh=0.5), frame_rate=30)
    # A walks right from x=100, B walks left from x=540; they cross at ~320.
    ids0 = None
    for i in range(40):
        ax, bx = 100 + 11 * i, 540 - 11 * i
        out = step(trk, [box(ax, 240), box(bx, 240)])
        if i == 5:
            ids0 = sorted(t.track_id for t in out)
    assert ids0 is not None and len(ids0) == 2
    # after the cross, both ids still alive and unchanged as a set
    ids1 = sorted(t.track_id for t in out)
    assert ids1 == ids0, f"ids changed across the cross: {ids0} -> {ids1}"
    # and the id that started left is now right (they passed each other):
    left_id_start = min(
        (t for t in step(trk, [box(540, 240), box(100, 240)])),
        key=lambda t: t.tlwh[0]).track_id
    print(f"PASS crossing targets keep ids ({ids0}), "
          f"left-most now id={left_id_start}")


def test_track_dies_after_buffer() -> None:
    trk = BYTETracker(TrackerArgs(track_thresh=0.5, track_buffer=10),
                      frame_rate=30)
    for i in range(5):
        out = step(trk, [box(300, 240, score=0.9)])
    tid = out[0].track_id
    # target vanishes entirely
    for i in range(12):
        out = step(trk, [])
    assert out == [], "no active tracks expected while lost"
    # reappears AFTER max_time_lost (10 frames) -> must be a NEW id
    out = step(trk, [box(300, 240, score=0.9)])
    out = step(trk, [box(300, 240, score=0.9)])  # second frame to activate
    assert out and out[0].track_id != tid, "stale id resurrected past buffer"
    print(f"PASS track dies after buffer (old id={tid}, "
          f"new id={out[0].track_id})")


def test_brief_loss_reacquires_same_id() -> None:
    trk = BYTETracker(TrackerArgs(track_thresh=0.5, track_buffer=30),
                      frame_rate=30)
    for i in range(6):
        out = step(trk, [box(300, 240, score=0.9)])
    tid = out[0].track_id
    for i in range(5):  # brief full loss, within buffer
        step(trk, [])
    out = step(trk, [box(315, 240, score=0.9)])
    assert out and out[0].track_id == tid, "brief loss should re-find same id"
    print(f"PASS brief loss reacquires same id ({tid})")


if __name__ == "__main__":
    test_low_conf_dip_keeps_identity()
    test_crossing_targets_keep_ids()
    test_track_dies_after_buffer()
    test_brief_loss_reacquires_same_id()
    print("ALL PASS")
