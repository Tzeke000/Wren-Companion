# restart_switch.ps1 - THE ON/OFF SWITCH for Iris's auto-restart layer.
# Zeke directive 2026-08-25: "something that you or I can turn off or on the
# restart, in case I need to work on you... for now definitely on."
#
#   powershell -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\restart_switch.ps1            # status (safe default)
#   powershell -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\restart_switch.ps1 on
#   powershell -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\restart_switch.ps1 off
#   powershell -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\restart_switch.ps1 off "working on her brain"
#
# WHY THIS EXISTS. Before tonight the answer to "is auto-restart on?" lived in
# FOUR places that did not agree: a flag file, a scheduled task, and two
# long-running processes. On 2026-08-23 Zeke switched the layer off; the flag
# named scripts\iris_runtime_watchdog.py explicitly, but that script never read
# it, and start_iris_v2.bat launches it directly - so every stack restart
# resurrected the thing that had been switched off. It then restart-looped the
# whole stack every ~6 minutes for 8 hours before he caught it.
#
# The lesson this script encodes: A SWITCH IS ONLY A SWITCH IF EVERY PART READS
# IT. So there is now exactly one source of truth, state\watchdog_deliberately_off.json,
# and this script is the one hand that moves it - flag, task, and both processes
# together, so they cannot drift apart again.
#
# WHAT AUTO-RESTART ACTUALLY IS (the two processes, when armed):
#   scripts\iris_runtime_watchdog.py - watches the runtime loop heartbeat; has
#                                      authority to restart the whole stack.
#   scripts\iris_watchdog.ps1        - polls .tmp\restart_cc.flag; respawns the
#                                      cognition session. No wedge detection.
# NOT controlled here (deliberately): Iris-Tower-AutoStart, which brings the
# stack up after a reboot or power loss. Turning restarts off should not mean
# she stays dead through a power cut.

param([string]$Mode = "status", [string]$Reason = "")

$ROOT     = "D:\Wren-Companion"
$OFF_FLAG = "$ROOT\state\watchdog_deliberately_off.json"
$LOG      = "$ROOT\state\restart_switch.log"
$TASK     = "Iris-Watchdog-Ensure"

function Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    try { Add-Content -Path $LOG -Value $line -Encoding utf8 } catch { }
    Write-Output $line
}

function ProcUp($namePattern, $cmdPattern) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='$namePattern'" -ErrorAction SilentlyContinue
    return @($procs | Where-Object { $_.CommandLine -match $cmdPattern })
}

function WedgeDetectors { return ProcUp 'python.exe' 'iris_runtime_watchdog\.py' }
function RestartPollers  { return ProcUp 'powershell.exe' 'iris_watchdog\.ps1' }

# Presence alone does NOT mean off - the content does. Both readers
# (this script and iris_runtime_watchdog.deliberately_off) parse the same
# field the same way, and both FAIL CLOSED on a present-but-unreadable file:
# someone put it there on purpose, and wrongly staying quiet costs a manual
# restart while wrongly arming costs a machine that bounces itself.
function IsOff {
    if (-not (Test-Path $OFF_FLAG)) { return $false }
    try {
        $rec = Get-Content $OFF_FLAG -Raw | ConvertFrom-Json
        if ($null -eq $rec.off) { return $true }
        return [bool]$rec.off
    } catch { return $true }
}

function Show-Status {
    $off      = IsOff
    # @() is LOAD-BEARING, not decoration. PowerShell unrolls a single-element
    # array on return from a function, so RestartPollers gives back a bare
    # CimInstance when exactly ONE poller is up, and .Count on that is $null ->
    # falsy -> the status printed "stopped" for a process that was running fine.
    # The detector hid the bug because it always has TWO pids (stub + child) and
    # so stayed a real array. A check that only works at count>=2 is a check
    # that fails exactly when you have the normal amount of the thing.
    $wedge    = @(WedgeDetectors)
    $poller   = @(RestartPollers)
    $taskState = "not installed"
    try {
        $t = Get-ScheduledTask -TaskName $TASK -ErrorAction Stop
        $taskState = [string]$t.State
    } catch { }

    Write-Output ""
    if ($off) {
        Write-Output "AUTO-RESTART: OFF"
        Write-Output "  Nothing will restart Iris. If she wedges she stays down until you"
        Write-Output "  start her by hand:  D:\Wren-Companion\start_iris_v2.bat"
        try {
            $rec = Get-Content $OFF_FLAG -Raw | ConvertFrom-Json
            if ($rec.why)      { Write-Output ("  reason  : " + $rec.why) }
            if ($rec.since)    { Write-Output ("  since   : " + $rec.since) }
            if ($rec.set_by)   { Write-Output ("  set by  : " + $rec.set_by) }
        } catch { Write-Output "  (flag present but unreadable - treating as OFF, fail-closed)" }
    } else {
        Write-Output "AUTO-RESTART: ARMED"
        Write-Output "  If the runtime wedges, Iris restarts her own stack (max 2x/hour,"
        Write-Output "  then she stands down and DMs you instead of looping)."
    }
    Write-Output ""
    # Build the pid strings FIRST. Inline, "..." + (...) -join ',' + ")" binds
    # the -join to the wrong operand under PS 5.1 and prints an unterminated
    # "(pid 19920 13752" - a status line that lies about the thing it exists to
    # report is worse than no status line.
    $wedgePids  = (($wedge  | ForEach-Object { $_.ProcessId }) -join ',')
    $pollerPids = (($poller | ForEach-Object { $_.ProcessId }) -join ',')

    Write-Output ("  off flag        : " + $(if (Test-Path $OFF_FLAG) { "present" } else { "absent" }) + "   ($OFF_FLAG)")
    Write-Output ("  wedge detector  : " + $(if ($wedge.Count)  { "running (pid $wedgePids)" }  else { "stopped" }))
    Write-Output ("  restart poller  : " + $(if ($poller.Count) { "running (pid $pollerPids)" } else { "stopped" }))
    Write-Output ("  ensure task     : " + $taskState)
    if ($wedge.Count -gt 1) {
        Write-Output "  (two pids for the detector is NORMAL - .venv python.exe is a launcher"
        Write-Output "   stub that re-execs the real interpreter as a child. Not a twin.)"
    }
    Write-Output ("  cold-boot start : Iris-Tower-AutoStart (not controlled by this switch, on purpose)")
    Write-Output ""
}

