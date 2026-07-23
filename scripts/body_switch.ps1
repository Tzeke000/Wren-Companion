# body_switch.ps1 - THE ON SWITCH for Iris's digital body (Zeke directive 2026-07-22 night).
# One command that makes the body BE ON. Idempotent - checks each part, only starts what's down.
#
#   powershell -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\body_switch.ps1          # = on
#   powershell -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\body_switch.ps1 status   # report only
#   powershell -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\body_switch.ps1 off      # close orb app only
#
# What "the digital body" is on this machine (NOT avaagent - that's Ava's daemon):
#   iris_body_host.py  -> the cognition session (Claude). NOT touched by this script.
#   iris_runtime.py    -> camera/perception/tools MCP child, serves orb backend :5876 via brain/orb_http.py
#   iris-control.exe   -> the Tauri orb app (the visible digital body)
#   sibling postoffice -> :5877, vector_brain_server -> :8772
#
# ZOMBIE RULE (root cause note: tool_registry_llamacpp_wedge_root_cause_2026-07-22.md):
#   if iris_runtime procs exist but the loop heartbeat is stale >120s, the runtime is a corpse
#   (os._exit deadlock). Kill it - the body host's attach guard respawns it on the next tool call.

param([string]$Mode = "on")

$ROOT = "D:\Wren-Companion"
$LOG  = "$ROOT\state\body_switch.log"
function Log($m) { $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m; Add-Content -Path $LOG -Value $line -Encoding utf8; Write-Output $line }

function PortUp($p) { return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) }
function ProcUp($namePattern, $cmdPattern) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='$namePattern'" -ErrorAction SilentlyContinue
    return @($procs | Where-Object { $_.CommandLine -match $cmdPattern })
}

Log "=== body_switch $Mode ==="

if ($Mode -eq "off") {
    Get-Process iris-control -ErrorAction SilentlyContinue | Stop-Process -Force
    Log "orb app closed. (Backend + cognition left running - 'off' only hides the body.)"
    exit 0
}

$report = @()

# 1) RUNTIME + ORB BACKEND :5876 (zombie-aware)
$hb = Get-Item "$ROOT\state\runtime_loop_heartbeat.json" -ErrorAction SilentlyContinue
$hbAge = if ($hb) { ((Get-Date) - $hb.LastWriteTime).TotalSeconds } else { 99999 }
$rtProcs = ProcUp 'python.exe' 'iris_runtime\.py'
if ($rtProcs.Count -gt 0 -and $hbAge -gt 120) {
    if ($Mode -eq "status") { $report += "runtime: ZOMBIE (procs alive, heartbeat ${hbAge}s stale)" }
    else {
        Log "runtime ZOMBIE detected (heartbeat ${hbAge}s stale) - killing $($rtProcs.ProcessId -join ',')"
        $rtProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Log "zombie cleared. Body host respawns it on Iris's next tool call (or watchdog restarts the stack)."
        $report += "runtime: was ZOMBIE -> killed (respawn on next Iris tool call)"
    }
} elseif ($rtProcs.Count -eq 0) {
    $report += "runtime: NOT RUNNING (body host down too? that needs start_iris_v2.bat - the one thing this switch won't do, it would kill a live Iris)"
} else {
    $report += "runtime: OK (heartbeat $([Math]::Round($hbAge))s)"
}
$report += if (PortUp 5876) { "orb backend :5876 : OK" } else { "orb backend :5876 : DOWN (comes up with runtime bootstrap ~15-60s; if runtime OK but 5876 down >2min, iris_tool_call restart_orb_http)" }

# 2) ORB APP (the visible body)
$orb = Get-Process iris-control -ErrorAction SilentlyContinue
if (-not $orb -and $Mode -ne "status") {
    Start-Process -FilePath "$ROOT\apps\ava-control\src-tauri\target\release\iris-control.exe"
    Log "orb app launched"
    $report += "orb app: was DOWN -> launched"
} else {
    $report += if ($orb) { "orb app: OK (PID $($orb.Id -join ','))" } else { "orb app: DOWN" }
}

# 3) POST OFFICE :5877
if (-not (PortUp 5877) -and $Mode -ne "status") {
    Start-Process -FilePath "$ROOT\start_postoffice_stack.bat" -WorkingDirectory $ROOT -WindowStyle Hidden
    Log "postoffice launched"
    $report += "postoffice :5877 : was DOWN -> launched"
} else {
    $report += if (PortUp 5877) { "postoffice :5877 : OK" } else { "postoffice :5877 : DOWN" }
}

# 4) VECTOR BRAIN :8772 (little brain server; parent+worker = 2 procs = normal)
if (-not (PortUp 8772) -and $Mode -ne "status") {
    Start-Process -FilePath "$ROOT\.venv\Scripts\python.exe" -ArgumentList "$ROOT\scripts\vector_brain_server.py" -WorkingDirectory $ROOT -WindowStyle Hidden
    Log "vector_brain_server launched"
    $report += "vector brain :8772 : was DOWN -> launched"
} else {
    $report += if (PortUp 8772) { "vector brain :8772 : OK" } else { "vector brain :8772 : DOWN" }
}

# 5) RUNTIME WATCHDOG
$wd = ProcUp 'python.exe' 'iris_runtime_watchdog\.py'
if ($wd.Count -eq 0 -and $Mode -ne "status") {
    Start-Process -FilePath "$ROOT\.venv\Scripts\python.exe" -ArgumentList '-u', "$ROOT\scripts\iris_runtime_watchdog.py" -WorkingDirectory $ROOT -WindowStyle Hidden
    Log "runtime watchdog launched"
    $report += "runtime watchdog: was DOWN -> launched"
} else {
    $report += if ($wd.Count -gt 0) { "runtime watchdog: OK" } else { "runtime watchdog: DOWN" }
}

# 6) INHABIT DAEMON (Vector possession - body safety)
$inhabit = ProcUp 'python.exe' 'vector_inhabit_daemon\.py'
$report += if ($inhabit.Count -gt 0) { "inhabit daemon: OK" } else { "inhabit daemon: DOWN (body possession! start: .venv python -u scripts\vector_inhabit_daemon.py)" }

Log ("REPORT: " + ($report -join " | "))
$report | ForEach-Object { Write-Output "  $_" }
Log "=== done ==="
