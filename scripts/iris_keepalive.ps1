# scripts/iris_keepalive.ps1 -- independent failsafe net for watchdog + CC liveness.
#
# Designed to run via Windows Task Scheduler every 5 minutes. Independent of
# iris_watchdog.ps1: if BOTH self-restart AND the watchdog fail catastrophically,
# this catches Iris within ~5 min and brings her back to at least bare-claude tier.
#
# Two checks:
#   1. Watchdog liveness -- verify .tmp/watchdog_heartbeat.txt updated in last 30s.
#      If stale, kill watchdog PID (in case it's a zombie) and respawn fresh.
#   2. CC liveness -- verify claude.exe with Wren-Companion path is running.
#      If absent, run start_iris.bat (full tier 2 path). If start_iris.bat fails
#      to produce a claude PID within 30s, fall to tier 3 (bare claude.cmd).
#
# Run manually for diagnostic:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\iris_keepalive.ps1
#
# This script EXITS after one check cycle -- it is NOT a long-running daemon.
# Task Scheduler is the periodicity mechanism.

$ErrorActionPreference = "Continue"

# ---- Config ----
$ROOT = "D:\Wren-Companion"
$WATCHDOG_HEARTBEAT = Join-Path $ROOT ".tmp\watchdog_heartbeat.txt"
$WATCHDOG_SCRIPT = Join-Path $ROOT "scripts\iris_watchdog.ps1"
$START_IRIS_BAT = Join-Path $ROOT "start_iris.bat"
$KEEPALIVE_LOG = Join-Path $ROOT ".tmp\keepalive.log"
$HEARTBEAT_STALE_S = 30      # heartbeat older than this means watchdog is dead
$CC_SPAWN_TIMEOUT_S = 30     # how long to wait for claude PID after spawning

# ---- Logging ----
function Write-KaLog($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] $msg"
    try {
        $logDir = Split-Path $KEEPALIVE_LOG -Parent
        if (-not (Test-Path $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
        # Cap log at 1MB; rotate to .old on overflow.
        if (Test-Path $KEEPALIVE_LOG) {
            try {
                $sz = (Get-Item $KEEPALIVE_LOG -ErrorAction SilentlyContinue).Length
                if ($sz -gt 1048576) {
                    $old = "$KEEPALIVE_LOG.old"
                    if (Test-Path $old) { Remove-Item -Path $old -Force -ErrorAction SilentlyContinue }
                    Move-Item -Path $KEEPALIVE_LOG -Destination $old -Force -ErrorAction SilentlyContinue
                }
            } catch { }
        }
        Add-Content -Path $KEEPALIVE_LOG -Value $line -Encoding UTF8
    } catch { }
    Write-Host $line
}

# ---- Watchdog health check ----
function Test-WatchdogAlive {
    if (-not (Test-Path $WATCHDOG_HEARTBEAT)) {
        return @{ alive = $false; reason = "heartbeat file missing"; age = -1 }
    }
    try {
        $content = (Get-Content -Path $WATCHDOG_HEARTBEAT -Raw -ErrorAction Stop).Trim()
        if (-not $content) {
            return @{ alive = $false; reason = "heartbeat file empty"; age = -1 }
        }
        $hbTs = [int64]$content
        $now = [DateTimeOffset]::Now.ToUnixTimeSeconds()
        $age = $now - $hbTs
        if ($age -gt $HEARTBEAT_STALE_S) {
            return @{ alive = $false; reason = "stale heartbeat"; age = $age }
        }
        return @{ alive = $true; reason = "fresh heartbeat"; age = $age }
    } catch {
        return @{ alive = $false; reason = "heartbeat read error: $_"; age = -1 }
    }
}

function Get-WatchdogPIDs {
    try {
        $wds = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -OperationTimeoutSec 10 -ErrorAction Stop | Where-Object {
            $_.CommandLine -and $_.CommandLine -like "*iris_watchdog.ps1*"
        }
        return @($wds | ForEach-Object { $_.ProcessId })
    } catch {
        Write-KaLog ("[WMI ERROR] Get-WatchdogPIDs failed: " + $_)
        return @()
    }
}

function Start-Watchdog {
    try {
        Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-WindowStyle","Hidden","-File",$WATCHDOG_SCRIPT `
            -WindowStyle Hidden
        Write-KaLog "spawned new watchdog via powershell -File"
        return $true
    } catch {
        Write-KaLog ("FATAL: watchdog spawn failed: " + $_)
        return $false
    }
}

function Repair-Watchdog {
    Write-KaLog "watchdog repair: killing existing PIDs + respawning"
    foreach ($pid_ in Get-WatchdogPIDs) {
        try {
            Stop-Process -Id $pid_ -Force -ErrorAction Stop
            Write-KaLog "  stopped zombie watchdog PID $pid_"
        } catch {
            Write-KaLog "  failed to stop PID $pid_ -- $_"
        }
    }
    Start-Sleep -Seconds 1
    $ok = Start-Watchdog
    if (-not $ok) { return $false }
    # Verify new heartbeat appears within 10s
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        $h = Test-WatchdogAlive
        if ($h.alive) {
            Write-KaLog "  new watchdog confirmed alive (heartbeat age $($h.age)s)"
            return $true
        }
        Start-Sleep -Seconds 1
    }
    Write-KaLog "WARN: respawned watchdog but heartbeat not appearing within 10s"
    return $false
}

