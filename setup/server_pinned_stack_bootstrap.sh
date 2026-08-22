#!/usr/bin/env bash
# server_pinned_stack_bootstrap.sh — §4 "the pinned stack" on iris-home.
#
# Staged 2026-08-22, RUN ON CABLE-DAY (Dell 4VPD3+TR5TP arrive Mon 08-24 →
# V100 passthrough first, THEN this). Encodes profiles/server/first_boot_plan.md
# §4 exactly so Monday is one command, not a freestyle. Idempotent: every step
# checks before acting; safe to re-run after a partial failure.
#
#   ssh iris@100.114.189.91 'bash -s' < setup/server_pinned_stack_bootstrap.sh
#   # or, already staged on the box:
#   ssh iris@100.114.189.91 'sudo bash ~/setup/server_pinned_stack_bootstrap.sh'
#
# THE PINS (do not freestyle — CUDA13/torch2.11 dropped Volta sm_70):
#   Driver  R535 branch (or 580/581 if that's the last full-feature Volta branch)
#   CUDA    12.6   |  PyTorch <=2.10 cu126  |  Ubuntu 22.04
#
# GO/NO-GO: step 2 (nvidia-smi sees the V100 32GB) is the whole build's gate.
# If it fails, STOP — nothing downstream is meaningful without the card.
set -uo pipefail

