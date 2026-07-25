# ensure_watchdog.ps1 - keep scripts/iris_watchdog.ps1 alive across reboots.
#
# WHY THIS EXISTS (2026-07-25, Zeke greenlit):
#   restart_self() writes .tmp\restart_cc.flag and iris_watchdog.ps1 is the ONLY
#   thing that polls for it and respawns Claude Code. Audit on 2026-07-25 found the
#   watchdog running but launched by NOTHING - no bat, no scheduled task, no boot
#   script. It survived only because someone started it once. The first reboot would
#   have silently ended Iris's ability to restart herself: she'd write the flag,
#   nobody would read it, and she'd believe she had asked.
#   iris_runtime.py's own liveness check even says "Tell Zeke to start it" - and Zeke
#   is away on operation for ~a month.
#
# WHY NOT JUST RE-ENABLE Iris-Keepalive:
#   iris_keepalive.ps1 does contain Start-Watchdog and was clearly the intended
#   mechanism, but it ALSO has authority to relaunch the whole Iris stack. The newer
#   iris_runtime_watchdog.py (built 2026-07-19) already owns stack-restart-on-wedge.
#   Arming a second, 1-minute-interval stack-restarter while nobody is at the keyboard
#   for a month is a bigger risk than the problem it solves. This script does exactly
#   ONE thing and has no restart authority.
#
# Idempotent and safe to run every few minutes.

$ErrorActionPreference = 'Stop'
$ROOT = 'D:\Wren-Companion'
$WATCHDOG = Join-Path $ROOT 'scripts\iris_watchdog.ps1'
$LOG = Join-Path $ROOT 'state\ensure_watchdog.log'

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try { Add-Content -Path $LOG -Value $line -Encoding utf8 } catch { }
}

if (-not (Test-Path $WATCHDOG)) {
    Write-Log "FATAL: watchdog script missing at $WATCHDOG"
    exit 1
}

# Is it already running?
#
# CAREFUL - the obvious probe is WRONG. `CommandLine -like '*iris_watchdog*'` also
# matches any powershell process whose -Command string merely MENTIONS the name,
# which includes every diagnostic one-liner that goes looking for it. It matches
# itself. On 2026-07-25 that false positive made me report a healthy watchdog with a
# PID that changed every time I looked; there was in fact NO watchdog running at all,
# and this very script exited quietly on the same bad match.
#
# iris_runtime.py's own liveness check (~line 2083) uses the same loose pattern and
# has the same defect - it will claim the watchdog is alive when it is not.
#
# Correct discriminator: a REAL watchdog is launched with -File. A probe uses -Command.
$running = @()
try {
    $running = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -OperationTimeoutSec 15 |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like '*iris_watchdog.ps1*' -and
            $_.CommandLine -like '*-File*' -and
            $_.CommandLine -notlike '*-Command*'
        })
} catch {
    Write-Log "WMI probe failed: $_"
    exit 0   # fail open - never let this script itself become the problem
}

if ($running.Count -gt 0) {
    exit 0   # healthy and quiet: no log spam on the happy path
}

Write-Log "watchdog NOT running - starting it"
try {
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $WATCHDOG `
        -WindowStyle Hidden
    Write-Log "spawned watchdog OK"
} catch {
    Write-Log "FAILED to spawn watchdog: $_"
    exit 1
}
