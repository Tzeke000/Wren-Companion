"""Minimal stand-in for `yolox.utils`, so the vendored model files run
inference without installing the YOLOX package (2026-08-25).

Why this exists: `yolo_head.py` imports four helpers from `yolox.utils`, but
only ONE of them (`meshgrid`) is reachable on the inference path. The other
three live in the loss/label-assignment code, which never executes in eval
mode. Installing the whole package to satisfy three unused names would drag in
`torchvision` and a `setup.py` build — the exact dependency risk this repo has
a scar about. Zeke explicitly okayed editing what I pull ("you could also pull
it and then edit it if you wanted"), so the import is repointed here instead.

The training-only names deliberately RAISE rather than returning something
plausible. A stub that quietly returns zeros would let a training path appear
to work while producing garbage; a stub that raises tells me immediately that
I have wandered off the inference path this shim was scoped for.
"""
from __future__ import annotations

import torch


def meshgrid(*tensors):
    """The one helper the inference decode actually needs.

    Mirrors YOLOX's own implementation: torch >= 1.10 warns unless `indexing`
    is passed explicitly, and the model's grid construction assumes 'ij'.
    """
    ver = [int(x) for x in torch.__version__.split(".")[:2]]
    if ver >= [1, 10]:
        return torch.meshgrid(*tensors, indexing="ij")
    return torch.meshgrid(*tensors)


def _training_only(name: str):
    def _raise(*_a, **_k):
        raise NotImplementedError(
            f"yolox.utils.{name} is a TRAINING-path helper and is not "
            f"implemented in this inference-only shim. Reaching it means the "
            f"model is not in eval mode, or the code path changed."
        )
    return _raise


bboxes_iou = _training_only("bboxes_iou")
cxcywh2xyxy = _training_only("cxcywh2xyxy")
visualize_assign = _training_only("visualize_assign")
