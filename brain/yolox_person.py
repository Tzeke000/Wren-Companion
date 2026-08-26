"""YOLOX-s person detector — a BODY box, never an identity.

Built 2026-08-25. The problem it solves, in Zeke's words: *"you can't look
through my forehead and hair when my face is down."* When he bends over, the
face pipeline has nothing to work with, the target goes to `lost`, and my head
stops following a person who is still plainly in the room. A body detector
bridges that.

★ THE CONSTRAINT THAT SHAPES THIS WHOLE MODULE ★
Zeke asked, of a Spartan helmet the detector had scored at 0.509: *"do you
think that the spartan helmet is me? Or just that you think it's a person?"*
And then, correcting my phrasing: *"someone is not always me. I am someone,
but I'm Zeke to you."*

That is the failure mode this module must not have. YOLOX knows exactly one
relevant thing — that something is person-SHAPED. It has never heard of Zeke.
So a body box may only ever CONTINUE an identity the face pipeline already
established; it may never CREATE one. Concretely:

  - `bridge()` refuses unless a real face match for this target happened within
    _BRIDGE_MAX_GAP_S. Identity is granted by the face; the body only carries it
    forward across a short occlusion.
  - It refuses when more than one person is in frame — with two candidates
    there is no non-guessing way to say which body is his.
  - Records are tagged `resolver="yolox_body"` so nothing downstream can mistake
    a body continuation for a face recognition.

Measured 2026-08-25 on this machine: 14-25ms/frame on the 3060, and 0.625 on the
exact frame where OWL-ViT managed 0.127. Weights: models/yolox/yolox_s.pth
(YOLOX, Apache-2.0, vendored inference-only under vendor/yolox).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_WEIGHTS = _ROOT / "models" / "yolox" / "yolox_s.pth"

_INPUT = 640
_PERSON_CLASS = 0            # COCO class 0
_MIN_SCORE = 0.60            # see NOTE below
_NMS = 0.45
# NOTE on 0.60: on 2026-08-25 real Zeke scored 0.625-0.960 across four frames
# and his crested helmet-on-a-stand scored 0.509, so 0.60 separates them — on
# FIVE FRAMES. That is a starting point, not a calibration, and it is written
# here rather than buried so the next person to touch it knows how thin the
# evidence is.
_BRIDGE_MAX_GAP_S = 10.0     # how long a face-established identity may coast
_CACHE_TTL_S = 0.25          # observe() can run at 30Hz; detection is ~15ms

_LOCK = threading.Lock()
_model = None
_device = "cpu"
_load_error = ""
_cache: dict[str, Any] = {"ts": 0.0, "fp": None, "dets": []}


def _log(msg: str) -> None:
    print(f"[yolox_person] {msg}", flush=True)


def available() -> bool:
    return _WEIGHTS.is_file()


def _ensure_model():
    """Load once. Returns None (never raises) if anything is missing — a
    detector that cannot load must degrade to 'no body seen', not to an
    exception inside the attention loop."""
    global _model, _device, _load_error
    if _model is not None or _load_error:
        return _model
    with _LOCK:
        if _model is not None or _load_error:
            return _model
        try:
            import sys
            import torch
            if str(_ROOT) not in sys.path:
                sys.path.insert(0, str(_ROOT))
            from vendor.yolox import YOLOX, YOLOPAFPN, YOLOXHead
            if not _WEIGHTS.is_file():
                _load_error = f"weights missing at {_WEIGHTS}"
                _log(_load_error)
                return None
            try:
                from brain.gpu_load_log import logged_load as _logged_load
            except Exception:
                from contextlib import nullcontext as _logged_load  # fail-open
            _device = "cuda" if torch.cuda.is_available() else "cpu"
            with _logged_load(f"yolox-s:{_device}"):
                m = YOLOX(YOLOPAFPN(0.33, 0.50), YOLOXHead(80, 0.50))   # -s config
                ck = torch.load(_WEIGHTS, map_location="cpu", weights_only=False)
                missing, unexpected = m.load_state_dict(ck["model"], strict=False)
                if missing or unexpected:
                    # A partially-loaded detector produces confident nonsense.
                    _load_error = (f"weight mismatch ({len(missing)} missing, "
                                   f"{len(unexpected)} unexpected)")
                    _log(_load_error)
                    return None
                _model = m.eval().to(_device)
            _log(f"loaded YOLOX-s on {_device}")
        except Exception as e:  # noqa: BLE001
            _load_error = repr(e)
            _log(f"load failed (staying silent, no bodies): {e!r}")
            return None
    return _model


def detect(frame, *, min_score: float = _MIN_SCORE) -> list[dict]:
    """All person boxes in a BGR frame, best first. [] on any failure."""
    model = _ensure_model()
    if model is None or frame is None:
        return []
    now = time.time()
    # ★ THE CACHE IS KEYED ON THE FRAME, NOT ON TIME (fixed 2026-08-25, minutes
    # after writing it wrong). The first version cached only on a 250ms TTL, so
    # two DIFFERENT frames handed to detect() inside that window both got the
    # first frame's answer. My own test caught it red-handed: the empty room
    # came back with the identical box and an identical 0.6250395774841309 to
    # the head-down frame before it. In the live loop — one camera, calls close
    # together — it would have looked perfect. Wrong exactly where nobody
    # checks, right exactly where everybody looks.
    try:
        import numpy as _np
        _flat = frame.reshape(-1)
        _fp = hash(_flat[::max(1, _flat.size // 4096)].tobytes())
    except Exception:  # noqa: BLE001
        _fp = None
    if (_fp is not None and _cache.get("fp") == _fp
            and now - float(_cache.get("ts") or 0.0) < _CACHE_TTL_S):
        return [d for d in _cache["dets"] if d["confidence"] >= min_score]
    try:
        import cv2
        import numpy as np
        import torch

        h, w = frame.shape[:2]
        r = min(_INPUT / h, _INPUT / w)
        resized = cv2.resize(frame, (int(w * r), int(h * r)),
                             interpolation=cv2.INTER_LINEAR)
        canvas = np.full((_INPUT, _INPUT, 3), 114, dtype=np.uint8)
        canvas[:resized.shape[0], :resized.shape[1]] = resized
        x = torch.from_numpy(canvas.transpose(2, 0, 1)).float()
        with torch.no_grad():
            out = model(x.unsqueeze(0).to(_device))
        o = out[0].cpu().numpy()
        scores = o[:, 4] * o[:, 5 + _PERSON_CLASS]
        keep = scores > min(0.05, min_score)
        if not keep.any():
            _cache.update({"ts": now, "fp": _fp, "dets": []})
            return []
        b, sc = o[keep, :4], scores[keep]
        xywh = np.stack([(b[:, 0] - b[:, 2] / 2) / r, (b[:, 1] - b[:, 3] / 2) / r,
                         b[:, 2] / r, b[:, 3] / r], axis=1)
        # cv2 NMS rather than torchvision.ops.nms — torchvision is not installed
        # and installing it would risk the working torch build (repo scar).
        idx = cv2.dnn.NMSBoxes(xywh.tolist(), sc.tolist(),
                               min(0.05, min_score), _NMS)
        dets = []
        for i in np.array(idx).flatten() if len(idx) else []:
            x0, y0, bw, bh = (float(v) for v in xywh[i])
            dets.append({"bbox": [int(x0), int(y0), int(x0 + bw), int(y0 + bh)],
                         "confidence": float(sc[i]), "resolver": "yolox_body"})
        dets.sort(key=lambda d: -d["confidence"])
        _cache.update({"ts": now, "fp": _fp, "dets": dets})
        return [d for d in dets if d["confidence"] >= min_score]
    except Exception as e:  # noqa: BLE001
        _log(f"detect failed (treating as no bodies): {e!r}")
        return []


# ── SPATIAL CONTINUITY (2026-08-25, second pass) ────────────────────────────
# The first version simply refused whenever two people were in frame. Zeke
# asked what that meant, and saying it out loud exposed it: refusing is the
# SAFE answer, not the good one — in a two-person room the feature switches
# off exactly when it is most wanted. So this is the evidence-based version:
# a face was at a known place a moment ago, and BODIES DO NOT TELEPORT, so the
# body belonging to that face is the one nearest where the face just was.
#
# That is reasoning from evidence rather than guessing, and it still refuses
# when the evidence is genuinely absent (see _choose_body). Kept honest by
# three gates, because a wrong guess here does not announce itself — I would
# go on tracking the other person while calling him Zeke.
_MAX_DRIFT_FRAC = 0.18       # of frame width, per second of face-gap...
_MAX_DRIFT_FLOOR = 0.22      # ...plus this, so a fresh loss still has slack
_AMBIGUITY_MARGIN = 1.35     # runner-up must be this many times further away


def _choose_body(dets: list[dict], last_face_bbox, frame_w: int,
                 frame_h: int, gap_s: float) -> tuple[dict | None, str]:
    """Pick the body that belongs to a face last seen at `last_face_bbox`.

    Returns (record, reason). Pure — no model, no I/O — so the decision logic
    is testable without a camera or a GPU, which is the only reason I trust it.
    """
    if not dets:
        return None, "no bodies detected"
    if len(dets) == 1:
        return dets[0], "single body in frame"
    if not last_face_bbox or len(last_face_bbox) < 4 or frame_w <= 0:
        # Several candidates and no idea where the face was: this is exactly
        # the case the first version refused, and it is still right to refuse.
        return None, f"{len(dets)} bodies and no last-known face position"

    fx = (float(last_face_bbox[0]) + float(last_face_bbox[2])) / 2.0
    fy = (float(last_face_bbox[1]) + float(last_face_bbox[3])) / 2.0
    scored = []
    for d in dets:
        x1, y1, x2, y2 = (float(v) for v in d["bbox"][:4])
        # Compare against the body's HEAD region, not its centre: a standing
        # body's centre sits at the waist, which would make the nearest-body
        # test wrong by half a torso for everyone equally.
        hx = (x1 + x2) / 2.0
        hy = y1 + (y2 - y1) * 0.15
        dist = ((hx - fx) ** 2 + ((hy - fy) * 1.0) ** 2) ** 0.5 / float(frame_w)
        scored.append((dist, d))
    scored.sort(key=lambda s: s[0])
    best_d, best = scored[0]
    runner_d = scored[1][0]

    budget = _MAX_DRIFT_FLOOR + _MAX_DRIFT_FRAC * max(0.0, gap_s)
    if best_d > budget:
        # Nobody is near where the face was. Either he left and these are other
        # people, or the detector is looking at furniture. Not evidence.
        return None, (f"nearest body is {best_d:.2f} of a frame-width from the "
                      f"last face, past the {budget:.2f} budget for a "
                      f"{gap_s:.1f}s gap")
    if runner_d < best_d * _AMBIGUITY_MARGIN:
        # Two bodies about equally close — standing side by side, say. The
        # spatial evidence does not distinguish them, so I have no answer, and
        # picking the marginally closer one would be a coin flip wearing a
        # number.
        return None, (f"ambiguous: nearest {best_d:.2f} vs runner-up "
                      f"{runner_d:.2f} — too close to call")
    rec = dict(best)
    rec["match_dist_frac"] = round(best_d, 3)
    rec["runner_up_frac"] = round(runner_d, 3)
    return rec, f"nearest to last face ({best_d:.2f} vs {runner_d:.2f})"


def bridge(frame, *, last_face_seen_ts: float, last_face_bbox=None,
           min_score: float = _MIN_SCORE) -> dict | None:
    """Carry an ALREADY-ESTABLISHED identity across a short face occlusion.

    Returns a body record, or None. Refuses whenever the evidence runs out —
    every refusal is this module declining to guess who someone is:

      * no recent face match -> identity was never established, or has gone
        cold. A body box here would be me inventing a person's name.
      * several bodies and no last-known face position -> nothing to match on.
      * nearest body too far from where the face was -> he probably left.
      * two bodies equally close -> the evidence does not distinguish them.

    See the module docstring: person-shaped is not somebody, and somebody is
    not Zeke.
    """
    gap = time.time() - float(last_face_seen_ts or 0.0)
    if not last_face_seen_ts or gap > _BRIDGE_MAX_GAP_S:
        return None
    dets = detect(frame, min_score=min_score)
    try:
        fh, fw = int(frame.shape[0]), int(frame.shape[1])
    except Exception:  # noqa: BLE001
        return None
    rec, reason = _choose_body(dets, last_face_bbox, fw, fh, gap)
    if rec is None:
        if len(dets) > 1:
            _log(f"body bridge declined — {reason}")
        return None
    rec["bridged_after_s"] = round(gap, 1)
    rec["bridge_reason"] = reason
    return rec
