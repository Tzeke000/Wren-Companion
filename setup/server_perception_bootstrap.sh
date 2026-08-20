#!/usr/bin/env bash
# server_perception_bootstrap.sh — stand up Iris's perception stack on the R740
# Target: Ubuntu 22.04 / V100 (sm_70) / driver R535 / CUDA 12.6  (pins per
# server_first_boot_constraints_2026-08-06 — do NOT bump them here).
# Written 2026-08-20 by Iris. Study: profiles/server/perception_stack_port.md
#
# Usage:  bash server_perception_bootstrap.sh [VENV_DIR]   (default ~/iris-perception)
# Safe to re-run; every step is idempotent. Downloads ~1GB of models on first verify.

set -euo pipefail

VENV_DIR="${1:-$HOME/iris-perception}"
PY=python3.11

echo "== [1/5] apt prerequisites =="
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3.11 python3.11-venv python3.11-dev \
  v4l-utils \
  libgl1 libglib2.0-0 \
  git curl

echo "== [2/5] venv =="
[ -d "$VENV_DIR" ] || $PY -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q

echo "== [3/5] pinned python stack (cu126 / Volta — do not bump) =="
# torch <=2.10 cu126: last line with sm_70 kernels. cu128+ drops Volta.
pip install -q "torch<=2.10" torchvision --index-url https://download.pytorch.org/whl/cu126
pip install -q \
  "numpy>=2,<3" \
  "opencv-python~=4.13" \
  "insightface==0.7.3" \
  "onnxruntime-gpu~=1.26" \
  "transformers~=5.8" \
  "mediapipe~=0.10" \
  pillow
# NOTE deliberately absent: winsdk (Windows-only PTZ), dlib/face_recognition (legacy, unused).

echo "== [4/5] camera + PTZ probe (UVC) =="
if ls /dev/video* >/dev/null 2>&1; then
  for dev in /dev/video*; do
    echo "--- $dev"
    v4l2-ctl -d "$dev" --info 2>/dev/null | head -4 || true
    v4l2-ctl -d "$dev" --list-ctrls 2>/dev/null | grep -E "pan_absolute|tilt_absolute|zoom_absolute" || true
  done
  echo "(expect pan_absolute/tilt_absolute on the EMEET Pixy; UVC units are 1/3600 deg)"
else
  echo "no /dev/video* found — plug the Pixy in and re-run this step"
fi

echo "== [5/5] verify: GPU + every model loads =="
python - <<'EOF'
import time
t0 = time.time()

import torch
assert torch.cuda.is_available(), "CUDA not available — check R535 driver + cu126 torch"
name = torch.cuda.get_device_name(0)
cc = torch.cuda.get_device_capability(0)
print(f"torch {torch.__version__}  cuda {torch.version.cuda}  gpu {name}  sm_{cc[0]}{cc[1]}")
assert cc >= (7, 0), "expected Volta sm_70+"

import onnxruntime as ort
assert "CUDAExecutionProvider" in ort.get_available_providers(), "onnxruntime-gpu missing CUDA EP"
print("onnxruntime CUDA EP: ok")

from insightface.app import FaceAnalysis
app = FaceAnalysis(name="buffalo_l")          # ~326MB download on first run
app.prepare(ctx_id=0, det_size=(640, 640))
print("insightface buffalo_l: ok")

from transformers import pipeline
det = pipeline("zero-shot-object-detection", model="google/owlvit-base-patch32", device=0)
print("owlvit-base-patch32: ok")
dep = pipeline("depth-estimation", model="depth-anything/Depth-Anything-V2-Small-hf", device=0)
print("Depth-Anything-V2-Small: ok")

import mediapipe, cv2, numpy
print(f"mediapipe {mediapipe.__version__}  opencv {cv2.__version__}  numpy {numpy.__version__}")
print(f"ALL PERCEPTION MODELS LOAD — {time.time()-t0:.0f}s (first run includes ~1GB downloads)")
EOF

echo ""
echo "Done. Identity transfer from the tower (run there, or via the vault):"
echo "  rsync -a tower:~/.insightface/models/buffalo_l  ~/.insightface/models/"
echo "  rsync -a tower:/d/Wren-Companion/faces/          <repo>/faces/    # known-faces DB"
echo "Remaining code port: V4l2PtzActuator (deg*3600 -> pan_absolute/tilt_absolute,"
echo "keep tilt floor at -60) — see profiles/server/perception_stack_port.md"
