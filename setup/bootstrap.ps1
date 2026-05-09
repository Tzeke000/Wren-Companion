# Wren-Companion bootstrap for a fresh Windows machine.
#
# Auto-installs (via winget): Python 3.11, Git, Node.js, Claude Code.
# Auto-installs (via pip): all Python deps, CUDA torch (cu128 for Blackwell).
# Auto-downloads: Piper voice models, Kokoro 82M.
# Manual still required: NVIDIA driver (already installed if GPU works),
#                       VB-CABLE (optional, for voice loopback testing),
#                       Voicemeeter (optional, advanced audio routing).
#
# Usage:
#   cd D:\Wren-Companion
#   .\setup\bootstrap.ps1
#
# Idempotent - safe to re-run if a step fails.

# Continue on stderr writes from native commands. PowerShell 5.1 with
# ErrorActionPreference=Stop treats *anything* a native command writes
# to stderr (including pip's PATH WARNING and npm's "npm notice" lines)
# as a fatal NativeCommandError. Both pip and npm legitimately use stderr
# for non-fatal info. We check $LASTEXITCODE manually after each native
# call to detect real failures.
$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "==================================================="
Write-Host "Wren-Companion bootstrap"
Write-Host "Repo:   $RepoRoot"
Write-Host "==================================================="

# ----- Helper: refresh PATH after winget installs -----
function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}

# ----- Helper: run a native command and check exit code -----
# Catches stderr noise without killing the script.
function Invoke-Native {
    param([string]$Description, [scriptblock]$Block)
    & $Block
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: $Description failed (exit code $LASTEXITCODE)"
        exit 1
    }
}

# ----- Step 0: Auto-install pre-reqs via winget -----
Write-Host "`n[0/8] Auto-installing prerequisites via winget..."
try {
    & winget --version 2>&1 | Out-Null
} catch {
    Write-Host "  ERROR: winget not found. Update Windows or install App Installer from Microsoft Store."
    Write-Host "         Then manually install Python 3.11, Git, Node.js, Claude Code and re-run this script."
    exit 1
}

# Python 3.11
try {
    $pyVer = & py -3.11 --version 2>&1
    if ($pyVer -match "Python 3\.11\.") {
        Write-Host "  Python 3.11 already installed: $pyVer"
    } else {
        throw "not 3.11"
    }
} catch {
    Write-Host "  Installing Python 3.11 via winget..."
    & winget install --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
    Refresh-Path
    Start-Sleep -Seconds 2
    try {
        $pyVer = & py -3.11 --version 2>&1
        Write-Host "  OK: $pyVer"
    } catch {
        Write-Host "  ERROR: Python 3.11 install via winget failed. Install manually:"
        Write-Host "         https://www.python.org/downloads/release/python-3119/"
        exit 1
    }
}

# Git
try {
    $gitVer = & git --version 2>&1
    Write-Host "  Git already installed: $gitVer"
} catch {
    Write-Host "  Installing Git via winget..."
    & winget install --id Git.Git --silent --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
    Refresh-Path
    Start-Sleep -Seconds 2
}

# Node.js (needed for Claude Code install via npm)
try {
    $nodeVer = & node --version 2>&1
    Write-Host "  Node.js already installed: $nodeVer"
} catch {
    Write-Host "  Installing Node.js LTS via winget..."
    & winget install --id OpenJS.NodeJS.LTS --silent --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
    Refresh-Path
    Start-Sleep -Seconds 2
}

# Claude Code (the brain)
# Wrap npm calls in cmd /c so PowerShell 5.1 doesn't treat npm's stderr
# notices as NativeCommandError. npm legitimately writes "npm notice" lines
# to stderr that aren't errors, but $ErrorActionPreference="Stop" trips on
# them. cmd /c isolates the call so PowerShell only sees the exit code.
$claudeAlreadyInstalled = $false
try {
    $claudeVer = & cmd /c "claude --version 2>&1"
    if ($LASTEXITCODE -eq 0 -and $claudeVer) {
        $claudeAlreadyInstalled = $true
        Write-Host "  Claude Code already installed: $claudeVer"
    }
} catch {
    # fall through to install
}
if (-not $claudeAlreadyInstalled) {
    Write-Host "  Installing Claude Code via npm (this takes a minute)..."
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & cmd /c "npm install -g @anthropic-ai/claude-code 2>&1" | Tee-Object -FilePath "$RepoRoot\setup\bootstrap_claude.log" | Out-Null
    $npmExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    Refresh-Path
    Start-Sleep -Seconds 2
    try {
        $claudeVer = & cmd /c "claude --version 2>&1"
        if ($LASTEXITCODE -eq 0 -and $claudeVer) {
            Write-Host "  OK: $claudeVer"
        } else {
            Write-Host "  WARN: Claude Code install may have failed (npm exit=$npmExit). Check $RepoRoot\setup\bootstrap_claude.log"
            Write-Host "         Manual fallback: npm install -g @anthropic-ai/claude-code"
        }
    } catch {
        Write-Host "  WARN: claude --version not found. Open a new PowerShell window and try again."
    }
}

