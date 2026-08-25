"""Vendored YOLOX model code — INFERENCE ONLY (2026-08-25).

Source: github.com/Megvii-BaseDetection/YOLOX, Apache-2.0, which permits this.
Only the files needed for a forward pass are here; the package's training
tooling, datasets, and setup.py build are deliberately absent.

Upstream's own `__init__` pulls in `build`, `yolo_fpn` and the training stack.
This one imports the three pieces that make a YOLOX-s detector and nothing
else, so an accidental training import fails loudly instead of quietly
installing half a research repo.
"""
from .yolo_head import YOLOXHead          # noqa: F401
from .yolo_pafpn import YOLOPAFPN         # noqa: F401
from .yolox_net import YOLOX              # noqa: F401
