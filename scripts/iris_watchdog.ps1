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
$WATCHDOG_HEARTBEAT = Join-Path $ROOT ".tmp\watchdog_heartbeat.txt"
$POLL_INTERVAL_S = 2
# 2026-08-05: 30 -> 120. The 08-04 double-cognition race
# (double_cognition_restart_race_2026-08-04) had a ~47s window between the
# runtime watchdog's relaunch and this poller acting on a manual flag — 30s
# could not bridge it. 120s covers the measured window with slack.
$DEBOUNCE_S = 120
# Stand-down freshness window (s): if a claude-family process this young
# exists when a trigger fires, another restart authority just acted — do NOT
# kill it and spawn a twin (the 08-04 race: killed 28816, spawned a CLI twin
# into an already-restarting stack).
$FRESH_STACK_S = 180

# ---- Utilities ----
function Write-WatchLog($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] $msg"
    try {
        $logDir = Split-Path $WATCHDOG_LOG -Parent
        if (-not (Test-Path $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
        # W4: cap log at 1MB; rotate to .old on overflow.
        if (Test-Path $WATCHDOG_LOG) {
            try {
                $sz = (Get-Item $WATCHDOG_LOG -ErrorAction SilentlyContinue).Length
                if ($sz -gt 1048576) {
                    $old = "$WATCHDOG_LOG.old"
                    if (Test-Path $old) { Remove-Item -Path $old -Force -ErrorAction SilentlyContinue }
                    Move-Item -Path $WATCHDOG_LOG -Destination $old -Force -ErrorAction SilentlyContinue
                }
            } catch {
                # rotation failed -- keep appending
            }
        }
        Add-Content -Path $WATCHDOG_LOG -Value $line -Encoding UTF8
    } catch {
        # log write failed -- nothing to do
    }
    Write-Host $line
}

function Find-ClaudeCommand {
    # W2: npm shim ships claude.cmd, not .exe. Check both, and fall back to
    # Get-Command if the hardcoded paths miss.
    $candidates = @(
        "$env:APPDATA\npm\claude.cmd",
        "$env:LOCALAPPDATA\Programs\claude\claude.cmd",
        "$env:LOCALAPPDATA\Programs\claude\Claude.exe",
        "$env:LOCALAPPDATA\Programs\Claude Code\claude.cmd",
        "$env:LOCALAPPDATA\Programs\Claude Code\Claude Code.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) {
            return $p
        }
    }
    $cli = Get-Command claude -ErrorAction SilentlyContinue
    if ($cli) {
        return $cli.Source
    }
    return $null
}

function Find-ActiveCC {
    # W6: distinguish WMI failure ($null) from "no matches" (@()).
    # Caller treats $null as "unknown, skip kill" to avoid double-spawn
    # when the WMI service is unhealthy.
    try {
        $names = @("Claude.exe", "Claude Code.exe", "claude.exe", "node.exe")
        $all = @()
        $wmiOk = $false
        foreach ($n in $names) {
            try {
                $p = Get-CimInstance Win32_Process -Filter "Name='$n'" -OperationTimeoutSec 10 -ErrorAction Stop
                $wmiOk = $true
                if ($p) { $all += $p }
            } catch {
                Write-WatchLog ("[WMI ERROR] query for $n failed: " + $_)
                return $null
            }
        }
        if (-not $wmiOk) {
            Write-WatchLog "[WMI ERROR] no successful query across any name"
            return $null
        }
        # W1: require BOTH claude-shaped cmdline AND Wren-Companion path.
        # For .exe-named claude processes we accept either (claude.exe alone
        # in this repo is ours); for node.exe we require both signals so we
        # don't reap unrelated npm/npx node children.
        $matching = $all | Where-Object {
            if (-not $_.CommandLine) { return $false }
            $cl = $_.CommandLine
            $isClaudeExe = ($_.Name -ieq "Claude.exe") -or ($_.Name -ieq "claude.exe") -or ($_.Name -ieq "Claude Code.exe")
            if ($isClaudeExe) {
                return ($cl -like "*Wren-Companion*") -or ($cl -like "*claude*")
            }
            # node.exe path: require Wren-Companion AND a claude-code marker
            return ($cl -like "*Wren-Companion*") -and (
                ($cl -like "*claude-code*") -or ($cl -like "*claude\node_modules*") -or ($cl -like "*\claude\*")
            )
        }
        return @($matching)
    } catch {
        Write-WatchLog ("[WMI ERROR] Find-ActiveCC outer catch: " + $_)
        return $null
    }
}