# ---- CC liveness check ----
# Detection by process-name only (no cmdline filter). Win32_Process CommandLine
# returns NULL for higher-integrity processes when read by a non-elevated caller,
# which made the previous cmdline-based filter miss watchdog-spawned CCs and
# triggered duplicate spawns (see handoff 2026-05-20 race). False positive
# (zombie claude.exe counted as alive) is preferable to false negative
# (duplicate CC spawned because real CC was invisible).
function Get-CCProcessSet {
    $set = @{}
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='claude.exe'" -OperationTimeoutSec 10 -ErrorAction SilentlyContinue
        if ($procs) {
            foreach ($p in $procs) {
                $set[[int]$p.ProcessId] = $true
            }
        }
    } catch { }
    return $set
}

function Test-CCAlive {
    $set = Get-CCProcessSet
    return $set.Count -gt 0
}

# ---- Restart-in-flight check ----
# If watchdog just respawned CC, or a restart flag was just written, hold off on
# spawning a duplicate. Window is generous (120s for flag, 60s for log entry) to
# cover the full kill -> tier-2 spawn -> claude PID visible -> iris_runtime
# bootstrap window.
function Test-RestartInFlight {
    $flagPath = Join-Path $ROOT ".tmp\restart_cc.flag"
    if (Test-Path $flagPath) {
        try {
            $flagAge = ((Get-Date) - (Get-Item $flagPath).LastWriteTime).TotalSeconds
            if ($flagAge -lt 120) {
                return @{ inflight = $true; reason = "restart_cc.flag mtime ${flagAge}s"; age = $flagAge }
            }
        } catch { }
    }
    $wdLog = Join-Path $ROOT ".tmp\watchdog.log"
    if (Test-Path $wdLog) {
        try {
            $wdAge = ((Get-Date) - (Get-Item $wdLog).LastWriteTime).TotalSeconds
            if ($wdAge -lt 60) {
                return @{ inflight = $true; reason = "watchdog.log mtime ${wdAge}s"; age = $wdAge }
            }
        } catch { }
    }
    return @{ inflight = $false; reason = ""; age = -1 }
}

function Find-ClaudeCommand {
    $candidates = @(
        "$env:APPDATA\npm\claude.cmd",
        "$env:LOCALAPPDATA\Programs\claude\claude.cmd",
        "$env:LOCALAPPDATA\Programs\Claude Code\claude.cmd"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { return $p }
    }
    $cli = Get-Command claude -ErrorAction SilentlyContinue
    if ($cli) { return $cli.Source }
    return $null
}

function Spawn-CC {
    Write-KaLog "CC spawn: tier 1 (start_iris.bat via cmd /k)"
    $baseline = Get-CCProcessSet
    if (Test-Path $START_IRIS_BAT) {
        try {
            Start-Process -FilePath "cmd.exe" -ArgumentList "/k", "`"$START_IRIS_BAT`"" -WindowStyle Normal
            $deadline = (Get-Date).AddSeconds($CC_SPAWN_TIMEOUT_S)
            while ((Get-Date) -lt $deadline) {
                $current = Get-CCProcessSet
                foreach ($k in $current.Keys) {
                    if (-not $baseline.ContainsKey($k)) {
                        Write-KaLog "  tier 1 confirmed: new claude/node PID $k"
                        return $true
                    }
                }
                Start-Sleep -Seconds 2
            }
            Write-KaLog "  tier 1 timed out -- start_iris.bat may have crashed"
        } catch {
            Write-KaLog ("  tier 1 threw: " + $_)
        }
    } else {
        Write-KaLog "  WARN: start_iris.bat not found, falling to tier 2"
    }

    # Tier 2: bare claude.cmd via powershell -NoExit (loses channel flags)
    Write-KaLog "CC spawn: tier 2 (bare claude.cmd)"
    $cmd = Find-ClaudeCommand
    if (-not $cmd) {
        Write-KaLog "FATAL: claude executable not found anywhere"
        return $false
    }
    try {
        $psArgs = "-NoExit -Command `"cd '$ROOT'; & '$cmd'`""
        Start-Process -FilePath "powershell.exe" -ArgumentList $psArgs -WindowStyle Normal
        Write-KaLog "  tier 2 fired: bare $cmd (channels + env var lost)"
        return $true
    } catch {
        Write-KaLog ("  tier 2 threw: " + $_)
        return $false
    }
}

# ---- Main check cycle ----
Write-KaLog "keepalive check starting"

# Check 1: watchdog
$wdHealth = Test-WatchdogAlive
if (-not $wdHealth.alive) {
    Write-KaLog "watchdog UNHEALTHY: $($wdHealth.reason) (age=$($wdHealth.age)s)"
    Repair-Watchdog | Out-Null
} else {
    Write-KaLog "watchdog OK (heartbeat age $($wdHealth.age)s)"
}

# Check 2: CC
if (Test-CCAlive) {
    Write-KaLog "CC OK (claude process alive)"
} else {
    # Backoff: if watchdog/restart-flag activity is recent, don't race.
    $rh = Test-RestartInFlight
    if ($rh.inflight) {
        Write-KaLog "CC ABSENT but restart in flight ($($rh.reason)) -- backing off"
    } else {
        Write-KaLog "CC ABSENT -- spawning"
        $ok = Spawn-CC
        if ($ok) {
            Write-KaLog "CC spawn complete"
        } else {
            Write-KaLog "CC spawn FAILED at all tiers -- manual intervention needed"
        }
    }
}

Write-KaLog "keepalive check complete"
