"""Offline tests for brain/person_track.py — no camera, no GPU.

YOLOX is monkeypatched with synthetic detections; ByteTrack and the binding /
aim logic run for real. Covers: identity binds only via face, association
carries it through a face-less stretch, rebind follows the face, unknown
faces bind nothing, lost track goes honestly None.

Run: .venv/Scripts/python.exe scripts/test_person_track.py
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, ".")
from brain import person_track, yolox_person  # noqa: E402

W, H = 640, 480
FRAME = np.zeros((H, W, 3), dtype=np.uint8)


def det(cx, cy, w=80, h=200, score=0.9):
    return {"bbox": [int(cx - w / 2), int(cy - h / 2),
                     int(cx + w / 2), int(cy + h / 2)],
            "confidence": score, "resolver": "yolox_body"}


def face_at(cx, cy, label="zeke", s=20):
    return {"person_id": label,
            "bbox": [cx - s, cy - s, cx + s, cy + s]}


_dets: list[dict] = []
yolox_person.detect = lambda frame, min_score=0.1: list(_dets)  # monkeypatch


def run_step(dets, faces, ts):
    global _dets
    _dets = dets
    return person_track.step(FRAME, faces, ts)


def test_face_binds_and_body_continues():
    person_track.reset()
    ts = 1000.0
    # 5 frames with a face: identity established
    for i in range(5):
        ts += 1 / 30
        run_step([det(300 + 4 * i, 240)], [face_at(300 + 4 * i, 170)], ts)
    off = person_track.target_offset("zeke")
    assert off is not None and off["source"] == "face", off
    # 30 face-less frames (1.0s, past _FACE_FRESH_S): body must carry the id
    for i in range(30):
        ts += 1 / 30
        run_step([det(320 + 5 * i, 240)], [], ts)
    off = person_track.target_offset("zeke")
    assert off is not None, "identity lost during face-less stretch"
    assert off["source"] == "body", off
    assert off["dx"] > 0.2, f"aim should be right of center, got {off['dx']}"
    print(f"PASS face binds, body continues (dx={off['dx']:.2f}, "
          f"vx={off['vx']:+.2f}/s, track={off['track_id']})")


def test_kf_velocity_sign_and_size():
    person_track.reset()
    ts = 2000.0
    # constant walk right at 6px/frame at 30fps = 180 px/s = 0.5625 offset/s
    for i in range(30):
        ts += 1 / 30
        run_step([det(100 + 6 * i, 240)],
                 [face_at(100 + 6 * i, 170)] if i < 3 else [], ts)
    off = person_track.target_offset("zeke")
    assert off is not None
    assert off["vx"] > 0.2, f"rightward walk must give vx>0, got {off['vx']}"
    assert abs(off["vy"]) < 0.15, f"no vertical motion, got vy={off['vy']}"
    print(f"PASS KF velocity sane (vx={off['vx']:+.3f}/s expect ~+0.56, "
          f"vy={off['vy']:+.3f}/s)")


def test_unknown_face_binds_nothing():
    person_track.reset()
    ts = 3000.0
    for i in range(5):
        ts += 1 / 30
        run_step([det(300, 240)], [face_at(300, 170, label="person3")], ts)
    assert person_track.target_offset("person3") is None
    assert person_track.status()["bindings"] == {}
    print("PASS unknown face ('person3') binds nothing")


def test_rebind_follows_face():
    person_track.reset()
    ts = 4000.0
    # two people; face says zeke is the LEFT one
    for i in range(6):
        ts += 1 / 30
        run_step([det(200, 240), det(440, 240)], [face_at(200, 170)], ts)
    tid_left = person_track.target_offset("zeke")["track_id"]
    # face-less: id continues on left track
    for i in range(10):
        ts += 1 / 30
        run_step([det(200, 240), det(440, 240)], [], ts)
    assert person_track.target_offset("zeke")["track_id"] == tid_left
    # face now shows up on the RIGHT body (I was wrong / they swapped
    # while both were occluded): face evidence must MOVE the binding.
    for i in range(3):
        ts += 1 / 30
        run_step([det(200, 240), det(440, 240)], [face_at(440, 170)], ts)
    off = person_track.target_offset("zeke")
    assert off["track_id"] != tid_left, "face evidence must outrank association"
    assert off["dx"] > 0, "should now aim right"
    print(f"PASS rebind follows the face ({tid_left} -> {off['track_id']})")


def test_lost_goes_none():
    person_track.reset()
    ts = 5000.0
    for i in range(5):
        ts += 1 / 30
        run_step([det(300, 240)], [face_at(300, 170)], ts)
    # gone for longer than track_buffer (60 frames) + face staleness
    for i in range(80):
        ts += 1 / 30
        run_step([], [], ts)
    assert person_track.target_offset("zeke") is None, \
        "must go honestly None when track dies and face is stale"
    print("PASS lost target goes honestly None")


def test_arms_up_does_not_move_aim():
    """Referee test 2026-08-25 ('hands up and your head went WILD'): raising
    arms grows the box UPWARD — top leaps, bottom (feet) stays planted. The
    aim point must ride the remembered head height above the BOTTOM edge, and
    the fed-forward vy must be the bottom-edge rate (~0), not the box-center
    rate (which climbs at vh/2 during the gesture)."""
    person_track.reset()
    ts = 6000.0
    # establish identity + face-height memory: box 140..340, face at y=170
    for i in range(6):
        ts += 1 / 30
        run_step([det(320, 240, w=80, h=200)], [face_at(320, 170)], ts)
    base = person_track.target_offset("zeke")
    # face gone: stand still long enough for the face to go stale (>0.7s)
    for i in range(21):
        ts += 1 / 30
        run_step([det(320, 240, w=80, h=200)], [], ts)
    # then arms go up FAST: bottom pinned at 340, height 200 -> 320
    off = None
    for i in range(12):
        ts += 1 / 30
        hh = 200 + 10 * (i + 1)
        run_step([det(320, 340 - hh / 2.0, w=80, h=hh)], [], ts)
        off = person_track.target_offset("zeke")
    assert off is not None and off["source"] == "body", off
    drift = abs(off["dy"] - base["dy"])
    assert drift < 0.08, f"aim chased the growing box: dy drift {drift:.3f}"
    assert abs(off["vy"]) < 0.15, \
        f"shape morph leaked into velocity: vy={off['vy']:+.3f}/s"
    # sanity: old top-anchored math WOULD have moved (proves the test bites)
    hh = 320.0
    old_ay = (340 - hh) + hh * 0.18
    old_dy = (old_ay - H / 2.0) / (H / 2.0)
    assert abs(old_dy - base["dy"]) > 0.15, "test would not catch a regression"
    print(f"PASS arms-up: aim dy drift {drift:.3f}, vy={off['vy']:+.3f}/s "
          f"(old aim would have drifted {abs(old_dy - base['dy']):.2f})")


if __name__ == "__main__":
    test_face_binds_and_body_continues()
    test_kf_velocity_sign_and_size()
    test_unknown_face_binds_nothing()
    test_rebind_follows_face()
    test_lost_goes_none()
    test_arms_up_does_not_move_aim()
    print("ALL PASS")
