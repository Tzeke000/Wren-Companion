# scripts/parsec_ensure.ps1
#
# PARSEC DURABLE SELF-HEAL (built 2026-07-20, deployment-2 departure day).
#
# Zeke deploys ~1 month; his ONLY remote access to this tower is Parsec. Two
# ways Parsec dies:
#   1. the Parsec service (pservice.exe) is Stopped even though StartType=Auto
#   2. parsecd.exe (the HOST APP that actually serves the desktop) isn't
#      running -- its HKCU Run-key only fires at interactive logon and has
#      been observed to silently NOT fire on a cold boot (2026-07-20 incident).
#
# This script makes BOTH true again, idempotently. It is run by the scheduled
# task `Iris-Parsec-Heal` as the interactive Owner user (so parsecd launches
# into session 1 and can host), at logon AND every 10 minutes. Pure PowerShell:
# no venv, no python, no network -- so it heals even when the Iris stack is
# down. Cognition-independent belt to the hourly-cron's suspenders.
#
# Manual test:  powershell -File scripts\parsec_ensure.ps1

$ErrorActionPreference = 'SilentlyContinue'
$log = 'D:\Wren-Companion\state\parsec_heal.log'
$parsecd = 'C:\Program Files\Parsec\parsecd.exe'

function Write-Log([string]$m) {
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    # .NET AppendAllText = UTF8 no-BOM, works on Windows PowerShell 5.1
    # (where -Encoding utf8NoBOM does not exist and would silently fail).
    [System.IO.File]::AppendAllText($log, "$stamp  $m`r`n")
}

$changed = $false

# 1. Parsec service running?
$svc = Get-Service -Name 'Parsec' -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne 'Running') {
    try { Start-Service -Name 'Parsec' -ErrorAction Stop; Write-Log "started Parsec service (was $($svc.Status))"; $changed = $true }
    catch { Write-Log "FAILED to start Parsec service: $($_.Exception.Message)" }
}

# 2. parsecd.exe host app running?
$proc = Get-CimInstance Win32_Process -Filter "Name='parsecd.exe'" -ErrorAction SilentlyContinue
if (-not $proc) {
    if (Test-Path $parsecd) {
        try {
            Start-Process $parsecd -ArgumentList 'app_silent=1' -ErrorAction Stop
            Write-Log 'launched parsecd.exe (was absent)'
            $changed = $true
        } catch { Write-Log "FAILED to launch parsecd.exe: $($_.Exception.Message)" }
    } else {
        Write-Log "parsecd.exe NOT FOUND at $parsecd"
    }
}

if (-not $changed) { Write-Log 'ok (service running, parsecd present)' }