Write-Host "  OK: pre-reqs installed."

# ----- Step 1: Verify Python 3.11 (re-check after auto-install) -----
Write-Host "`n[1/8] Verifying Python 3.11..."
try {
    $pyVer = & py -3.11 --version 2>&1
    if ($pyVer -notmatch "Python 3\.11\.") {
        Write-Host "  ERROR: py -3.11 returned: $pyVer"
        exit 1
    }
    Write-Host "  OK: $pyVer"
} catch {
    Write-Host "  ERROR: py launcher not found despite winget install. Open a new PowerShell window and re-run."
    exit 1
}

# ----- Step 1.5: Create venv on D: drive so all packages install there -----
# Without this, pip installs to C:\Users\...\Python311\site-packages by
# default, which fills C: drive (cu128 torch alone is ~5GB extracted).
# Per Zeke 2026-05-09: 'can we have everything downloaded in D drive and
# used there?' Yes - via venv on D:.
Write-Host "`n[1.5/8] Setting up venv on D: (so packages don't fill C:)..."
$VenvDir = "$RepoRoot\.venv"
$VenvPython = "$VenvDir\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "  creating venv at $VenvDir..."
    & py -3.11 -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        Write-Host "  ERROR: venv creation failed."
        exit 1
    }
}
Write-Host "  OK: venv ready ($VenvPython)"

# Relocate pip cache + temp dir to D: so even download staging stays
# off C:. Without this, pip downloads 2.7GB cu128 wheel through %TEMP%
# which is on C: and fills it.
$PipCacheDir = "$RepoRoot\.pip-cache"
$TempDir = "$RepoRoot\.tmp"
New-Item -ItemType Directory -Force -Path $PipCacheDir | Out-Null
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
$env:TMP = $TempDir
$env:TEMP = $TempDir
$env:PIP_CACHE_DIR = $PipCacheDir
Write-Host "  pip cache: $PipCacheDir"
Write-Host "  temp dir:  $TempDir"

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

# ----- Step 3: Install pip deps INTO VENV (on D:) -----
Write-Host "`n[3/8] Installing pip dependencies into venv on D: (this takes a few minutes)..."
& $VenvPython -m pip install --upgrade pip 2>&1 | Out-Null
& $VenvPython -m pip install -r "$RepoRoot\requirements.txt" 2>&1 | Tee-Object -FilePath "$RepoRoot\setup\bootstrap_pip.log"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: pip install failed. See setup\bootstrap_pip.log."
    exit 1
}
Write-Host "  OK: requirements.txt installed into venv."

# ----- Step 4: Install CUDA-enabled torch INTO VENV (cu128 for Blackwell) -----
Write-Host "`n[4/8] Installing PyTorch with CUDA 12.8 into venv (cu128 - works for Ampere/Ada/Blackwell)..."
& $VenvPython -m pip install --index-url https://download.pytorch.org/whl/cu128 --force-reinstall torch torchaudio 2>&1 | Tee-Object -FilePath "$RepoRoot\setup\bootstrap_torch.log"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: torch CUDA install failed. See setup\bootstrap_torch.log."
    Write-Host "  If you don't have a Blackwell GPU, try cu126 or cpu instead."
    exit 1
}
& $VenvPython -c "import torch; assert torch.cuda.is_available(); print('torch=' + torch.__version__ + ' cuda_built=' + str(torch.version.cuda) + ' cuda_avail=' + str(torch.cuda.is_available()))"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: torch CUDA verification failed."
    exit 1
}
Write-Host "  OK: torch CUDA verified in venv."

# ----- Step 5: Verify InsightFace + Kokoro + Piper imports -----
Write-Host "`n[5/8] Verifying core imports in venv..."
& $VenvPython -c @'
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
