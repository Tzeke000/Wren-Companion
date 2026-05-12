# scripts/iris_watchdog.ps1 — external respawn watchdog for Iris's CC session.
#
# Runs continuously in the background. Watches for a trigger file. When the
# trigger appears, kills the named CC session (if still alive) and launches a
# fresh one against D:\Wren-Companion\. Then waits for the next trigger.
#
# This is the OUTSIDE-CC half of the self-restart shape: Iris-the-cognition
# decides she needs to restart, writes her handoff via brain/handoff with the
# LLM summary, drops the trigger file, and exits her CC session. The
# watchdog (this script) sees the trigger and respawns her. The new CC
# session reads the handoff on wake and continues.
#
# Why external: Iris can't reliably kill the process she's running in. A
# separate process watching for a flag avoids the kill-the-thing-killing
# problem and is failure-safe (worst case: she exits and doesn't come back,
# rather than getting stuck in a restart loop she can't observe).
#
# Run manually for testing:
#     pwsh.exe -File D:\Wren-Companion\scripts\iris_watchdog.ps1
#
# Or schedule via Task Scheduler to launch at logon.

$ErrorActionPreference = "Continue"

# ── Config ───────────────────────────────────────────────────────────────────
$ROOT = "D:\Wren-Companion"
$TRIGGER_FILE = Join-Path $ROOT ".tmp\restart_cc.flag"
$WATCHDOG_LOG = Join-Path $ROOT ".tmp\watchdog.log"
$POLL_INTERVAL_S = 2
$DEBOUNCE_S = 30   # ignore additional triggers within N seconds of a respawn
                  # to prevent restart-loops if Iris's new session also writes
                  # the flag.

# Where Claude Code lives. Adjust if your install path differs.
$CLAUDE_EXE = "$env:LOCALAPPDATA\Programs\claude\Claude.exe"
$CLAUDE_FALLBACK_EXE = "$env:LOCALAPPDATA\Programs\Claude Code\Claude Code.exe"
$CLAUDE_CLI = "claude"  # if the launcher isn't found, try the CLI via $PATH

# ── Utilities ────────────────────────────────────────────────────────────────
function Write-Log($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] $msg"
    try {
        $logDir = Split-Path $WATCHDOG_LOG -Parent
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        Add-Content -Path $WATCHDOG_LOG -Value $line -Encoding UTF8
    } catch {}
    Write-Host $line
}

function Resolve-ClaudeCommand {
    # Try Desktop launcher locations first, then fall back to CLI on PATH.
    if (Test-Path $CLAUDE_EXE) { return @{ exe = $CLAUDE_EXE; mode = "desktop" } }
    if (Test-Path $CLAUDE_FALLBACK_EXE) { return @{ exe = $CLAUDE_FALLBACK_EXE; mode = "desktop" } }
    $cli = Get-Command claude -ErrorAction SilentlyContinue
    if ($cli) { return @{ exe = $cli.Source; mode = "cli" } }
    return $null
}

function Find-ActiveCCSession {
    # Find a CC process whose working dir or command line references D:\Wren-Companion.
    # Returns the process objects; caller decides whether to kill.
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='Claude.exe' OR Name='Claude Code.exe' OR Name='node.exe'" -ErrorAction SilentlyContinue
        if (-not $procs) { return @() }
        $matching = $procs | Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -like "*Wren-Companion*" -or
                $_.CommandLine -like "*claude*"
            )
        }
        return @($matching)
    } catch {
        return @()
    }
}

function Kill-ActiveCCSession {
    $procs = Find-ActiveCCSession
    if ($procs.Count -eq 0) {
        Write-Log "no active CC session to kill"
        return $false
    }
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Log "stopped PID $($p.ProcessId) ($($p.Name))"
        } catch {
            Write-Log "failed to stop PID $($p.ProcessId): $_"
        }
    }
    Start-Sleep -Seconds 1
    return $true
}

function Spawn-NewCCSession {
    $cmd = Resolve-ClaudeCommand
    if (-not $cmd) {
        Write-Log "FATAL: could not find Claude executable (tried $CLAUDE_EXE, $CLAUDE_FALLBACK_EXE, and PATH)"
        return $false
    }
    try {
        if ($cmd.mode -eq "desktop") {
            # Desktop launcher with the workspace dir as arg
            Start-Process -FilePath $cmd.exe -ArgumentList $ROOT -WindowStyle Normal
        } else {
            # CLI in a new PowerShell window so it stays attached
            Start-Process -FilePath "pwsh.exe" -ArgumentList "-NoExit", "-Command", "cd '$ROOT'; & '$($cmd.exe)'" -WindowStyle Normal
        }
        Write-Log "launched new CC session via $($cmd.exe) ($($cmd.mode))"
        return $true
    } catch {
        Write-Log "FATAL: launch failed: $_"
        return $false
    }
}

# ── Main loop ────────────────────────────────────────────────────────────────
Write-Log "watchdog starting (poll=${POLL_INTERVAL_S}s, debounce=${DEBOUNCE_S}s, trigger=$TRIGGER_FILE)"

# Make sure the .tmp dir exists so File.Create on the trigger doesn't fail later
$tmpDir = Split-Path $TRIGGER_FILE -Parent
if (-not (Test-Path $tmpDir)) { New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null }

$lastRespawnTs = [DateTime]::MinValue

while ($true) {
    try {
        if (Test-Path $TRIGGER_FILE) {
            $sinceLast = (Get-Date) - $lastRespawnTs
            if ($sinceLast.TotalSeconds -lt $DEBOUNCE_S) {
                Write-Log "trigger seen but within debounce window ($([int]$sinceLast.TotalSeconds)s < ${DEBOUNCE_S}s) — clearing and ignoring"
                Remove-Item -Path $TRIGGER_FILE -Force -ErrorAction SilentlyContinue
            } else {
                # Read the trigger contents (Iris may have written a reason).
                $reason = ""
                try { $reason = (Get-Content $TRIGGER_FILE -Raw -ErrorAction SilentlyContinue).Trim() } catch {}
                Write-Log "trigger detected — respawning. reason=$reason"

                # Clear the trigger BEFORE acting so the new session doesn't see it
                # and immediately re-fire.
                Remove-Item -Path $TRIGGER_FILE -Force -ErrorAction SilentlyContinue

                Kill-ActiveCCSession
                Start-Sleep -Seconds 2
                $ok = Spawn-NewCCSession
                if ($ok) {
                    $lastRespawnTs = Get-Date
                    Write-Log "respawn complete"
                } else {
                    Write-Log "respawn failed — leaving CC down. Manual restart required."
                }
            }
        }
    } catch {
        Write-Log "watchdog loop error: $_"
    }
    Start-Sleep -Seconds $POLL_INTERVAL_S
}