log() { printf '\n\033[1;36m[pinned-stack]\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[pinned-stack WARN]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m[pinned-stack STOP]\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo (apt + hold + driver install need root)"

# ── 1. IOMMU / passthrough sanity (Proxmox branch A — the card is passed in) ──
log "1. IOMMU + card-visible-to-VM check"
if ! lspci 2>/dev/null | grep -qi 'NVIDIA'; then
  die "no NVIDIA device on the PCI bus. Card not passed through / not seated. \
Fix passthrough on the Proxmox host (vfio-pci bind, clean IOMMU group) FIRST — \
see first_boot_plan.md §3 Branch A step 2. Nothing here works without it."
fi
lspci | grep -i nvidia | sed 's/^/    /'

# ── 2. Driver — R535 branch, NOT "latest". THE GO/NO-GO. ──
log "2. NVIDIA driver (R535 branch — the go/no-go moment)"
if have nvidia-smi && nvidia-smi -L 2>/dev/null | grep -qi 'V100'; then
  log "driver already present:"; nvidia-smi -L | sed 's/^/    /'
else
  apt-get update
  # Prefer 535; accept 580/581 (last full-feature Volta branches) if offered.
  # Deliberately NOT `ubuntu-drivers autoinstall` (can pull 590+ maintenance-only).
  CAND=""
  for b in nvidia-driver-535 nvidia-driver-535-server nvidia-driver-580 nvidia-driver-581; do
    if apt-cache show "$b" >/dev/null 2>&1; then CAND="$b"; break; fi
  done
  [ -n "$CAND" ] || die "no R535/580/581 driver package found in apt. Add the \
graphics-drivers PPA or NVIDIA repo, then re-run. Do NOT install 'latest'."
  log "installing $CAND (reboot required after)"
  apt-get install -y "$CAND"
  warn "REBOOT NOW, then re-run this script. Driver needs a reboot to load."
  exit 0
fi
nvidia-smi | grep -Ei 'V100|32[0-9]{3}MiB|Driver Version' | sed 's/^/    /'
nvidia-smi | grep -qi 'V100' || die "GO/NO-GO FAILED: nvidia-smi runs but no V100. \
Wrong card / passthrough grabbed the Matrox. Stop and investigate."

# ── 2b. Hold the driver + kernel so `apt upgrade` never decays the pin ──
log "2b. apt-mark hold driver + kernel HWE (this box never blind-upgrades)"
HOLDS=$(dpkg -l 'nvidia-driver-*' 2>/dev/null | awk '/^ii/{print $2}')
# shellcheck disable=SC2086
[ -n "$HOLDS" ] && apt-mark hold $HOLDS
apt-mark hold linux-generic-hwe-22.04 linux-image-generic-hwe-22.04 2>/dev/null || true
apt-mark showhold | sed 's/^/    held: /'

# ── 3. CUDA toolkit 12.6 (pinned — CUDA 13 removed Volta) ──
log "3. CUDA toolkit 12.6"
if have nvcc && nvcc --version 2>/dev/null | grep -q 'release 12.6'; then
  log "CUDA 12.6 toolkit already installed"
else
  warn "CUDA 12.6 toolkit not present. Install from the NVIDIA repo PINNED to \
12.6 (do NOT add the repo unpinned — it will pull 12.x-latest):"
  cat <<'EOF'
    # Ubuntu 22.04 / x86_64 — pinned to the 12.6 line:
    wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
    sudo dpkg -i cuda-keyring_1.1-1_all.deb
    sudo apt-get update
    sudo apt-get install -y cuda-toolkit-12-6   # NOT 'cuda' (that's a metapkg tracking latest)
    echo 'export PATH=/usr/local/cuda-12.6/bin:$PATH' >> ~/.bashrc
    echo 'export LD_LIBRARY_PATH=/usr/local/cuda-12.6/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
EOF
  warn "run the block above, re-source ~/.bashrc, then re-run this script."
  exit 0
fi

# ── 4. Tailscale (door on day one). Per CORE §5 this is likely already up. ──
log "4. Tailscale"
if have tailscale && tailscale status >/dev/null 2>&1; then
  tailscale ip -4 2>/dev/null | sed 's/^/    tailnet ip: /'
else
  warn "tailscale not up. Install: curl -fsSL https://tailscale.com/install.sh | sh ; sudo tailscale up"
fi

# ── 5. llama.cpp built for Volta (ARCHS=700). -fa is UNVERIFIED on Volta: bench it. ──
log "5. llama.cpp (CUDA, ARCHS=700 — sm_70 + CUDA graphs proven, PR #25749)"
LLAMA_DIR="/opt/llama.cpp"
if [ -x "$LLAMA_DIR/build/bin/llama-cli" ]; then
  log "llama.cpp already built at $LLAMA_DIR"
else
  apt-get install -y build-essential cmake git libcurl4-openssl-dev
  [ -d "$LLAMA_DIR/.git" ] || git clone https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
  cd "$LLAMA_DIR"
  # ARCHS=700 = Volta. USE_GRAPHS on. Build only needs nvcc, not the running card.
  cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=70 -DGGML_CUDA_USE_GRAPHS=ON
  cmake --build build --config Release -j"$(nproc)"
  log "built. Banner should read: CUDA : ARCHS = 700 | USE_GRAPHS = 1"
fi

# ── 6. Bake venv — torch 2.10 cu126 (NEVER copy the tower's 2.11+cu128) ──
log "6. bake venv (torch==2.10.* cu126)"
BAKE_VENV="$HOME/bake-venv"
if [ -d "$BAKE_VENV" ] && "$BAKE_VENV/bin/python" -c 'import torch' 2>/dev/null; then
  "$BAKE_VENV/bin/python" -c 'import torch;print("    torch",torch.__version__,"cuda",torch.version.cuda)'
else
  apt-get install -y python3-venv python3-pip
  python3 -m venv "$BAKE_VENV"
  "$BAKE_VENV/bin/pip" install --upgrade pip
  # cu126 is the only wheel line still carrying sm_70. Pin <2.11.
  "$BAKE_VENV/bin/pip" install "torch==2.10.*" --index-url https://download.pytorch.org/whl/cu126 \
    || warn "torch 2.10 cu126 install failed — check the wheel line still exists; do NOT fall back to a cu128 wheel."
fi

# ── ACCEPTANCE TEST (§4, 10 min) ──
log "ACCEPTANCE TEST"
nvidia-smi >/dev/null 2>&1 && echo "    [ok] nvidia-smi" || echo "    [FAIL] nvidia-smi"
"$BAKE_VENV/bin/python" -c 'import torch; print("    [ok] torch sees", torch.cuda.get_device_name(0))' 2>/dev/null \
  || echo "    [FAIL] torch.cuda.get_device_name — driver/torch/cuda mismatch"
cat <<'EOF'

    MANUAL (do by hand — needs a GGUF + eyeballs on temps):
      pull a small GGUF, then run llama-cli BOTH ways and keep the winner:
        /opt/llama.cpp/build/bin/llama-cli -m <model.gguf> -ngl 99 -p "hi" -n 32
        /opt/llama.cpp/build/bin/llama-cli -m <model.gguf> -ngl 99 -fa -p "hi" -n 32
      -fa on Volta is UNVERIFIED (my 08-11 FA claim FAILED verification 08-17).
      Watch tokens flow + temps settle. If both + nvidia-smi + torch pass → real.

    NEXT (not this script): perception stack →
      setup/server_perception_bootstrap.sh ; model = muse-glimmer:30b q4_K_M
      (VRAM budget: ~24GB usable of 32 after perception+voice — first_boot_plan.md §4).
EOF
log "done — pinned-stack staging script complete."
