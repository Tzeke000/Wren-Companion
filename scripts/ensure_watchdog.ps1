# ensure_watchdog.ps1 - keep Iris's two watchdogs alive across reboots.
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
# 2026-08-19 - SECOND WATCHDOG ADDED, and it is the same bug as 2026-07-25:
#   Today the runtime wedged (event loop silent 181s) and sat there until Zeke noticed
#   over Moonlight, because scripts\iris_runtime_watchdog.py - the Python wedge
#   detector that owns stack-restart - WAS NOT RUNNING. It had died silently
#   (no exit line in its log, no Windows fault event) sometime after 15:40.
#   Nothing revived it, because THIS script only ever guarded iris_watchdog.ps1.
#   The 2026-07-25 fix was applied to the PowerShell watchdog; the newer Python one
#   inherited the exact defect the header above describes - "survived only because
#   someone started it once", started solely by start_iris_v2.bat.
#   So it is guarded here now. Note the scope change: reviving it restores a process
#   that HAS stack-restart authority. That is intended state, not new authority -
#   it is supposed to be running continuously, and it only acts after a 181s wedge
#   plus a 120s grace window with a holdoff flag. The alternative is what happened
#   today: a wedge with no safety net until a human happens to look.
#
# Idempotent and safe to run every few minutes.

$ErrorActionPreference = 'Stop'
$ROOT = 'D:\Wren-Companion'
$WATCHDOG = Join-Path $ROOT 'scripts\iris_watchdog.ps1'
$RUNTIME_WATCHDOG = Join-Path $ROOT 'scripts\iris_runtime_watchdog.py'
$VENV_PY = Join-Path $ROOT '.venv\Scripts\python.exe'
$LOG = Join-Path $ROOT 'state\ensure_watchdog.log'

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try { Add-Content -Path $LOG -Value $line -Encoding utf8 } catch { }
}

# ---------------------------------------------------------------------------
# 1. iris_watchdog.ps1 - polls for the restart_cc flag. No restart authority.
# ---------------------------------------------------------------------------
if (-not (Test-Path $WATCHDOG)) {
    Write-Log "FATAL: watchdog script missing at $WATCHDOG"
} else {
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
    $probeOk = $true
    try {
        $running = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -OperationTimeoutSec 15 |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine -like '*iris_watchdog.ps1*' -and
                $_.CommandLine -like '*-File*' -and
                $_.CommandLine -notlike '*-Command*'
            })
    } catch {
        Write-Log "WMI probe failed (ps watchdog): $_"
        $probeOk = $false   # fail open - never let this script itself become the problem
    }

    if ($probeOk -and $running.Count -eq 0) {
        Write-Log "watchdog NOT running - starting it"
        try {
            Start-Process -FilePath 'powershell.exe' `
                -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $WATCHDOG `
                -WindowStyle Hidden
            Write-Log "spawned watchdog OK"
        } catch {
            Write-Log "FAILED to spawn watchdog: $_"
        }
    }
    # healthy and quiet: no log spam on the happy path
}

# ---------------------------------------------------------------------------
# 2. iris_runtime_watchdog.py - detects a wedged runtime event loop and
#    restarts the stack. Added 2026-08-19 after it died unnoticed (see header).
# ---------------------------------------------------------------------------
if (-not (Test-Path $RUNTIME_WATCHDOG)) {
    Write-Log "FATAL: runtime watchdog script missing at $RUNTIME_WATCHDOG"
    exit 1
}
if (-not (Test-Path $VENV_PY)) {
    Write-Log "FATAL: venv python missing at $VENV_PY - cannot start runtime watchdog"
    exit 1
}

# Probe note: this one is simpler than the .ps1 case because we filter on
# Name='python.exe' - a PowerShell diagnostic one-liner that merely mentions the
# script name is powershell.exe and cannot match here. A normal healthy state is
# TWO matching processes (venv python parent + system python worker); that is the
# documented PARENT+WORKER shape, not a double-spawn. Count > 0 is the test.
# The watchdog also holds a named mutex ("IrisRuntimeWatchdog"), so even if this
# script races and spawns a second one, the loser logs and exits on its own.
$rtRunning = @()
try {
    $rtRunning = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -OperationTimeoutSec 15 |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*iris_runtime_watchdog.py*' })
} catch {
    Write-Log "WMI probe failed (runtime watchdog): $_"
    exit 0   # fail open
}

if ($rtRunning.Count -eq 0) {
    Write-Log "runtime watchdog NOT running - starting it"
    try {
        Start-Process -FilePath $VENV_PY -ArgumentList $RUNTIME_WATCHDOG -WindowStyle Hidden
        Write-Log "spawned runtime watchdog OK"
    } catch {
        Write-Log "FAILED to spawn runtime watchdog: $_"
    }
}

# ---------------------------------------------------------------------------
# 3. sibling_postoffice.py - FastAPI on :5877, Wren's lifeline. Added
#    2026-08-21 after it crashed and sat dead until manually restarted
#    (postoffice_monitor.py only ALARMS on outage - it never restarts).
#
#    Policy (deliberately conservative - this is a SHARED system):
#      - /health answers 200        -> quiet, do nothing.
#      - process exists, health bad -> LOG ONLY. A wedged-but-present server
#        might be mid-work for Wren; killing it is a human/Iris decision,
#        never this script's. (CORE: "don't bounce unilaterally.")
#      - NO process at all          -> start the SERVER ALONE (never the bat:
#        start_postoffice_stack.bat also spawns postoffice_monitor.py, which
#        is not idempotent - relaunching the stack doubles the monitor).
#    Two-instance hazard (2026-06-28 scar): only ever spawn when the process
#    count is ZERO, and always on the .venv interpreter.
# ---------------------------------------------------------------------------
$POSTOFFICE = Join-Path $ROOT 'scripts\sibling_postoffice.py'
if (Test-Path $POSTOFFICE) {
    $healthy = $false
    try {
        $resp = & curl.exe -s -o NUL -w '%{http_code}' -m 5 'http://127.0.0.1:5877/health'
        $healthy = ($resp -eq '200')
    } catch { $healthy = $false }

    if (-not $healthy) {
        $poRunning = @()
        $poProbeOk = $true
        try {
            $poRunning = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -OperationTimeoutSec 15 |
                Where-Object { $_.CommandLine -and $_.CommandLine -like '*sibling_postoffice.py*' })
        } catch {
            Write-Log "WMI probe failed (postoffice): $_"
            $poProbeOk = $false   # fail open
        }
        if ($poProbeOk) {
            if ($poRunning.Count -gt 0) {
                Write-Log ("postoffice UNHEALTHY but process present (pid " +
                    (($poRunning | ForEach-Object { $_.ProcessId }) -join ',') +
                    ") - NOT killing a shared system; needs a human/Iris decision")
            } else {
                Write-Log "postoffice DOWN (no process, health=$resp) - starting server alone"
                try {
                    Start-Process -FilePath $VENV_PY -ArgumentList $POSTOFFICE -WindowStyle Hidden `
                        -RedirectStandardOutput (Join-Path $ROOT '.tmp\postoffice.out.log') `
                        -RedirectStandardError (Join-Path $ROOT '.tmp\postoffice.err.log')
                    Write-Log "spawned postoffice server OK"
                } catch {
                    Write-Log "FAILED to spawn postoffice: $_"
                }
            }
        }
    }
} else {
    Write-Log "postoffice script missing at $POSTOFFICE - skipping guard"
}