function Get-YoungestCCAgeSeconds {
    # Age in seconds of the YOUNGEST claude-family process, or $null if none
    # (or WMI failed). Used by the trigger path to stand down when another
    # restart authority (runtime watchdog / tower sentinel) just relaunched
    # the stack (2026-08-05, from the 08-04 double-cognition race).
    $procs = Find-ActiveCC
    if ($null -eq $procs -or $procs.Count -eq 0) { return $null }
    $now = Get-Date
    $ages = @()
    foreach ($p in $procs) {
        try {
            if ($p.CreationDate) { $ages += ($now - $p.CreationDate).TotalSeconds }
        } catch { }
    }
    if ($ages.Count -eq 0) { return $null }
    return ($ages | Measure-Object -Minimum).Minimum
}

function Kill-ActiveCC {
    $procs = Find-ActiveCC
    # W6: $null means WMI failed -- skip kill rather than assuming clean.
    if ($null -eq $procs) {
        Write-WatchLog "Find-ActiveCC returned null (WMI error); skipping kill step to avoid double-spawn"
        return
    }
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

# ---- claude.exe presence check (used for bat-crash detection) ----
# Returns a hashtable of claude.exe-family PIDs currently running. We compare
# pre-spawn vs post-spawn snapshots: if no new PID appeared after the wait
# window, the bat must have crashed inside its window (pywinpty error,
# python syntax error, etc.) and we need to fall through to bare-claude.
#
# We include node.exe because CC's actual cognition runs as node child
# processes under the claude CLI on Windows — a healthy launch produces
# new node.exe PIDs with "claude" or "Wren-Companion" in the command line.
function Get-ClaudeProcessSet {
    $set = @{}
    try {
        $names = @("claude.exe", "Claude.exe", "Claude Code.exe", "node.exe")
        foreach ($n in $names) {
            $procs = Get-CimInstance Win32_Process -Filter "Name='$n'" -OperationTimeoutSec 10 -ErrorAction SilentlyContinue
            if ($procs) {
                foreach ($p in $procs) {
                    if ($p.CommandLine -and (
                            $p.CommandLine -like "*claude*" -or
                            $p.CommandLine -like "*Wren-Companion*"
                        )) {
                        $set[[int]$p.ProcessId] = $true
                    }
                }
            }
        }
    } catch {
        # if WMI fails, return whatever we collected — caller treats empty
        # set as "no claude running" which is the safe default for detection
    }
    return $set
}

# Wait up to $TimeoutS seconds for a new claude/node PID (not in $Baseline)
# to appear. Returns $true on success, $false on timeout. Polls every 2s so
# we don't bail too early on a slow cold start.
# 2026-08-05: 30 -> 120. Live fire 00:18 spawned start_iris_v2_fable.bat via
# tier 2; the SDK stack took ~65s (spawn 00:18:50 -> claude PID 00:19:55) but
# the 30s wait gave up at 00:19:22 and fired tier 3, creating a bare-claude
# TWIN alongside the healthy bat stack. 30s was tuned for the CLI path; the
# SDK host (bat -> cold_wake -> python -> bundled claude) needs the slack.
function Wait-ForNewClaudeProcess {
    param(
        [hashtable]$Baseline,
        [int]$TimeoutS = 120
    )
    $deadline = (Get-Date).AddSeconds($TimeoutS)
    while ((Get-Date) -lt $deadline) {
        $current = Get-ClaudeProcessSet
        foreach ($procId in $current.Keys) {
            if (-not $Baseline.ContainsKey($procId)) {
                return $true
            }
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Spawn-BareClaude {
    # Last-resort tier: bare `claude` via powershell -NoExit. Loses the
    # --channels flag + AVA_TTS_ENGINE env var that start_iris.bat would
    # have set, but a degraded CC is better than no CC at all.
    $cmd = Find-ClaudeCommand
    if (-not $cmd) {
        Write-WatchLog "FATAL: bare-claude fallback unreachable — claude executable not found"
        return $false
    }
    try {
        $args = "-NoExit -Command `"cd '$ROOT'; & '$cmd'`""
        Start-Process -FilePath "powershell.exe" -ArgumentList $args -WindowStyle Normal
        Write-WatchLog ("tier 3 fired: bare " + $cmd + " (channels + env var lost)")
        return $true
    } catch {
        Write-WatchLog ("FATAL: bare-claude launch failed: " + $_)
        return $false
    }
}

function Spawn-NewCC {
    # Prefer start_iris.bat — it sets AVA_TTS_ENGINE + dev-channel flags
    # (server:iris on both --dangerously-load-development-channels and
    # --channels). Bare `claude` skips the channel flags and breaks fam-chat
    # + wake-word event delivery. start_iris.bat is the source of truth for
    # how Iris is supposed to launch; the watchdog must respect it.
    #
    # 2026-05-17: launch via Windows Terminal (wt.exe) so iris_cold_wake.py's
    # pty stdout has a visible window to render to. An even-earlier version used
    # a HIDDEN-watchdog `cmd.exe /c`, which produced no visible window and made
    # restart_self look like it had failed (Zeke never saw CC come back up after
    # the 15:33 watchdog respawn). Fallback chain: wt.exe → cmd /c → bare claude.
    #
    # 2026-06-26: switched the visible spawns back to `/c` (from the interim `/k`).
    # The `/k` was added for window visibility, but iris_cold_wake.py BLOCKS until
    # CC exits, so `/c` is already visible for the whole session AND closes cleanly
    # at the end. `/k` only kept the DEAD shell open afterward — one orphan "Iris…"
    # window per restart, which read as a second half-attached session.
    #
    # 2026-05-17 (later): added crash-detection between tiers. Tier 1 (wt)
    # and tier 2 (cmd /c) both route through start_iris.bat → iris_cold_wake.py.
    # If that script crashes (pywinpty error, syntax error, anything that
    # exits before claude actually launches), the wt/cmd window opens fine
    # — Start-Process reports success — but no claude.exe ever spawns inside
    # it. So we snapshot claude PIDs BEFORE spawning, wait up to 30s, and
    # check whether any new PID appeared. If not, fall through to the next
    # tier. This makes tier 3 (bare claude) actually reachable when the
    # cold-wake script itself is the failure point.
    # 2026-08-05: honor state/boot_launcher.txt — the SAME source of truth the
    # runtime watchdog + tower sentinel use. Hardcoding start_iris.bat here is
    # what spawned the CLI twin on 08-04 while the rest of the chain launched
    # start_iris_v2_fable.bat. Fallback to start_iris.bat if the file is
    # missing/empty/points at a bat that doesn't exist.
    $batPath = Join-Path $ROOT "start_iris.bat"
    $launcherFile = Join-Path $ROOT "state\boot_launcher.txt"
    try {
        if (Test-Path $launcherFile) {
            $launcherName = (Get-Content $launcherFile -Raw -ErrorAction Stop).Trim()
            if ($launcherName) {
                $candidate = Join-Path $ROOT $launcherName
                if (Test-Path $candidate) {
                    $batPath = $candidate
                    Write-WatchLog ("launcher resolved from boot_launcher.txt: " + $launcherName)
                } else {
                    Write-WatchLog ("WARN: boot_launcher.txt names '" + $launcherName + "' but it doesn't exist — falling back to start_iris.bat")
                }
            }
        }
    } catch {
        Write-WatchLog ("WARN: boot_launcher.txt read failed (" + $_ + ") — falling back to start_iris.bat")
    }
    if (Test-Path $batPath) {
        # Snapshot existing claude/node PIDs so we can detect "no new PID
        # spawned" — the signature of an inside-the-window bat crash.
        $baseline = Get-ClaudeProcessSet
        Write-WatchLog ("baseline claude/node PIDs before spawn: " + $baseline.Count)

        # ---- Tier 1: Windows Terminal (visible, real console) ----
        # W3: use `-w 0 nt` form so wt parses subcommand grammar explicitly;
        # quote paths defensively. wt.exe doesn't surface launch errors via
        # $? — verify by checking WindowsTerminal process presence and the
        # 30s claude-pid wait. The wait is still the load-bearing detector.
        $wt = Get-Command wt.exe -ErrorAction SilentlyContinue
        if ($wt) {
            try {
                $wtBefore = @(Get-Process WindowsTerminal -ErrorAction SilentlyContinue).Count
                # /c (not /k): iris_cold_wake.py blocks until CC exits, so the window
                # stays visible for the whole session and closes CLEANLY when CC ends.
                # /k used to keep the dead shell open after exit — an orphan "Iris…"
                # window per restart that looked like a second half-attached session
                # (fixed 2026-06-26).
                $wtArgs = @("-w", "0", "nt", "-d", "`"$ROOT`"", "cmd.exe", "/c", "`"$batPath`"")
                Start-Process -FilePath "wt.exe" -ArgumentList $wtArgs
                Start-Sleep -Milliseconds 500
                $wtAfter = @(Get-Process WindowsTerminal -ErrorAction SilentlyContinue).Count
                Write-WatchLog ("tier 1 spawned: wt.exe + start_iris.bat (WindowsTerminal procs " + $wtBefore + "->" + $wtAfter + ") — waiting up to 120s for claude PID")
                if (Wait-ForNewClaudeProcess -Baseline $baseline -TimeoutS 120) {
                    Write-WatchLog ("tier 1 confirmed: new claude/node PID detected")
                    return $true
                }
                Write-WatchLog ("tier 1 spawn returned but no claude PID appeared within 120s — bat likely crashed or wt args malformed; trying tier 2")
            } catch {
                Write-WatchLog ("tier 1 spawn threw: " + $_)
            }
        }

        # ---- Tier 2: cmd /c (visible cmd window, closes cleanly at session end) ----
        # Refresh baseline so any side-effect of tier 1 (in case a PID
        # appeared juuust after the timeout) doesn't mask a tier 2 crash.
        # /c (not /k): see the tier-1 note — the window lives for the whole session
        # (cmd → start_iris.bat → iris_cold_wake.py → claude, all blocking) and closes
        # when CC exits, instead of orphaning a dead "Iris…" window per restart.
        $baseline = Get-ClaudeProcessSet
        try {
            Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$batPath`"" -WindowStyle Normal
            Write-WatchLog ("tier 2 spawned: cmd /c + start_iris.bat — waiting up to 120s for claude PID")
            if (Wait-ForNewClaudeProcess -Baseline $baseline -TimeoutS 120) {
                Write-WatchLog ("tier 2 confirmed: new claude/node PID detected")
                return $true
            }
            Write-WatchLog ("tier 2 spawn returned but no claude PID appeared within 120s — bat likely crashed; falling to tier 3 (bare claude)")
        } catch {
            Write-WatchLog ("tier 2 spawn threw: " + $_)
        }
    } else {
        Write-WatchLog ("WARN: start_iris.bat not found at $batPath, jumping to tier 3 (bare claude)")
    }

    # ---- Tier 3: bare claude (loses channel flags + env var) ----
    return Spawn-BareClaude
}

# ---- Letter-arrival watch (added 2026-05-19) ----
# Watches state/iris_sibling/inbox/ for new letters (mtime > startup mark).
# When a new letter arrives AND CC is not running, spawn CC. This is the
# cold-wake path for ritual_scheduler letters and Wren-during-NC letters.
# Same debounce machinery as the trigger-file path.
$INBOX_DIR = Join-Path $ROOT "state\iris_sibling\inbox"
$lastInboxLatestMtime = 0.0
if (Test-Path $INBOX_DIR) {
    try {
        $files = Get-ChildItem -Path $INBOX_DIR -File -ErrorAction SilentlyContinue
        if ($files) {
            $lastInboxLatestMtime = ($files | ForEach-Object { $_.LastWriteTime.Ticks } | Measure-Object -Maximum).Maximum
            if ($null -eq $lastInboxLatestMtime) { $lastInboxLatestMtime = 0 }
        }
    } catch { }
}

function Check-InboxForNewLetter {
    # Returns the latest mtime in inbox (as ticks), or 0 if no files. Only
    # detects NEW letters (latest mtime > $lastInboxLatestMtime). Caller
    # updates $script:lastInboxLatestMtime when it acts on the result.
    if (-not (Test-Path $INBOX_DIR)) { return 0 }
    try {
        $files = Get-ChildItem -Path $INBOX_DIR -File -ErrorAction SilentlyContinue
        if (-not $files) { return 0 }
        $latest = ($files | ForEach-Object { $_.LastWriteTime.Ticks } | Measure-Object -Maximum).Maximum
        if ($null -eq $latest) { return 0 }
        return $latest
    } catch {
        return 0
    }
}

function Is-CCRunning {
    # Returns $true if any claude process is alive. Best-effort.
    try {
        $procs = Get-Process -Name "claude" -ErrorAction SilentlyContinue
        return ($null -ne $procs -and $procs.Count -gt 0)
    } catch {
        return $false
    }
}

# ---- Main loop ----
Write-WatchLog "watchdog starting (trigger=$TRIGGER_FILE inbox=$INBOX_DIR poll=${POLL_INTERVAL_S}s debounce=${DEBOUNCE_S}s)"

$tmpDir = Split-Path $TRIGGER_FILE -Parent
if (-not (Test-Path $tmpDir)) {
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
}

$lastRespawn = [DateTime]::MinValue

# Heartbeat: write a fresh timestamp every poll iteration so external
# health checks can verify the watchdog is actually polling (not zombie).
# Written BEFORE the try/catch so even if the loop body throws, the heartbeat
# still updates -- proving the while($true) is alive. If THIS stops updating,
# the watchdog process is truly dead.
#
# Failure tracking: if heartbeat write fails N consecutive times (disk full,
# AV lock, .tmp deleted, etc.) the loop keeps polling but keepalive will
# see a stale heartbeat and respawn this watchdog, only for the new one to
# hit the same write failure -> respawn loop. After N consecutive failures
# we log a CRITICAL line to the watchdog log (best-effort) AND Write-Warning
# to stderr (separate channel, may survive disk-side failures).
$script:HeartbeatFailCount = 0
$HEARTBEAT_FAIL_WARN_AT = 3
function Write-Heartbeat {
    try {
        $now = [DateTimeOffset]::Now.ToUnixTimeSeconds()
        Set-Content -Path $WATCHDOG_HEARTBEAT -Value "$now" -NoNewline -ErrorAction Stop
        $script:HeartbeatFailCount = 0
    } catch {
        $script:HeartbeatFailCount++
        if ($script:HeartbeatFailCount -eq $HEARTBEAT_FAIL_WARN_AT) {
            $msg = "[CRITICAL] heartbeat write failed $script:HeartbeatFailCount consecutive times. Cause: $_"
            try { Write-WatchLog $msg } catch { }
            Write-Warning $msg
        }
    }
}

while ($true) {
    Write-Heartbeat
    try {
        # Check for sibling-letter arrival (ritual_scheduler + Wren letters).
        # Only spawn if CC is NOT already running — if CC is open, the Stop
        # hook + sibling_inbox_list will pick up the letter naturally.
        $currentLatestMtime = Check-InboxForNewLetter
        if ($currentLatestMtime -gt $lastInboxLatestMtime) {
            $sinceLast = (Get-Date) - $lastRespawn
            if ($sinceLast.TotalSeconds -lt $DEBOUNCE_S) {
                Write-WatchLog ("[INBOX DEBOUNCED] new letter within debounce window (" + [int]$sinceLast.TotalSeconds + "s of " + $DEBOUNCE_S + "s); ignoring")
                $lastInboxLatestMtime = $currentLatestMtime
            } elseif (Is-CCRunning) {
                Write-WatchLog ("new letter detected but CC already running -- Stop hook will handle")
                $lastInboxLatestMtime = $currentLatestMtime
            } else {
                Write-WatchLog "new letter detected AND CC not running -- spawning"
                $lastInboxLatestMtime = $currentLatestMtime
                $ok = Spawn-NewCC
                if ($ok) {
                    $lastRespawn = Get-Date
                    Write-WatchLog "spawn-on-letter complete"
                } else {
                    Write-WatchLog "spawn-on-letter failed -- letter sits until next attempt"
                }
            }
        }

        if (Test-Path $TRIGGER_FILE) {
            $sinceLast = (Get-Date) - $lastRespawn
            if ($sinceLast.TotalSeconds -lt $DEBOUNCE_S) {
                # W5: capture reason before deleting so post-mortem is possible.
                $dbReason = ""
                try {
                    $dbReason = (Get-Content $TRIGGER_FILE -Raw -ErrorAction SilentlyContinue).Trim()
                } catch {
                    $dbReason = "(could not read reason)"
                }
                Write-WatchLog ("[DEBOUNCED] trigger swallowed within debounce window (" + [int]$sinceLast.TotalSeconds + "s of " + $DEBOUNCE_S + "s); reason=" + $dbReason)
                Remove-Item -Path $TRIGGER_FILE -Force -ErrorAction SilentlyContinue
            } else {
                $reason = ""
                try {
                    $reason = (Get-Content $TRIGGER_FILE -Raw -ErrorAction SilentlyContinue).Trim()
                } catch {
                    $reason = "(could not read reason)"
                }
                # 2026-08-05 stand-down guard: a very young claude process
                # means another restart authority just brought the stack up
                # (or is mid-restart). Killing it and spawning here is how the
                # 08-04 twin was born. Swallow the flag instead.
                $youngest = Get-YoungestCCAgeSeconds
                if ($null -ne $youngest -and $youngest -lt $FRESH_STACK_S) {
                    Write-WatchLog ("[STAND-DOWN] trigger ignored: claude process only " + [int]$youngest + "s old (< " + $FRESH_STACK_S + "s) — another authority just restarted the stack. reason=" + $reason)
                    Remove-Item -Path $TRIGGER_FILE -Force -ErrorAction SilentlyContinue
                    continue
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