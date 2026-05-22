# elevate_keepalive.ps1 - v3 audited (round 1 + round 2 fan-out)
# 2026-05-21 Iris pre-deployment admin elevation
# Run from an ELEVATED PowerShell window: & "D:\Wren-Companion\scripts\elevate_keepalive.ps1"

# 0. Backup current task definition as rollback artifact (ASCII = no BOM)
$backupPath = "D:\Wren-Companion\.tmp\keepalive_backup_$(Get-Date -f yyyyMMdd_HHmmss).xml"
$backupDir = Split-Path $backupPath -Parent
if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir -Force | Out-Null }
[System.IO.File]::WriteAllText($backupPath, (Export-ScheduledTask -TaskName 'Iris-Keepalive'), [System.Text.UTF8Encoding]::new($false))
if (-not (Test-Path $backupPath) -or (Get-Item $backupPath).Length -eq 0) {
  Write-Error "Backup write failed - refusing to proceed without rollback artifact"; return
}
$backupSize = (Get-Item $backupPath).Length
Write-Output "[0/9] backup saved to $backupPath ($backupSize bytes)"

# 1. Sanity check elevated
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]'Administrator')) {
  Write-Error "NOT ELEVATED - close and reopen PowerShell as Administrator"; return
}
Write-Output "[1/9] elevated OK"

# 2. Capture existing fields as PRIMITIVES (not CIM refs) before Unregister
$existing = Get-ScheduledTask -TaskName 'Iris-Keepalive' -ErrorAction Stop
$execute        = $existing.Actions[0].Execute
$arguments      = $existing.Actions[0].Arguments
$existingUserId = $existing.Principal.UserId
$startBoundary  = $existing.Triggers[0].StartBoundary
Write-Output "[2/9] current RunLevel=$($existing.Principal.RunLevel) UserId=$existingUserId Repetition=$($existing.Triggers[0].Repetition.Interval)"

# 3. Re-register with Highest, rebuilding trigger from primitives (don't reuse CIM ref)
Unregister-ScheduledTask -TaskName 'Iris-Keepalive' -Confirm:$false
$action = New-ScheduledTaskAction -Execute $execute -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once -At $startBoundary -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration ([TimeSpan]::FromDays(3650))
$principal = New-ScheduledTaskPrincipal -UserId $existingUserId -RunLevel Highest -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew -Compatibility Win7
Register-ScheduledTask -TaskName 'Iris-Keepalive' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -ErrorAction Stop | Out-Null
$verify = Get-ScheduledTask -TaskName 'Iris-Keepalive'
if ($verify.Principal.RunLevel -ne 'Highest')           { Write-Error "RunLevel did not stick - restore from $backupPath"; return }
if ($verify.State -ne 'Ready')                          { Write-Error "Task state=$($verify.State) not Ready - restore from $backupPath"; return }
if ($verify.Triggers[0].Repetition.Interval -ne 'PT1M') { Write-Error "Repetition pattern lost - restore from $backupPath"; return }
Write-Output "[3/9] re-registered RunLevel=Highest State=Ready Repetition=PT1M UserId=$($verify.Principal.UserId)"

# 4. Pre-fire guard: refresh watchdog.log mtime + heartbeat so keepalive sees in-flight + watchdog healthy
$now = [DateTimeOffset]::Now.ToUnixTimeSeconds()
$watchdogLog = "D:\Wren-Companion\.tmp\watchdog.log"
if (Test-Path $watchdogLog) { (Get-Item $watchdogLog).LastWriteTime = Get-Date }
Set-Content -Path "D:\Wren-Companion\.tmp\watchdog_heartbeat.txt" -Value "$now" -NoNewline
Start-Sleep -Milliseconds 300
Write-Output "[4/9] pre-fire guards in place (watchdog.log mtime fresh, heartbeat $now)"

