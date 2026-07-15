"""vector_owl.py — open-vocabulary object detection for my Vector eyes (2026-07-15).

MORPHED from kingardor/vector-advanced-ai's src/owl.py (the one genuinely novel
capability in the whole Vector open-source ecosystem — both research agents ranked
it #1). Their version needs nanoowl + TensorRT (a heavy Jetson-flavored engine
build). I DON'T need that: my venv already has torch 2.11+cu128 + transformers 5.8
on the 3060, so I run the underlying OWL-ViT model directly — same capability,
dependency-light, works on my hardware today.

Why it matters: mediapipe EfficientDet (my `vector_detect`) only knows fixed COCO
classes — "cone"/"charger"/"Vector's cube" aren't good COCO labels. OWL-ViT lets
me detect by TEXT PROMPT ("an orange traffic cone", "a small robot", "a cube"), so
I can look for exactly the thing I'm navigating to. ~0.7s/frame on the 3060.

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


def _ensure_model():
    """Lazy-load OWL-ViT once, resident in-process (~600MB VRAM on the 3060)."""
    if _state["model"] is not None:
        return
    with _lock:
        if _state["model"] is not None:
            return
        import torch
        from transformers import OwlViTForObjectDetection, OwlViTProcessor
        dev = "cuda" if torch.cuda.is_available() else "cpu"
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


def detect(image, prompts, threshold: float = 0.05, bright: bool = False,
           max_results: int = 12) -> dict:
    """Open-vocab detect. `image` = a PIL.Image or a path str. `prompts` = list of
    text queries. Returns {ok, count, objects:[{label,score,box[xyxy],where,band,
    center}], infer_s}. Objects sorted by score desc."""
    try:
        from PIL import Image
        if isinstance(image, (str, Path)):
            pil = Image.open(str(image)).convert("RGB")
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
