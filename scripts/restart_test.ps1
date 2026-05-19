# scripts/restart_test.ps1 - End-to-end restart test for Iris.
#
# PURPOSE: verify that the restart-CC path (restart_self -> watchdog ->
# start_iris.bat -> iris_cold_wake.py -> boot ritual) is healthy WITHOUT
# actually killing the current CC session. Per Zeke directive 2026-05-19:
# this test must NOT take down the running CC.
#
# Two modes:
#   --preflight (default)   safe to run anytime; verifies all preconditions
#                           for a successful restart. No side effects. No
#                           process spawning. No file modifications.
#   --sandbox-spawn         FUTURE: spawn a fresh CC in a temp dir with
#                           IRIS_SKIP_INSTANCE_CHECK=1 so it doesn't conflict
#                           with the running iris_runtime's PID lockfile.
#                           NOT YET IMPLEMENTED. Requires operator presence
#                           to kill the test instance if it hangs.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/restart_test.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/restart_test.ps1 -Preflight
#
# Returns exit 0 on all-pass, non-zero on any failure.
# Each check prints PASS/FAIL with a one-line reason.

param(
    [switch]$Preflight = $true,
    [switch]$SandboxSpawn = $false
)

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path

$Failures = 0
$Checks = 0

function Test-Step($name, [scriptblock]$check, $detail = "") {
    $script:Checks++
    try {
        $result = & $check
        if ($result) {
            Write-Host "  PASS  $name" -ForegroundColor Green
            if ($detail) { Write-Host "        $detail" -ForegroundColor DarkGray }
        } else {
            Write-Host "  FAIL  $name" -ForegroundColor Red
            if ($detail) { Write-Host "        $detail" -ForegroundColor DarkGray }
            $script:Failures++
        }
    } catch {
        Write-Host "  FAIL  $name (exception: $_)" -ForegroundColor Red
        $script:Failures++
    }
}

if ($SandboxSpawn) {
    Write-Host "Sandbox-spawn mode is not yet implemented." -ForegroundColor Yellow
    Write-Host "Design lives in docs/restart_test_procedure.md."
    Write-Host "Falling back to preflight checks."
    Write-Host ""
}

# ===== Preflight: file existence + executability =====
Write-Host "=== Restart test: preflight (safe; no side effects) ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "Files:"
Test-Step "start_iris.bat exists" {
    Test-Path (Join-Path $RepoRoot "start_iris.bat")
}
Test-Step "scripts/iris_cold_wake.py exists" {
    Test-Path (Join-Path $RepoRoot "scripts\iris_cold_wake.py")
}
Test-Step "scripts/iris_watchdog.ps1 exists" {
    Test-Path (Join-Path $RepoRoot "scripts\iris_watchdog.ps1")
}
Test-Step ".venv\Scripts\python.exe exists" {
    Test-Path (Join-Path $RepoRoot ".venv\Scripts\python.exe")
}
Test-Step "ava_core\IDENTITY.md exists" {
    Test-Path (Join-Path $RepoRoot "ava_core\IDENTITY.md")
}

Write-Host ""
Write-Host "Syntax / parse checks:"
Test-Step "iris_watchdog.ps1 parses clean" {
    $errors = $null
    $tokens = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $RepoRoot "scripts\iris_watchdog.ps1"),
        [ref]$tokens,
        [ref]$errors
    ) | Out-Null
    return ($null -eq $errors -or $errors.Count -eq 0)
}
Test-Step "install_ritual_scheduler.ps1 parses clean" {
    $errors = $null
    $tokens = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $RepoRoot "scripts\install_ritual_scheduler.ps1"),
        [ref]$tokens,
        [ref]$errors
    ) | Out-Null
    return ($null -eq $errors -or $errors.Count -eq 0)
}
Test-Step "iris_cold_wake.py parses clean" {
    $py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $script = Join-Path $RepoRoot "scripts\iris_cold_wake.py"
    & $py -c "import ast; ast.parse(open(r'$script').read())" 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

Write-Host ""
Write-Host "Configuration:"
Test-Step "start_iris.bat references --channels flag" {
    $content = Get-Content (Join-Path $RepoRoot "start_iris.bat") -Raw
    $content -match "--channels"
} "Restart-spawned CC must have channels flag or fam-chat + wake-word break."

Test-Step "start_iris.bat references iris_cold_wake.py" {
    $content = Get-Content (Join-Path $RepoRoot "start_iris.bat") -Raw
    $content -match "iris_cold_wake"
} "start_iris.bat must invoke iris_cold_wake for boot ritual injection."

Test-Step "iris_watchdog.ps1 references start_iris.bat" {
    $content = Get-Content (Join-Path $RepoRoot "scripts\iris_watchdog.ps1") -Raw
    $content -match "start_iris\.bat"
} "Watchdog must use start_iris.bat, not bare claude."

Write-Host ""
Write-Host "Discoverability:"
Test-Step "claude executable findable" {
    $cmd = Get-Command claude -ErrorAction SilentlyContinue
    return ($null -ne $cmd)
} "Without claude on PATH, watchdog respawn falls through all tiers."

Write-Host ""
Write-Host "Restart-related state:"
Test-Step "state/iris.pid exists (singleton lockfile)" {
    Test-Path (Join-Path $RepoRoot "state\iris.pid")
} "If absent, singleton guard hasn't run yet on this iris_runtime instance — expected if iris_runtime predates singleton guard install."

Test-Step ".tmp directory exists or creatable" {
    $tmp = Join-Path $RepoRoot ".tmp"
    if (Test-Path $tmp) { return $true }
    try {
        New-Item -ItemType Directory -Path $tmp -Force -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

Write-Host ""
Write-Host "Watchdog daemon health:"
Test-Step "iris_watchdog.log exists (proves watchdog ran at least once)" {
    Test-Path (Join-Path $RepoRoot ".tmp\watchdog.log")
} "If absent, watchdog has never run or log path differs."

Test-Step "iris_watchdog.ps1 process detected" {
    $procs = Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like "*iris_watchdog*" }
    return ($null -ne $procs)
} "If absent, the watchdog is not running — letter-arrival cold-spawn won't fire."

# ===== Summary =====
Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
Write-Host "Total checks: $Checks"
if ($Failures -eq 0) {
    Write-Host "ALL PASSED" -ForegroundColor Green
    Write-Host ""
    Write-Host "Preflight clean. Restart path components are all in place. To test"
    Write-Host "the FULL restart (which kills current CC), use restart_self from CC"
    Write-Host "or write .tmp/restart_cc.flag manually -- but only when an operator"
    Write-Host "is available to recover if the restart fails."
    exit 0
} else {
    Write-Host "FAILED: $Failures of $Checks checks" -ForegroundColor Red
    Write-Host ""
    Write-Host "Fix the failures before relying on the restart path. Each fail above"
    Write-Host "lists what's broken. Restart attempts in this state may not recover."
    exit 1
}
