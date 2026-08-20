"""Object lock — detect rarely, track cheaply, re-validate honestly.

Built 2026-08-20 at Zeke's ask: research how the big shops do object tracking
and build it. The canonical industry pipeline is TRACKING-BY-DETECTION:

    1. ACQUIRE  — expensive detector (OWL-ViT, ~90-176ms GPU on the 3060) runs
                  ONCE to find the thing and hand over a bbox.
    2. TRACK    — a light single-object tracker follows that bbox frame-to-frame
                  for cheap. Here: OpenCV TrackerVit (transformer tracker,
                  opencv_zoo vittrack ONNX, measured ~12ms CPU per update on
                  this machine — ~10x faster than OWL-ViT and ZERO GPU).
    3. REVALIDATE — correlation/template trackers DRIFT. Every REVAL_S seconds
                  (or on tracker failure) the detector runs again and is
                  compared to the tracked box by IoU. Agreement -> keep lock
                  (re-seat on the detector's box). Disagreement -> trust the
                  DETECTOR (it has semantics; the tracker only has appearance).
                  Detector says gone, twice in a row -> the lock DIES and the
                  caller sees a MISS. No zombie locks that follow a cushion
                  around because it once looked like a mug.

The honesty rules from visual_attention.py carry over: a stale frame is a MISS,
an unavailable tracker is a MISS, and this module never fabricates a bbox.

Integration: visual_attention._match_object() calls resolve() below; on any
failure it falls back to its original pure-OWL-ViT path, so this layer can
never make object tracking WORSE than it was this morning.

No second camera handle — frames come from frame_store.get_buffered_frame()
only (same discipline as everything else that sees).

Portability note for the server (profiles/server/perception_stack_port.md):
this module is pure OpenCV+numpy on CPU. It ports with zero changes; only the
vittrack.onnx file (714KB, models/trackers/) travels with it.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()

# One live lock at a time (the PTZ head can only chase one thing anyway).
_state: dict[str, Any] = {
    "target_id": None,      # "object:mug"
    "tracker": None,        # cv2.TrackerVit instance
    "kind": None,           # "vit" | "mil"
    "box": None,            # last tracked (x, y, w, h)
    "score": 0.0,           # tracker confidence (VitTrack tracking score)
    "acquired_ts": 0.0,
    "last_update_ts": 0.0,
    "last_reval_ts": 0.0,
    "reval_misses": 0,      # consecutive detector misses at revalidation
    "updates": 0,
    "revals": 0,
    "reseats": 0,           # revalidations that corrected drift
    "last_frame_ts": 0.0,   # capture_ts of the frame last fed to the tracker
    "err": None,
}

REVAL_S = 3.0          # detector re-check cadence while locked
REVAL_MISS_LIMIT = 2   # consecutive detector misses that kill the lock
IOU_AGREE = 0.30       # detector-vs-tracker agreement threshold
MIN_TRACK_SCORE = 0.30 # below this VitTrack score, force a revalidation
_MODEL = Path(__file__).resolve().parent.parent / "models" / "trackers" / "vittrack.onnx"


def _log(msg: str) -> None:
    print(f"[object_lock] {msg}", file=sys.stderr, flush=True)


def _new_tracker():
    """VitTrack if the ONNX is present, TrackerMIL as the no-model fallback."""
    import cv2
    if _MODEL.exists():
        p = cv2.TrackerVit_Params()
        p.net = str(_MODEL)
        return cv2.TrackerVit_create(p), "vit"
    return cv2.TrackerMIL_create(), "mil"


def _iou(a, b) -> float:
    """IoU of two (x, y, w, h) boxes."""
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return float(inter / union) if union > 0 else 0.0


def _detect(want: str, frame, min_score: float) -> tuple | None:
    """One OWL-ViT pass -> (x, y, w, h, score) or None. The expensive call."""
    from brain import vector_owl
    r = vector_owl.detect(frame, [want], threshold=min_score, max_results=5, bgr=True)
    if not r.get("ok"):
        return None
    for o in (r.get("objects") or []):
        box = o.get("box") or []
        score = float(o.get("score") or 0.0)
        if len(box) >= 4 and score >= min_score:
            x1, y1, x2, y2 = [float(v) for v in box[:4]]
            return (x1, y1, max(1.0, x2 - x1), max(1.0, y2 - y1), score)
    return None


def _rec(box, score: float, resolver: str) -> dict:
    x, y, w, h = box
    return {"bbox": [int(x), int(y), int(x + w), int(y + h)],
            "confidence": float(score), "label": None, "resolver": resolver}


def drop(reason: str = "") -> None:
    """Kill the current lock (idempotent)."""
    with _LOCK:
        if _state["target_id"] is not None:
            _log(f"lock dropped for {_state['target_id']!r}"
                 + (f" ({reason})" if reason else ""))
        _state.update({"target_id": None, "tracker": None, "box": None,
                       "score": 0.0, "reval_misses": 0, "err": None})


def status() -> dict:
    with _LOCK:
        s = {k: v for k, v in _state.items() if k != "tracker"}
        s["locked"] = _state["tracker"] is not None
        s["model"] = _state["kind"]
        s["age_s"] = round(time.time() - _state["acquired_ts"], 1) if s["locked"] else None
        return s


def resolve(target_id: str, want: str, *, min_score: float = 0.10) -> dict | None:
    """The one entry point: return an insightface-shaped rec for the target,
    or None for an honest miss. Called from visual_attention._match_object at
    follow-loop rate (~2Hz). Cheap path is a single tracker.update()."""
    try:
        from brain import frame_store
        res = frame_store.get_buffered_frame(max_age_sec=2.0)
        if res.frame is None:
            return None  # stale/absent frame = miss, never a guess
        frame = res.frame
        now = time.time()

        with _LOCK:
            # ── no lock, or lock belongs to a different target: ACQUIRE ──
            if _state["tracker"] is None or _state["target_id"] != target_id:
                det = _detect(want, frame, min_score)
                if det is None:
                    return None
                x, y, w, h, score = det
                tracker, kind = _new_tracker()
                tracker.init(frame, (int(x), int(y), int(w), int(h)))
                _state.update({"target_id": target_id, "tracker": tracker,
                               "kind": kind, "box": (x, y, w, h), "score": score,
                               "acquired_ts": now, "last_update_ts": now,
                               "last_reval_ts": now, "reval_misses": 0,
                               "updates": 0, "revals": 0, "reseats": 0,
                               "last_frame_ts": res.capture_ts, "err": None})
                _log(f"acquired {target_id!r} ({kind}) box={_state['box']} "
                     f"det_score={score:.2f}")
                return _rec((x, y, w, h), score, "owlvit+lock")

            # ── locked: cheap tracker update (skip if same frame as last time) ──
            tracker = _state["tracker"]
            if res.capture_ts > _state["last_frame_ts"]:
                ok, box = tracker.update(frame)
                _state["last_frame_ts"] = res.capture_ts
                _state["updates"] += 1
                if ok:
                    tscore = 1.0
                    if _state["kind"] == "vit":
                        try:
                            tscore = float(tracker.getTrackingScore())
                        except Exception:
                            pass
                    _state["box"] = tuple(float(v) for v in box)
                    _state["score"] = tscore
                    _state["last_update_ts"] = now
                else:
                    _state["score"] = 0.0  # forces revalidation below

            # ── revalidation: on cadence, on weak score, or on update failure ──
            due = (now - _state["last_reval_ts"]) >= REVAL_S
            weak = _state["score"] < MIN_TRACK_SCORE
            if due or weak:
                _state["last_reval_ts"] = now
                _state["revals"] += 1
                det = _detect(want, frame, min_score)
                if det is None:
                    _state["reval_misses"] += 1
                    if _state["reval_misses"] >= REVAL_MISS_LIMIT:
                        drop("detector lost it %d revalidations in a row"
                             % REVAL_MISS_LIMIT)
                        return None
                    # one detector miss: tracker box survives on probation
                else:
                    _state["reval_misses"] = 0
                    dx, dy, dw, dh, dscore = det
                    if _iou(_state["box"], (dx, dy, dw, dh)) < IOU_AGREE:
                        # drift: trust the detector, re-seat the tracker
                        tracker, kind = _new_tracker()
                        tracker.init(frame, (int(dx), int(dy), int(dw), int(dh)))
                        _state.update({"tracker": tracker, "kind": kind,
                                       "box": (dx, dy, dw, dh), "score": dscore,
                                       "reseats": _state["reseats"] + 1})
                    else:
                        _state["box"] = (dx, dy, dw, dh)
                        _state["score"] = max(_state["score"], dscore)

            if _state["box"] is None or _state["score"] <= 0.0:
                return None
            return _rec(_state["box"], _state["score"],
                        f"lock:{_state['kind']}")
    except Exception as e:  # any failure = honest miss + let caller fall back
        with _LOCK:
            _state["err"] = repr(e)
        _log(f"resolve failed for {target_id!r}: {e!r}")
        return None