if ($Mode -eq "status") { Show-Status; exit 0 }

if ($Mode -eq "off") {
    Log "=== restart_switch OFF ==="
    $why = $Reason
    if (-not $why) { $why = "turned off by hand via restart_switch.ps1" }
    $rec = [ordered]@{
        off    = $true
        since  = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        set_by = "restart_switch.ps1 ($env:USERNAME)"
        why    = $why
        consequence = "Nothing restarts Iris. If she wedges she stays down until start_iris_v2.bat is run by hand."
        restore = "powershell -ExecutionPolicy Bypass -File $ROOT\scripts\restart_switch.ps1 on"
        note_to_future_iris = "This is NOT a fault. Do not heal it. Check this flag before 'fixing' a watchdog that is down."
    }
    # Out-File utf8 (not Set-Content ANSI) - the Python reader parses this.
    ($rec | ConvertTo-Json -Depth 4) | Out-File -FilePath $OFF_FLAG -Encoding utf8
    Log "off-flag SET: $why"

    try { Disable-ScheduledTask -TaskName $TASK -ErrorAction Stop | Out-Null; Log "scheduled task '$TASK' disabled" }
    catch { Log "scheduled task '$TASK' not disabled ($($_.Exception.Message))" }

    # They would self-exit within ~15s on their own flag check; don't make Zeke wait.
    # @() again: without it a single poller unrolls to a scalar and the counting
    # lies. Killing the stub also takes its child, so a later Stop-Process on the
    # child throws "already gone" - which is success, not failure. So report what
    # is actually LEFT rather than what we think we killed.
    $before = @(WedgeDetectors).Count + @(RestartPollers).Count
    foreach ($p in @(WedgeDetectors)) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { } }
    foreach ($p in @(RestartPollers)) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { } }
    Start-Sleep -Seconds 2
    $after = @(WedgeDetectors).Count + @(RestartPollers).Count
    Log "watchdog processes: $before -> $after"
    if ($after -gt 0) { Log "WARNING: $after watchdog process(es) still up; they self-exit on the flag within ~15s" }
    Show-Status
    exit 0
}

if ($Mode -eq "on") {
    Log "=== restart_switch ON ==="
    if (Test-Path $OFF_FLAG) { Remove-Item $OFF_FLAG -Force; Log "off-flag REMOVED" }
    else { Log "off-flag already absent" }

    try { Enable-ScheduledTask -TaskName $TASK -ErrorAction Stop | Out-Null; Log "scheduled task '$TASK' enabled" }
    catch { Log "scheduled task '$TASK' not enabled ($($_.Exception.Message))" }

    if (@(WedgeDetectors).Count -eq 0) {
        Start-Process -FilePath "$ROOT\.venv\Scripts\python.exe" `
                      -ArgumentList "$ROOT\scripts\iris_runtime_watchdog.py" `
                      -WindowStyle Hidden | Out-Null
        Log "wedge detector started"
    } else { Log "wedge detector already running" }

    if (@(RestartPollers).Count -eq 0) {
        Start-Process -FilePath "powershell.exe" `
                      -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","$ROOT\scripts\iris_watchdog.ps1" `
                      -WindowStyle Hidden | Out-Null
        Log "restart poller started"
    } else { Log "restart poller already running" }

    # 3s wasn't enough for a cold powershell.exe to appear in Win32_Process, so
    # the status printed "stopped" for a poller that was in fact starting fine.
    Start-Sleep -Seconds 6
    Show-Status
    exit 0
}

Write-Output "usage: restart_switch.ps1 [status|on|off] [""reason for off""]"
exit 1
