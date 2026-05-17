# scripts/iris_watchdog.ps1 -- external respawn watchdog for Iris's CC session.
#
# Runs continuously in the background. Watches for a trigger file. When the
# trigger appears, kills the named CC session (if still alive) and launches
# a fresh one against D:\Wren-Companion\. Then waits for the next trigger.
#
# Run manually:
#   powershell.exe -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\iris_watchdog.ps1
#
# Or schedule via Task Scheduler at logon for permanence.

$ErrorActionPreference = "Continue"

# ---- Config ----
$ROOT = "D:\Wren-Companion"
$TRIGGER_FILE = Join-Path $ROOT ".tmp\restart_cc.flag"
$WATCHDOG_LOG = Join-Path $ROOT ".tmp\watchdog.log"
$POLL_INTERVAL_S = 2
$DEBOUNCE_S = 30

# ---- Utilities ----
function Write-WatchLog($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] $msg"
    try {
        $logDir = Split-Path $WATCHDOG_LOG -Parent
        if (-not (Test-Path $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
        Add-Content -Path $WATCHDOG_LOG -Value $line -Encoding UTF8
    } catch {
        # log write failed -- nothing to do
    }
    Write-Host $line
}

function Find-ClaudeCommand {
    $cli = Get-Command claude -ErrorAction SilentlyContinue
    if ($cli) {
        return $cli.Source
    }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\claude\Claude.exe",
        "$env:LOCALAPPDATA\Programs\Claude Code\Claude Code.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) {
            return $p
        }
    }
    return $null
}

function Find-ActiveCC {
    try {
        $names = @("Claude.exe", "Claude Code.exe", "node.exe")
        $all = @()
        foreach ($n in $names) {
            $p = Get-CimInstance Win32_Process -Filter "Name='$n'" -ErrorAction SilentlyContinue
            if ($p) {
                $all += $p
            }
        }
        $matching = $all | Where-Object {
            $_.CommandLine -and ($_.CommandLine -like "*Wren-Companion*" -or $_.CommandLine -like "*claude*")
        }
        return @($matching)
    } catch {
        return @()
    }
}

function Kill-ActiveCC {
    $procs = Find-ActiveCC
    if ($procs.Count -eq 0) {
        Write-WatchLog "no active CC session found"
        return
    }
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-WatchLog ("stopped PID " + $p.ProcessId + " (" + $p.Name + ")")
        } catch {
            Write-WatchLog ("failed to stop PID " + $p.ProcessId + ": " + $_)
        }
    }
    Start-Sleep -Seconds 1
}

function Spawn-NewCC {
    # Prefer start_iris.bat — it sets AVA_TTS_ENGINE + dev-channel flags
    # (server:iris on both --dangerously-load-development-channels and
    # --channels). Bare `claude` skips the channel flags and breaks fam-chat
    # + wake-word event delivery. start_iris.bat is the source of truth for
    # how Iris is supposed to launch; the watchdog must respect it.
    $batPath = Join-Path $ROOT "start_iris.bat"
    if (Test-Path $batPath) {
        try {
            Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$batPath`"" -WindowStyle Normal
            Write-WatchLog ("launched new CC via start_iris.bat")
            return $true
        } catch {
            Write-WatchLog ("start_iris.bat launch failed, falling through: " + $_)
        }
    } else {
        Write-WatchLog ("WARN: start_iris.bat not found at $batPath, falling back to bare claude")
    }
    # Fallback: bare claude (loses channel flags + env var, but better than nothing)
    $cmd = Find-ClaudeCommand
    if (-not $cmd) {
        Write-WatchLog "FATAL: could not find claude executable"
        return $false
    }
    try {
        $args = "-NoExit -Command `"cd '$ROOT'; & '$cmd'`""
        Start-Process -FilePath "powershell.exe" -ArgumentList $args -WindowStyle Normal
        Write-WatchLog ("launched new CC via fallback bare " + $cmd + " (channels + env var lost)")
        return $true
    } catch {
        Write-WatchLog ("FATAL: launch failed: " + $_)
        return $false
    }
}

# ---- Main loop ----
Write-WatchLog "watchdog starting (trigger=$TRIGGER_FILE poll=${POLL_INTERVAL_S}s debounce=${DEBOUNCE_S}s)"

$tmpDir = Split-Path $TRIGGER_FILE -Parent
if (-not (Test-Path $tmpDir)) {
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
}

$lastRespawn = [DateTime]::MinValue

while ($true) {
    try {
        if (Test-Path $TRIGGER_FILE) {
            $sinceLast = (Get-Date) - $lastRespawn
            if ($sinceLast.TotalSeconds -lt $DEBOUNCE_S) {
                Write-WatchLog ("trigger seen within debounce window (" + [int]$sinceLast.TotalSeconds + "s) -- clearing")
                Remove-Item -Path $TRIGGER_FILE -Force -ErrorAction SilentlyContinue
            } else {
                $reason = ""
                try {
                    $reason = (Get-Content $TRIGGER_FILE -Raw -ErrorAction SilentlyContinue).Trim()
                } catch {
                    $reason = "(could not read reason)"
                }
                Write-WatchLog ("trigger detected -- respawning. reason=" + $reason)
                Remove-Item -Path $TRIGGER_FILE -Force -ErrorAction SilentlyContinue
                Kill-ActiveCC
                Start-Sleep -Seconds 2
                $ok = Spawn-NewCC
                if ($ok) {
                    $lastRespawn = Get-Date
                    Write-WatchLog "respawn complete"
                } else {
                    Write-WatchLog "respawn failed -- CC is down. Manual restart required."
                }
            }
        }
    } catch {
        Write-WatchLog ("watchdog loop error: " + $_)
    }
    Start-Sleep -Seconds $POLL_INTERVAL_S
}