# Creates a Windows desktop shortcut to launch Ava (full stack via start.bat).
# Optional: -Mode UiOnly targets the packaged exe (operator API must already be running).
param(
    [Parameter(Mandatory = $false)]
    [string] $RepoRoot = "",
    [ValidateSet("Full", "UiOnly")]
    [string] $Mode = "Full"
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
else {
    $RepoRoot = (Resolve-Path $RepoRoot).Path
}

$packagedExe = Join-Path $RepoRoot "apps\ava-control\src-tauri\target\release\ava-control.exe"
$startBat = Join-Path $RepoRoot "start.bat"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "Ava.lnk"

if (-not (Test-Path $startBat)) {
    Write-Host "[ava-shortcut] ERROR: start.bat not found at: $startBat"
    exit 1
}

if ($Mode -eq "UiOnly") {
    if (-not (Test-Path $packagedExe)) {
        Write-Host "[ava-shortcut] ERROR: packaged exe not found:"
        Write-Host "           $packagedExe"
        Write-Host "           Build first (apps\ava-control: npm run tauri:build) or use Full mode."
        exit 1
    }
    $targetPath = $packagedExe
    $arguments = ""
    $workingDirectory = Split-Path $packagedExe -Parent
    $description = "Ava operator UI only (expects Python/operator HTTP already running)."
    $iconLocation = "$packagedExe,0"
}
else {
    $targetPath = $startBat
    $arguments = ""
    $workingDirectory = $RepoRoot
    $description = "Launch Ava — Python runtime, operator HTTP, desktop app (uses packaged exe when built)."
    if (Test-Path $packagedExe) {
        $iconLocation = "$packagedExe,0"
        Write-Host "[ava-shortcut] Packaged exe found — shortcut icon will use the app executable."
    }
    else {
        $iconLocation = "$startBat,0"
        Write-Host "[ava-shortcut] No packaged exe yet — shortcut targets start.bat (dev fallback inside launch script)."
        Write-Host "           Build: cd apps\ava-control && npm run tauri:build"
    }
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.TargetPath = $targetPath
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $workingDirectory
$shortcut.Description = $description
$shortcut.IconLocation = $iconLocation
$shortcut.Save()

Write-Host "[ava-shortcut] Created: $lnkPath"
Write-Host "[ava-shortcut] Target: $targetPath"
Write-Host "[ava-shortcut] Mode: $Mode"
exit 0
