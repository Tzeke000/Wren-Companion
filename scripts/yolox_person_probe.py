"""YOLOX-s person detector — bench + probe against the OWL-ViT baseline.

Built 2026-08-25 after OWL-ViT scored `person` at 0.127 on a frame Zeke was
filling. A re-ID pipeline embeds a cropped body, so the detector is the
foundation everything else stands on; this measures whether the foundation is
actually better before anything gets wired in.

Runs the VENDORED inference-only YOLOX (vendor/yolox, Apache-2.0) against
weights at models/yolox/yolox_s.pth. No torchvision: NMS is cv2.dnn.NMSBoxes.

Usage:  python scripts/yolox_person_probe.py <image> [<image> ...]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vendor.yolox import YOLOX, YOLOPAFPN, YOLOXHead  # noqa: E402

INPUT_SIZE = (640, 640)
PERSON_CLASS = 0          # COCO class 0 is 'person'
SCORE_THRESH = 0.05       # deliberately low — matches the OWL-ViT probe's
NMS_THRESH = 0.45         # threshold so the comparison is like-for-like


def build(weights: Path, device: str) -> torch.nn.Module:
    # YOLOX-s config, from exps/default/yolox_s.py
    model = YOLOX(YOLOPAFPN(0.33, 0.50), YOLOXHead(80, 0.50))
    ck = torch.load(weights, map_location="cpu", weights_only=False)
    missing, unexpected = model.load_state_dict(ck["model"], strict=False)
    if missing or unexpected:
        raise SystemExit(f"weight mismatch: {len(missing)} missing, "
                         f"{len(unexpected)} unexpected — refusing to run a "
                         f"partially-loaded model")
    return model.eval().to(device)


def letterbox(img: np.ndarray) -> tuple[np.ndarray, float]:
    """Resize preserving aspect into a 114-padded square, YOLOX-style."""
    h, w = img.shape[:2]
    r = min(INPUT_SIZE[0] / h, INPUT_SIZE[1] / w)
    resized = cv2.resize(img, (int(w * r), int(h * r)),
                         interpolation=cv2.INTER_LINEAR)
    canvas = np.full((INPUT_SIZE[0], INPUT_SIZE[1], 3), 114, dtype=np.uint8)
    canvas[:resized.shape[0], :resized.shape[1]] = resized
    return canvas, r


def detect_people(model, device, img: np.ndarray) -> tuple[list, float]:
    canvas, ratio = letterbox(img)
    # YOLOX (non-legacy) takes raw 0-255 BGR, CHW, no mean/std normalisation.
    x = torch.from_numpy(canvas.transpose(2, 0, 1)).float().unsqueeze(0).to(device)
    if device == "cuda":
        torch.cuda.synchronize()
    t0 = time.perf_counter()
    with torch.no_grad():
        out = model(x)
    if device == "cuda":
        torch.cuda.synchronize()
    ms = (time.perf_counter() - t0) * 1000.0

    o = out[0].cpu().numpy()                       # (8400, 85)
    scores = o[:, 4] * o[:, 5 + PERSON_CLASS]      # objectness * P(person)
    keep = scores > SCORE_THRESH
    if not keep.any():
        return [], ms
    boxes_cxcywh, sc = o[keep, :4], scores[keep]
    # cxcywh -> xywh in ORIGINAL image coordinates
    xywh = np.stack([(boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2) / ratio,
                     (boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2) / ratio,
                     boxes_cxcywh[:, 2] / ratio,
                     boxes_cxcywh[:, 3] / ratio], axis=1)
    idx = cv2.dnn.NMSBoxes(xywh.tolist(), sc.tolist(), SCORE_THRESH, NMS_THRESH)
    if len(idx) == 0:
        return [], ms
    idx = np.array(idx).flatten()
    dets = [(float(sc[i]), [int(v) for v in xywh[i]]) for i in idx]
    dets.sort(reverse=True, key=lambda d: d[0])
    return dets, ms


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = build(ROOT / "models" / "yolox" / "yolox_s.pth", device)
    print(f"YOLOX-s on {device} | score>{SCORE_THRESH} | "
          f"NMS {NMS_THRESH} | class {PERSON_CLASS} (person)\n")

    # One warm-up: the first CUDA call pays kernel compilation and would
    # otherwise be reported as the model's speed.
    detect_people(model, device, np.zeros((480, 640, 3), np.uint8))

    for path in sys.argv[1:]:
        img = cv2.imread(path)
        if img is None:
            print(f"{Path(path).name:28s} UNREADABLE")
            continue
        dets, ms = detect_people(model, device, img)
        if not dets:
            print(f"{Path(path).name:28s} {ms:6.1f}ms  NO PERSON")
            continue
        best, box = dets[0]
        extra = f"  (+{len(dets) - 1} more)" if len(dets) > 1 else ""
        print(f"{Path(path).name:28s} {ms:6.1f}ms  person {best:.3f} "
              f"box={box}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