# 5. SANITY FIRE - verify task elevates without spawning duplicates
$preFireRun = (Get-ScheduledTaskInfo -TaskName 'Iris-Keepalive').LastRunTime
Start-ScheduledTask -TaskName 'Iris-Keepalive'
$deadline = (Get-Date).AddSeconds(30)
do {
  Start-Sleep -Seconds 1
  $info = Get-ScheduledTaskInfo -TaskName 'Iris-Keepalive'
} while ($info.LastRunTime -le $preFireRun -and (Get-Date) -lt $deadline)
if ($info.LastRunTime -le $preFireRun) {
  Write-Error "Sanity fire never ran (LastRunTime did not advance from $preFireRun within 30s)"; return
}
# Allow result codes: 0 (success), 267009 = SCHED_S_TASK_HAS_NOT_RUN, 267011 = SCHED_S_TASK_RUNNING
if ($info.LastTaskResult -ne 0 -and $info.LastTaskResult -ne 267009 -and $info.LastTaskResult -ne 267011) {
  Write-Error "Sanity fire failed (code $($info.LastTaskResult)) - task not elevating. Restore: Unregister-ScheduledTask -TaskName 'Iris-Keepalive' -Confirm:`$false; Register-ScheduledTask -Xml (Get-Content '$backupPath' -Raw) -TaskName 'Iris-Keepalive'"
  return
}
Write-Output "[5/9] sanity fire passed: LastTaskResult=$($info.LastTaskResult) LastRunTime=$($info.LastRunTime) - proceeding to kill chain"

# 6. Kill chain by name+cmdline filter (admin sees all cmdlines)
$myPid = $PID
$killed = 0
$targets = @(
  @{ Name='claude.exe';     Filter={ (-not $_.CommandLine) -or ($_.CommandLine -like '*Wren-Companion*') -or ($_.CommandLine -like '*server:iris*') } },
  @{ Name='node.exe';       Filter={ $_.CommandLine -and ($_.CommandLine -like '*Wren-Companion*' -or $_.CommandLine -like '*claude-code*' -or $_.CommandLine -like '*\claude\node_modules*' -or $_.CommandLine -like '*\claude\*') } },
  @{ Name='python.exe';     Filter={ $_.CommandLine -like '*iris_runtime*' -or $_.CommandLine -like '*iris_cold_wake*' -or $_.CommandLine -like '*xtts_server*' } },
  @{ Name='bun.exe';        Filter={ $_.CommandLine -like '*claude-plugins-official*' -or $_.CommandLine -like '*CLAUDE_PLUGIN_ROOT*' -or $_.CommandLine -like '*discord*' } },
  @{ Name='powershell.exe'; Filter={ ($_.CommandLine -like '*iris_watchdog*' -or $_.CommandLine -like '*iris_keepalive*') -and $_.ProcessId -ne $myPid } },
  @{ Name='cmd.exe';        Filter={ $_.CommandLine -like '*start_iris.bat*' -or $_.CommandLine -like '*claude.CMD*' } }
)
foreach ($t in $targets) {
  Get-CimInstance Win32_Process -Filter "Name='$($t.Name)'" | Where-Object $t.Filter | ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++; Write-Output "  killed PID $($_.ProcessId) ($($t.Name))" }
    catch { Write-Output "  FAILED PID $($_.ProcessId): $($_.Exception.Message)" }
  }
}
Write-Output "[6/9] killed $killed processes"

# 7. Sleep + force-stale heartbeat (race-safe: give Stop-Process time to settle)
Start-Sleep -Seconds 2
Remove-Item "D:\Wren-Companion\.tmp\watchdog_heartbeat.txt" -Force -ErrorAction SilentlyContinue
Write-Output "[7/9] heartbeat removed (watchdog now appears missing to keepalive)"

# 8. Trigger keepalive to spawn admin chain
Start-ScheduledTask -TaskName 'Iris-Keepalive'
Write-Output "[8/9] keepalive triggered - admin chain respawn within ~30-90s (full body ~5min)"

# 9. Final state summary
$final = Get-ScheduledTask -TaskName 'Iris-Keepalive'
$finalInfo = Get-ScheduledTaskInfo -TaskName 'Iris-Keepalive'
Write-Output "[9/9] FINAL: RunLevel=$($final.Principal.RunLevel) State=$($final.State) NextRunTime=$($finalInfo.NextRunTime) LastTaskResult=$($finalInfo.LastTaskResult)"
Write-Output ""
Write-Output "Verify in 90 sec:  Get-Process claude | Select Id,StartTime"
Write-Output "If no new CC:      cd D:\Wren-Companion ; .\start_iris.bat  (from this same elevated PS)"
Write-Output "Full restore:      Unregister-ScheduledTask -TaskName 'Iris-Keepalive' -Confirm:`$false ; Register-ScheduledTask -Xml (Get-Content '$backupPath' -Raw) -TaskName 'Iris-Keepalive'"
