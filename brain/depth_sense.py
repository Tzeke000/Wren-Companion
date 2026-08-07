"""Monocular depth — near / mid / far / approaching, from one flat camera.

Built 2026-08-06 on Zeke's go: *"go ahead and see if you can download something
for judging distance."* Model: **Depth Anything V2 Small** (24.8M params,
`depth-anything/Depth-Anything-V2-Small-hf`), free, runs on the 3060.

═══ WHAT THIS IS AND IS NOT — say it once, loudly ═══

**RELATIVE depth, NOT metric.** It answers "is that nearer than this" and "is it
coming toward me." It does NOT answer "six feet." A monocular model has no
baseline, so absolute distance is unrecoverable without a reference of known
size. Anything in here that reads like a distance is an ordering, not a
measurement, and `metric=False` is returned on every call so a caller can't
quietly forget.

Zeke told me in June and again 2026-08-06 that a program could judge distance
"but not as good as real dual eyes." That was correct then and it's correct now.
The OAK-D Lite we didn't buy would have given true stereo; this closes most of
the *proximity* gap and none of the *metric* one.

═══ WHY THE PREPROCESSING IS HAND-ROLLED ═══

`AutoImageProcessor` requires torchvision, which is not in this venv. I did NOT
pip-install it: the venv carries the live body (insightface, mediapipe, the
capture loop), torch here is 2.11.0+cu128, and letting pip resolve torchvision
could drag torch and take the running body down mid-session. Not worth it for a
resize.

So the resize/normalize below is done with cv2 + numpy, using the values READ
from the model's own `preprocessor_config.json` rather than remembered:
    size 518x518, keep_aspect_ratio, ensure_multiple_of=14, bicubic,
    rescale 1/255, mean [0.485,0.456,0.406], std [0.229,0.224,0.225]
**Deviation, stated honestly:** the reference DPT resize has its own
keep-aspect-ratio rounding rules that I approximate (scale longest side to 518,
round both dims to a multiple of 14). For relative depth that is immaterial —
the output is an ordering — but it is a deviation and it is written down rather
than hidden.

═══ COST ═══

Lazy-loaded: no model, no VRAM, no CUDA context until something asks for depth.
Perception already sits at ~5GB of the 3060's 12GB; this is ~100MB of weights
plus activations. Call it at a low rate — depth is a "where is it" question, not
a per-frame one.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Any

_TORCH_OK = False
try:
    import numpy as np
    import torch
    _TORCH_OK = True
except Exception:
    np = None  # type: ignore[assignment]

_CV2_OK = False
try:
    import cv2
    _CV2_OK = True
except Exception:
    pass

MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"

# read from preprocessor_config.json, not remembered
_TARGET = 518
_MULTIPLE = 14
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

_LOCK = threading.RLock()
_MODEL: Any = None
_DEV = "cpu"
_LOAD_ERR: str | None = None
_HIST: dict[str, deque] = {}


def _log(msg: str) -> None:
    print(f"[depth_sense] {msg}", file=sys.stderr, flush=True)


def available() -> bool:
    """True if depth COULD be produced. Does not load the model."""
    return _TORCH_OK and _CV2_OK


def load_error() -> str | None:
    return _LOAD_ERR


def _ensure_model() -> Any:
    global _MODEL, _DEV, _LOAD_ERR
    with _LOCK:
        if _MODEL is not None:
            return _MODEL
        if not available():
            _LOAD_ERR = "torch/cv2 unavailable"
            return None
        try:
            from transformers import AutoModelForDepthEstimation
            t0 = time.time()
            m = AutoModelForDepthEstimation.from_pretrained(MODEL_ID)
            _DEV = "cuda" if torch.cuda.is_available() else "cpu"
            _MODEL = m.to(_DEV).eval()
            _log(f"loaded {MODEL_ID} on {_DEV} in {time.time() - t0:.1f}s")
        except Exception as e:
            _LOAD_ERR = repr(e)
            _log(f"load failed: {e!r}")
            return None
        return _MODEL


def unload() -> dict:
    """Drop the model and free VRAM. Depth is occasional; holding weights for a
    question nobody asked is exactly the kind of cost I should be able to undo."""
    global _MODEL
    with _LOCK:
        had = _MODEL is not None
        _MODEL = None
        try:
            if _TORCH_OK and torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        return {"ok": True, "unloaded": had}


def _preprocess(frame_bgr: Any) -> Any:
    """BGR uint8 HxWx3 -> normalised NCHW float tensor. See the module header
    for why this is hand-rolled and where it deviates."""
    h, w = frame_bgr.shape[:2]
    scale = _TARGET / float(max(h, w))
    nh = max(_MULTIPLE, int(round(h * scale / _MULTIPLE)) * _MULTIPLE)
    nw = max(_MULTIPLE, int(round(w * scale / _MULTIPLE)) * _MULTIPLE)
    img = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_CUBIC)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype("float32") / 255.0
    img = (img - np.asarray(_MEAN, dtype="float32")) / np.asarray(_STD, dtype="float32")
    return torch.from_numpy(img.transpose(2, 0, 1)).unsqueeze(0)


def depth_map(frame_bgr: Any) -> dict:
    """Relative inverse-depth map for a frame.

    Depth Anything outputs INVERSE depth: larger = nearer. Kept in that
    convention and labelled, rather than silently flipped.
    """
    m = _ensure_model()
    if m is None:
        return {"ok": False, "error": _LOAD_ERR or "model unavailable"}
    try:
        t0 = time.time()
        with torch.no_grad():
            out = m(pixel_values=_preprocess(frame_bgr).to(_DEV))
        d = out.predicted_depth.squeeze().float().cpu().numpy()
        return {"ok": True, "map": d, "device": _DEV,
                "infer_s": round(time.time() - t0, 3),
                "convention": "inverse_depth: HIGHER = NEARER",
                "metric": False}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def _band(pct: float) -> str:
    """Coarse verbal band from a percentile within the frame's own depth range.
    Deliberately three buckets: the model earns 'nearer/farther', not numbers."""
    if pct >= 0.66:
        return "near"
    if pct >= 0.33:
        return "mid"
    return "far"


def depth_for_bbox(frame_bgr: Any, bbox: Any, *, track_key: str | None = None) -> dict:
    """Where does this box sit in the frame's depth ordering, and is it moving
    toward me?

    `track_key` (e.g. 'person:zeke') enables approach detection by remembering
    the last few readings for that target. Without it, no trend is reported —
    a single frame cannot tell you something is approaching, and claiming
    otherwise would be inventing motion from one sample.
    """
    r = depth_map(frame_bgr)
    if not r.get("ok"):
        return r
    d = r["map"]
    try:
        H, W = d.shape[:2]
        fh, fw = frame_bgr.shape[:2]
        x1, y1, x2, y2 = [float(v) for v in list(bbox)[:4]]
        sx, sy = W / float(fw), H / float(fh)
        a = max(0, int(x1 * sx)); b = min(W, max(a + 1, int(x2 * sx)))
        c = max(0, int(y1 * sy)); e = min(H, max(c + 1, int(y2 * sy)))
        patch = d[c:e, a:b]
        if patch.size == 0:
            return {"ok": False, "error": "bbox outside frame"}
        val = float(np.median(patch))
        lo, hi = float(d.min()), float(d.max())
        pct = 0.5 if hi <= lo else (val - lo) / (hi - lo)
    except Exception as ex:
        return {"ok": False, "error": f"bbox math: {ex!r}"}

    out = {"ok": True, "relative": round(pct, 3), "band": _band(pct),
           "raw_inverse_depth": round(val, 3), "metric": False,
           "infer_s": r.get("infer_s"), "device": r.get("device"),
           "trend": None, "note": "relative ordering only — NOT a distance"}

    if track_key:
        with _LOCK:
            h = _HIST.setdefault(track_key, deque(maxlen=5))
            h.append((time.time(), pct))
            if len(h) >= 3:
                first, last = h[0][1], h[-1][1]
                delta = last - first
                # inverse depth: rising = getting nearer
                if delta > 0.06:
                    out["trend"] = "approaching"
                elif delta < -0.06:
                    out["trend"] = "receding"
                else:
                    out["trend"] = "steady"
                out["trend_delta"] = round(delta, 3)
            else:
                out["trend_note"] = (f"need {3 - len(h)} more reading(s) before "
                                     f"I'll claim a direction")
    return out


def reset_history(track_key: str | None = None) -> dict:
    with _LOCK:
        if track_key:
            _HIST.pop(track_key, None)
        else:
            _HIST.clear()
    return {"ok": True}


def status() -> dict:
    return {"ok": True, "available": available(), "loaded": _MODEL is not None,
            "device": _DEV if _MODEL is not None else None,
            "model": MODEL_ID, "metric": False,
            "load_error": _LOAD_ERR,
            "tracked_keys": sorted(_HIST.keys())}


def self_test() -> dict:
    """Hardware-light check: does the model load, run, and order two synthetic
    planes correctly? Uses a crafted image rather than noise, because noise has
    no depth cues and would prove nothing — which is the same 'window too short'
    mistake in a different costume.
    """
    fails: list[str] = []
    if not available():
        return {"ok": False, "checks_failed": ["torch/cv2 unavailable"]}

    # A large bright rectangle low in frame reads as nearer surface than a small
    # dim one high in frame for essentially every monocular model (perspective +
    # position priors). This is a WEAK test and labelled as such.
    img = np.full((480, 640, 3), 40, dtype="uint8")
    cv2.rectangle(img, (40, 300), (600, 470), (200, 200, 200), -1)   # near-ish
    cv2.rectangle(img, (280, 40), (360, 110), (90, 90, 90), -1)      # far-ish

    r = depth_map(img)
    if not r.get("ok"):
        return {"ok": False, "checks_failed": [f"depth_map failed: {r.get('error')}"]}
    d = r["map"]
    if not np.isfinite(d).all():
        fails.append("depth map has non-finite values")
    if float(d.max()) <= float(d.min()):
        fails.append("depth map is flat")

    lo = depth_for_bbox(img, (40, 300, 600, 470))
    hi = depth_for_bbox(img, (280, 40, 360, 110))
    if not (lo.get("ok") and hi.get("ok")):
        fails.append("depth_for_bbox failed")
    elif not lo["relative"] > hi["relative"]:
        fails.append(f"expected the low/large region nearer: "
                     f"{lo.get('relative')} vs {hi.get('relative')} (WEAK test)")

    # single reading must NOT claim a trend
    reset_history("t")
    one = depth_for_bbox(img, (40, 300, 600, 470), track_key="t")
    if one.get("trend") is not None:
        fails.append("claimed a trend from one sample")
    for _ in range(3):
        depth_for_bbox(img, (40, 300, 600, 470), track_key="t")
    many = depth_for_bbox(img, (40, 300, 600, 470), track_key="t")
    if many.get("trend") != "steady":
        fails.append(f"static scene should read steady, got {many.get('trend')}")
    if many.get("metric") is not False:
        fails.append("metric must always be False")

    return {"ok": not fails, "checks_failed": fails,
            "infer_s": r.get("infer_s"), "device": r.get("device"),
            "near_pct": lo.get("relative"), "far_pct": hi.get("relative")}


if __name__ == "__main__":
    import json
    out = self_test()
    print(json.dumps(out, indent=2))
    sys.exit(0 if out["ok"] else 1)
