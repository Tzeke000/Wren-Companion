# Wren-Companion bootstrap for a fresh Windows machine.
#
# Run from the repo root after manual prereqs are installed
# (Python 3.11, Git, NVIDIA driver, Claude Code).
#
# Usage:
#   cd D:\Wren-Companion
#   .\setup\bootstrap.ps1
#
# Idempotent - safe to re-run if a step fails.

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "==================================================="
Write-Host "Wren-Companion bootstrap"
Write-Host "Repo:   $RepoRoot"
Write-Host "==================================================="

# ----- Step 1: Verify Python 3.11 -----
Write-Host "`n[1/8] Verifying Python 3.11..."
try {
    $pyVer = & py -3.11 --version 2>&1
    if ($pyVer -notmatch "Python 3\.11\.") {
        Write-Host "  ERROR: py -3.11 returned: $pyVer"
        Write-Host "  Install Python 3.11 from https://www.python.org/downloads/release/python-3110/"
        exit 1
    }
    Write-Host "  OK: $pyVer"
} catch {
    Write-Host "  ERROR: py launcher not found. Install Python 3.11 (with py launcher)."
    exit 1
}

# ----- Step 2: Verify NVIDIA driver -----
Write-Host "`n[2/8] Verifying NVIDIA driver..."
try {
    $nvidiaInfo = & nvidia-smi --query-gpu=driver_version,name --format=csv,noheader 2>&1
    Write-Host "  OK: $nvidiaInfo"
    $driverVer = ($nvidiaInfo -split ",")[0].Trim()
    $major = [int]($driverVer -split "\.")[0]
    if ($major -lt 535) {
        Write-Host "  WARN: driver $driverVer may be too old for CUDA 12.8 (sm_120). Update if Kokoro CUDA fails."
    }
} catch {
    Write-Host "  WARN: nvidia-smi not found. Wren will still run, but Kokoro CUDA will fall back to Piper."
}

# ----- Step 3: Install pip deps -----
Write-Host "`n[3/8] Installing pip dependencies (this takes a few minutes)..."
& py -3.11 -m pip install --upgrade pip 2>&1 | Out-Null
& py -3.11 -m pip install -r "$RepoRoot\requirements.txt" 2>&1 | Tee-Object -FilePath "$RepoRoot\setup\bootstrap_pip.log"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: pip install failed. See setup\bootstrap_pip.log."
    exit 1
}
Write-Host "  OK: requirements.txt installed."

# ----- Step 4: Install CUDA-enabled torch (cu128 for Blackwell) -----
Write-Host "`n[4/8] Installing PyTorch with CUDA 12.8 (cu128 wheel - required for RTX 50-series sm_120)..."
& py -3.11 -m pip install --index-url https://download.pytorch.org/whl/cu128 --force-reinstall torch torchaudio 2>&1 | Tee-Object -FilePath "$RepoRoot\setup\bootstrap_torch.log"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: torch CUDA install failed. See setup\bootstrap_torch.log."
    Write-Host "  If you don't have a Blackwell GPU, try cu126 or cpu instead."
    exit 1
}
& py -3.11 -c "import torch; assert torch.cuda.is_available(); print('torch=' + torch.__version__ + ' cuda_built=' + str(torch.version.cuda) + ' cuda_avail=' + str(torch.cuda.is_available()))"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: torch CUDA verification failed."
    exit 1
}
Write-Host "  OK: torch CUDA verified."

# ----- Step 5: Verify InsightFace + Kokoro + Piper imports -----
Write-Host "`n[5/8] Verifying core imports..."
& py -3.11 -c @'
import insightface
import faster_whisper
import sounddevice
from kokoro import KPipeline
from piper import PiperVoice
print('all core imports OK')
'@
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: some imports failed. Check pip log."
    exit 1
}
Write-Host "  OK: imports work."

# ----- Step 6: Download Piper voice models -----
Write-Host "`n[6/8] Downloading Piper voice models..."
$piperDir = "$RepoRoot\models\piper"
New-Item -ItemType Directory -Force -Path $piperDir | Out-Null
$voices = @(
    @{name="en_US-amy-medium.onnx"; url="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx"},
    @{name="en_US-amy-medium.onnx.json"; url="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json"},
    @{name="en_US-lessac-high.onnx"; url="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx"},
    @{name="en_US-lessac-high.onnx.json"; url="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx.json"}
)
foreach ($v in $voices) {
    $target = Join-Path $piperDir $v.name
    if (Test-Path $target) {
        Write-Host "  skip: $($v.name) already exists"
        continue
    }
    Write-Host "  downloading: $($v.name)"
    try {
        Invoke-WebRequest -Uri $v.url -OutFile $target -UseBasicParsing
    } catch {
        Write-Host "  ERROR: download failed for $($v.name): $_"
        exit 1
    }
}
Write-Host "  OK: Piper voices ready."

# ----- Step 7: Pre-fetch Kokoro 82M (~360MB) -----
Write-Host "`n[7/8] Pre-fetching Kokoro 82M model (~360MB, internet required)..."
& py -3.11 -c @'
import time
print('  loading KPipeline (will download to ~/.cache/huggingface/hub/ if not cached)...')
from kokoro import KPipeline
t0 = time.time()
pipe = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M', device='cuda')
print('  Kokoro loaded in ' + str(round(time.time()-t0, 1)) + 's')
print('  warmup synth...')
t0 = time.time()
for _ in pipe('Hello.', voice='af_heart', speed=1.0): pass
print('  warmup done in ' + str(round(time.time()-t0, 1)) + 's')
'@
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARN: Kokoro warmup failed (may still work later). Check internet + cuDNN."
}
Write-Host "  OK: Kokoro ready."

# ----- Step 8: Create empty state directories -----
Write-Host "`n[8/8] Creating empty state directories..."
foreach ($d in @("state", "memory", "profiles", "faces", "logs")) {
    $p = Join-Path $RepoRoot $d
    if (-not (Test-Path $p)) {
        New-Item -ItemType Directory -Path $p | Out-Null
        Write-Host "  created: $d/"
    } else {
        Write-Host "  exists: $d/"
    }
}

# ----- Done -----
Write-Host ""
Write-Host "==================================================="
Write-Host "Bootstrap complete."
Write-Host "==================================================="
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. (manual, optional) Install VB-CABLE from https://vb-audio.com/Cable/  (for voice loopback testing)"
Write-Host "  2. (manual, optional) Install Voicemeeter Potato (advanced audio routing)"
Write-Host "  3. (manual, already done if you cloned the vault) Make sure D:\ClaudeCodeMemory\ exists with Wren's continuity"
Write-Host "  4. Run claude (from this directory) to open Claude Code as Wren"
Write-Host "  5. Have her read ava_core/BOOTSTRAP.md first"
Write-Host ""
Write-Host "If you want to test voice without a human at the mic:"
Write-Host "  py -3.11 scripts\audio_loopback_harness.py speak 'Hey Wren are you there'"
Write-Host ""
