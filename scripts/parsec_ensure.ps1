# scripts/parsec_ensure.ps1
#
# PARSEC DURABLE SELF-HEAL (built 2026-07-20; connectivity-probe added 2026-07-25).
#
# Zeke's ONLY remote access to this tower is Parsec. THREE ways it dies:
#   1. the Parsec service (pservice.exe) is Stopped even though StartType=Auto
#   2. parsecd.exe (the HOST APP that serves the desktop) isn't running --
#      its HKCU Run-key only fires at interactive logon and has been observed
#      to silently NOT fire on a cold boot (2026-07-20 incident).
#   3. NEW (2026-07-25): parsecd is PRESENT but DEREGISTERED -- it shows OFFLINE
#      to Zeke's client while this script kept logging "ok (parsecd present)".
#      Happened twice on 2026-07-24; each needed a manual kill+relaunch.
#
# The connectivity probe (step 3) fixes #3. Positive ONLINE signal = parsecd
# holds an ESTABLISHED outbound TCP:443 to Parsec cloud (verified 2026-07-25:
# parsecd -> 104.18.x.x:443 when registered). If parsecd is present but has NO
# :443 cloud connection, it's stale -> restart it.
#
# HARD SAFETY RULE: NEVER restart parsecd during an ACTIVE SESSION. When a
# client is connected, %APPDATA%\Parsec\log.txt streams "FPS:" / "cg event"
# lines every 1-2s. If those are fresh, we DO NOTHING -- a restart would cut
# Zeke off mid-stream. This guard makes the probe strictly no-worse-than-before.
#
# Run by scheduled task `Iris-Parsec-Heal` as interactive Owner (so parsecd
# lands in session 1 and can host), at logon AND every 10 min. Pure PowerShell.
#
# Manual test:        powershell -File scripts\parsec_ensure.ps1
# Safe dry-run test:  powershell -File scripts\parsec_ensure.ps1 -DryRun
#   (-DryRun reports what it WOULD do and never touches parsecd -- safe to run
#    while Zeke is connected.)

param([switch]$DryRun)

$ErrorActionPreference = 'SilentlyContinue'
$log = 'D:\Wren-Companion\state\parsec_heal.log'
$parsecd = 'C:\Program Files\Parsec\parsecd.exe'

function Write-Log([string]$m) {
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $prefix = if ($DryRun) { '[dry-run] ' } else { '' }
    # .NET AppendAllText = UTF8 no-BOM, works on Windows PowerShell 5.1
    # (where -Encoding utf8NoBOM does not exist and would silently fail).
    [System.IO.File]::AppendAllText($log, "$stamp  $prefix$m`r`n")
}

$changed = $false

# 1. Parsec service running?
$svc = Get-Service -Name 'Parsec' -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne 'Running') {
    if ($DryRun) { Write-Log "WOULD start Parsec service (is $($svc.Status))"; $changed = $true }
    else {
        try { Start-Service -Name 'Parsec' -ErrorAction Stop; Write-Log "started Parsec service (was $($svc.Status))"; $changed = $true }
        catch { Write-Log "FAILED to start Parsec service: $($_.Exception.Message)" }
    }
}

# 2. parsecd.exe host app running?
$proc = Get-CimInstance Win32_Process -Filter "Name='parsecd.exe'" -ErrorAction SilentlyContinue
if (-not $proc) {
    if (Test-Path $parsecd) {
        if ($DryRun) { Write-Log 'WOULD launch parsecd.exe (absent)'; $changed = $true }
        else {
            try {
                Start-Process $parsecd -ArgumentList 'app_silent=1' -ErrorAction Stop
                Write-Log 'launched parsecd.exe (was absent)'
                $changed = $true
            } catch { Write-Log "FAILED to launch parsecd.exe: $($_.Exception.Message)" }
        }
    } else {
        Write-Log "parsecd.exe NOT FOUND at $parsecd"
    }
}

# 3. Connectivity/staleness probe (2026-07-25). Only when parsecd is present
#    and nothing above already acted this run.
if ($proc -and -not $changed) {
    # a) ACTIVE-SESSION GUARD (hard): fresh FPS/cg-event lines = client connected.
    $activeSession = $false
    try {
        $logPath = Join-Path $env:APPDATA 'Parsec\log.txt'
        if (Test-Path $logPath) {
            $secsSinceWrite = ((Get-Date) - (Get-Item $logPath).LastWriteTime).TotalSeconds
            if ($secsSinceWrite -lt 90) {
                $tail = Get-Content $logPath -Tail 30 -ErrorAction SilentlyContinue
                if (($tail -match 'FPS:') -or ($tail -match 'cg event')) { $activeSession = $true }
            }
        }
    } catch {}

    if ($activeSession) {
        Write-Log 'active session detected -> connectivity probe skipped (no restart)'
    } else {
        # b) ONLINE signal: any parsecd PID with an ESTABLISHED remote :443.
        $pids = @($proc.ProcessId)
        $online = $false
        try {
            $online = [bool](Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue |
                Where-Object { ($pids -contains $_.OwningProcess) -and ($_.RemotePort -eq 443) })
        } catch {}

        # c) GRACE: only heal a daemon old enough to have connected (>90s).
        $ageOk = $true
        try {
            $oldest = ($proc | ForEach-Object { $_.CreationDate } | Sort-Object | Select-Object -First 1)
            if ($oldest) { $ageOk = (((Get-Date) - $oldest).TotalSeconds -gt 90) }
        } catch {}

        if ($online) {
            # healthy + registered; the final "ok" line logs below.
        } elseif (-not $ageOk) {
            Write-Log 'parsecd present, no cloud :443 yet, but <90s old -> still connecting, wait'
        } else {
            # STALE: present, no cloud connection, no active session, had time to connect.
            if ($DryRun) {
                Write-Log 'WOULD restart STALE parsecd (present, no cloud :443, no active session)'
                $changed = $true
            } else {
                Write-Log 'STALE: parsecd present but no cloud :443 + no active session -> restarting'
                $proc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
                Start-Sleep -Seconds 2
                if (Test-Path $parsecd) {
                    Start-Process $parsecd -ArgumentList 'app_silent=1' -ErrorAction SilentlyContinue
                    Write-Log 'relaunched parsecd after stale-heal'
                    $changed = $true
                }
            }
        }
    }
}

if (-not $changed) { Write-Log 'ok (service running, parsecd present + online)' }
