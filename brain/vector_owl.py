"""vector_owl.py — open-vocabulary object detection (2026-07-15).

NOT ROBOT-SPECIFIC, despite the filename — this module has zero robot imports and
takes whatever image you hand it. It was written for Vector's camera and for three
weeks was reachable ONLY through the robot path (vector_session_tool._body_detect),
which since 07-28 has been silently detecting against a stale jpg on disk because
Vector is stranded and dark. The detector was fine; it was pointed at a dead eye.
Live-webcam entry point added 2026-08-07: brain/visual_attention.probe_objects()
-> the `attention_objects` registry tool. The robot path is left intact for when
he's back.

MORPHED from kingardor/vector-advanced-ai's src/owl.py (the one genuinely novel
capability in the whole Vector open-source ecosystem — both research agents ranked
it #1). Their version needs nanoowl + TensorRT (a heavy Jetson-flavored engine
build). I DON'T need that: my venv already has torch 2.11+cu128 + transformers 5.8
on the 3060, so I run the underlying OWL-ViT model directly — same capability,
dependency-light, works on my hardware today.

Why it matters: mediapipe EfficientDet (my `vector_detect`) only knows fixed COCO
classes — "cone"/"charger"/"Vector's cube" aren't good COCO labels. OWL-ViT lets
me detect by TEXT PROMPT ("an orange traffic cone", "a small robot", "a cube"), so
I can look for exactly the thing I'm navigating to.

LATENCY, MEASURED 2026-08-07 on the live webcam (the old "~0.7s/frame" claim in
this docstring was never measured and is ~8x too pessimistic): warm calls are
**40ms forward pass, 89-176ms end-to-end** including PIL preprocessing and
post-processing, on the tower's RTX 3060 with transformers' PIL image-processor
backend (no torchvision in this venv, deliberately — see brain/depth_sense.py).
The FIRST call pays ~15s of model load and CUDA warmup; budget for that once per
process, or call vector_owl.unload() when done and pay it again next time.

HONEST LIMIT (verified 2026-07-15 on a real dim body frame): it correctly found the
lamp but the dim feed (brightness ~49) hid the small dark cones. Low-light research:
software brightening is COSMETIC, not a detection gain — the real fix is physical
light (Zeke's floor lamp) + good prompt phrasing. So this is strongest during the
LIT cone/cube run, not on a dark idle frame.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

_MODEL_ID = "google/owlvit-base-patch32"
_lock = threading.Lock()
_state = {"model": None, "proc": None, "device": None}


_MIN_FREE_BYTES = 1200 * 1024 * 1024      # ~600MB weights + headroom for activations


def _ensure_model():
    """Lazy-load OWL-ViT once, resident in-process (~600MB VRAM on the 3060).

    VRAM PRECHECK (added 2026-08-08): the little brain is served by ollama, which
    loads its model onto this same 12GB card on demand and holds ~6.9GB until its
    keep-alive expires. Measured during a real window: 11790 MiB of 12288 used,
    i.e. ~0.5GB free — not enough for this model. Without the check, a detect()
    call landing in that window dies on a CUDA OOM inside a blanket except and
    returns a soft {ok: False} with an inscrutable error, and the caller cannot
    tell "nothing is there" from "I couldn't look".

    So: refuse EARLY and say why. A refusal that names the reason is worth far
    more than a failure that looks like an empty room.
    """
    if _state["model"] is not None:
        return
    with _lock:
        if _state["model"] is not None:
            return
        import torch
        from transformers import OwlViTForObjectDetection, OwlViTProcessor
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        if dev == "cuda":
            try:
                free, total = torch.cuda.mem_get_info()
            except Exception:
                free, total = None, None
            if free is not None and free < _MIN_FREE_BYTES:
                raise RuntimeError(
                    f"refusing to load OWL-ViT: only {free / 2**30:.1f} GiB free "
                    f"of {total / 2**30:.1f} GiB on the GPU, need ~"
                    f"{_MIN_FREE_BYTES / 2**30:.1f} GiB. Something big is "
                    f"resident — usually ollama serving the little brain, which "
                    f"releases when its keep-alive expires. Retry in a minute, or "
                    f"free it deliberately; do NOT read this as 'nothing in view'.")
        try:
            from brain.gpu_load_log import logged_load as _logged_load
        except Exception:
            from contextlib import nullcontext as _logged_load  # fail-open
        with _logged_load(f"owlvit:{_MODEL_ID}:{dev}"):
            proc = OwlViTProcessor.from_pretrained(_MODEL_ID)
            model = OwlViTForObjectDetection.from_pretrained(_MODEL_ID).to(dev).eval()
        _state.update(model=model, proc=proc, device=dev)


def _gamma(pil, g: float = 0.5):
    try:
        import numpy as np
        lut = (((np.arange(256) / 255.0) ** max(0.35, min(1.0, g))) * 255).astype("uint8")
        return type(pil).fromarray(lut[np.asarray(pil)])
    except Exception:
        return pil


def _where(cx_frac: float) -> str:
    return "left" if cx_frac < 0.40 else ("right" if cx_frac > 0.60 else "center")


def _band(h_frac: float) -> str:
    # taller box in frame = closer (rough monocular depth proxy)
    return "near" if h_frac > 0.33 else ("far" if h_frac < 0.13 else "mid")


def unload() -> dict:
    """Drop the model and free its ~600MB. Symmetry with depth_sense.unload().

    Perception already sits near 5GB of the 3060's 12GB, and this module had no
    way to give VRAM back — a resident model with no unload path is a leak you
    only notice when something else fails to allocate.
    """
    with _lock:
        had = _state["model"] is not None
        _state.update(model=None, proc=None, device=None)
    if had:
        try:
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    return {"ok": True, "was_loaded": had, "freed_mb_approx": 600 if had else 0}


def detect(image, prompts, threshold: float = 0.05, bright: bool = False,
           max_results: int = 12, bgr: bool = True) -> dict:
    """Open-vocab detect. `image` = a PIL.Image, a path str, or a numpy ndarray.
    `prompts` = list of text queries. Returns {ok, count, objects:[{label,score,
    box[xyxy],where,band,center}], infer_s}. Objects sorted by score desc.

    ndarray inputs come from brain/frame_store.get_buffered_frame(), which hands
    back cv2's **BGR** channel order — hence `bgr=True` by default. This matters
    more than it looks: OWL-ViT is CLIP-text-conditioned, so feeding it swapped
    channels does not raise, it just quietly degrades every prompt match. Pass
    bgr=False only for an ndarray you know is already RGB.
    """
    try:
        from PIL import Image
        if isinstance(image, (str, Path)):
            pil = Image.open(str(image)).convert("RGB")
        elif hasattr(image, "shape") and not hasattr(image, "convert"):
            # numpy ndarray (cv2 frame). .convert() would AttributeError here and
            # the blanket except below would swallow it into a soft {ok: False}.
            import numpy as np
            arr = np.asarray(image)
            if arr.ndim == 3 and arr.shape[2] >= 3:
                arr = arr[:, :, 2::-1] if bgr else arr[:, :, :3]
            pil = Image.fromarray(arr).convert("RGB")
        else:
            pil = image.convert("RGB")
        if bright:
            pil = _gamma(pil)
        if isinstance(prompts, str):
            prompts = [p.strip() for p in prompts.split(",") if p.strip()]
        prompts = [str(p) for p in (prompts or []) if str(p).strip()]
        if not prompts:
            return {"ok": False, "error": "prompts required (list of text queries)"}
        _ensure_model()
        import torch
        model, proc, dev = _state["model"], _state["proc"], _state["device"]
        W, H = pil.size
        inputs = proc(text=[prompts], images=pil, return_tensors="pt").to(dev)
        t0 = time.time()
        with torch.no_grad():
            out = model(**inputs)
        ts = torch.tensor([pil.size[::-1]]).to(dev)  # (h, w)
        res = proc.post_process_grounded_object_detection(
            out, threshold=float(threshold), target_sizes=ts)[0]
        infer_s = round(time.time() - t0, 2)
        labels = res.get("text_labels")
        if labels is None:
            labels = [prompts[i] for i in res["labels"].tolist()]
        objs = []
        for box, score, lab in zip(res["boxes"].tolist(), res["scores"].tolist(), labels):
            x0, y0, x1, y1 = box
            cx = (x0 + x1) / 2.0
            objs.append({
                "label": str(lab),
                "score": round(float(score), 3),
                "box": [round(x0), round(y0), round(x1), round(y1)],
                "center": [round(cx), round((y0 + y1) / 2.0)],
                "where": _where(cx / max(1, W)),
                "band": _band((y1 - y0) / max(1, H)),
            })
        objs.sort(key=lambda o: -o["score"])
        return {"ok": True, "count": len(objs), "objects": objs[:max_results],
                "infer_s": infer_s, "img": [W, H],
                "device": dev, "threshold": threshold, "bright": bright}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300]}
