"""ByteTrack — multi-object tracking by associating every detection box.

Vendored 2026-08-25 from github.com/FoundationVision/ByteTrack (MIT, LICENSE
in this directory), tracker module only — per the 08-25 handoff: "vendor the
tracker file (Kalman + association); do NOT run its setup.py" (it pulls
pycocotools + cython_bbox builds).

Patches (each file's header lists its own):
  * lap.lapjv          -> scipy.optimize.linear_sum_assignment
  * cython_bbox        -> pure-numpy bbox_ious (same +1 pixel convention)
  * torch              -> removed (we always feed Nx5 numpy)
  * np.float           -> float (numpy >= 1.24)
  * added TrackerArgs  -> stands in for the upstream argparse namespace

What this is FOR (and not): ByteTrack replaces the SEEING half — identity
kept across frames by refusing to discard low-confidence detections (the
occluded / bent-over / head-down case, exactly where InsightFace + raw YOLOX
thresholding lose Zeke). It has NO opinion about moving a gimbal; the
STEERING half stays ours (attention_smooth / attention_follow).

Feed it YOLOX person detections as an Nx5 numpy array [x1,y1,x2,y2,score]
in FRAME pixels, with img_info == img_size (we do our own preprocessing, so
the internal rescale becomes a no-op):

    from vendor.bytetrack import BYTETracker, TrackerArgs
    trk = BYTETracker(TrackerArgs(track_thresh=0.5), frame_rate=30)
    tracks = trk.update(dets, (h, w), (h, w))
    for t in tracks:  # STrack
        t.track_id, t.tlwh, t.score

Offline unit test: scripts/test_bytetrack_vendored.py
"""
from .byte_tracker import BYTETracker, STrack, TrackerArgs  # noqa: F401
from .basetrack import TrackState  # noqa: F401
