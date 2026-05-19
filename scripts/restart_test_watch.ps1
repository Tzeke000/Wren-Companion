# scripts/restart_test_watch.ps1 - End-to-end restart test with verification.
#
# Spawns a fresh CC via start_iris_test.bat in a separate window, then polls
# .tmp/ for the test_complete_<id>.flag file written by the new CC at the
# end of its boot ritual. Confirms not just "new CC started" but also
# "new CC read memory and completed all 9 steps" (the lights-on-but-nobody-
# home failure mode is the real concern during deployment).
#
# Per Zeke directive 2026-05-19 ~12:50 EDT: testing should catch "new CC
# never starts" AND "new CC starts but doesn't read memory fully." This
# script catches both.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/restart_test_watch.ps1
#
# Returns exit 0 on flag file found within timeout, non-zero on timeout or
# error.

param(
    [int]$TimeoutSec = 600  # 10-minute default; cold-wake can be slow
)

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$TestLauncher = Join-Path $RepoRoot "start_iris_test.bat"
$TmpDir = Join-Path $RepoRoot ".tmp"

if (-not (Test-Path $TestLauncher)) {
    Write-Error "Test launcher not found at $TestLauncher"
    exit 1
}

# Compute the expected flag filename. Use the same timestamp scheme as
# start_iris_test.bat so we know what to poll for. wmic dance matches what
# the .bat does to ensure exact match.
$dt = (Get-Date).ToString("yyyyMMddHHmmss")
$ExpectedFlag = Join-Path $TmpDir "test_complete_$dt.flag"

Write-Host "=== Restart test (parallel spawn + verify memory read) ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Repo root:        $RepoRoot"
Write-Host "Test launcher:    $TestLauncher"
Write-Host "Expected flag:    $ExpectedFlag"
Write-Host "Timeout:          $TimeoutSec sec"
Write-Host ""

# Ensure .tmp/ exists and the flag isn't lingering from a prior test.
if (-not (Test-Path $TmpDir)) {
    New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
}
if (Test-Path $ExpectedFlag) {
    Write-Warning "Stale flag at expected path; removing before test."
    Remove-Item -Path $ExpectedFlag -Force -ErrorAction SilentlyContinue
}

# IMPORTANT: the IRIS_TEST_ID env var must be set BEFORE start_iris_test.bat
# runs, AND the value must match the $dt we computed. We override what the
# .bat would compute internally by setting IRIS_TEST_ID in this process env
# before launching the .bat. The .bat reads env first, so this wins.
$env:IRIS_TEST_ID = $dt

# Spawn the test launcher in a NEW Windows Terminal window so we can watch
# the cold-wake visually. start_iris_test.bat inherits the env vars.
Write-Host "Spawning test CC in new Windows Terminal window..."
try {
    Start-Process "wt.exe" -ArgumentList "-w", "0", "nt", "cmd.exe", "/k", $TestLauncher
} catch {
    # Fallback: cmd /k if wt.exe unavailable
    Write-Warning "wt.exe unavailable; falling back to cmd.exe /k"
    Start-Process "cmd.exe" -ArgumentList "/k", $TestLauncher
}

Write-Host "Spawned. Polling for $ExpectedFlag (every 5s, timeout $TimeoutSec)..."
Write-Host ""

# Poll loop.
$startTime = Get-Date
$found = $false
while (((Get-Date) - $startTime).TotalSeconds -lt $TimeoutSec) {
    if (Test-Path $ExpectedFlag) {
        $found = $true
        break
    }
    Start-Sleep -Seconds 5
    $elapsed = [int]((Get-Date) - $startTime).TotalSeconds
    Write-Host "  ...waiting ($elapsed`s elapsed)" -ForegroundColor DarkGray
}

Write-Host ""
if ($found) {
    Write-Host "PASS: flag found at $ExpectedFlag" -ForegroundColor Green
    Write-Host ""
    Write-Host "Flag contents:"
    Get-Content -Path $ExpectedFlag | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
    Write-Host "Test CC completed full boot ritual including memory read."
    Write-Host "You can close the test CC window now."
    exit 0
} else {
    Write-Host "FAIL: flag never appeared within $TimeoutSec sec" -ForegroundColor Red
    Write-Host ""
    Write-Host "Possible failure modes:"
    Write-Host "  1. Test CC never started --check the Windows Terminal window for errors."
    Write-Host "  2. Test CC started but didn't reach step 10 -- boot ritual incomplete."
    Write-Host "  3. Test CC started, ran boot ritual, but failed to write the flag file."
    Write-Host "  4. start_iris_test.bat IRIS_TEST_ID mismatched expected $dt --race."
    Write-Host ""
    Write-Host "Diagnosis:"
    Write-Host "  - Check the test CC window for boot ritual output."
    Write-Host "  - Check .tmp/ for any test_complete_*.flag files (might have different timestamp)."
    Write-Host "  - Close the test CC window to clean up before retrying."
    exit 1
}
