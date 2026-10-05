# git_push_desktop.ps1 — push a repo from a session that can't reach the credential vault.
#
# Found 2026-10-05 (planned test reboot): after an UNATTENDED boot, my stack's processes are not a
# "desktop session" to Git Credential Manager — `git push` dies with
#   "Unable to persist credentials with the 'wincredman' credential store"
# while the very same push succeeds from a one-shot scheduled task created with /IT (runs in
# Zeke's interactive logon session). So: hand the push to such a task, wait, return its output.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\git_push_desktop.ps1 [-Repo D:\ClaudeCodeMemory] [-Branch master]
# Exit code 0 = pushed (or already up to date), 1 = failed (log printed).
param(
    [string]$Repo = "D:\Wren-Companion",
    [string]$Branch = "",
    [int]$TimeoutS = 60
)
$ErrorActionPreference = "Stop"
if (-not $Branch) { $Branch = (git -C $Repo rev-parse --abbrev-ref HEAD).Trim() }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path "D:\Wren-Companion\.tmp" "gpd_$stamp.log"   # short: schtasks /TR caps at 261 chars
$done = "$log.done"
$task = "Iris-GitPush-$stamp"
$cmd = "cmd.exe /c cd /d `"$Repo`" && git push origin $Branch > `"$log`" 2>&1 & echo %errorlevel% > `"$done`""
schtasks /create /tn $task /tr $cmd /sc once /st 23:59 /it /f | Out-Null
schtasks /run /tn $task | Out-Null
$t0 = Get-Date
while (-not (Test-Path $done) -and ((Get-Date) - $t0).TotalSeconds -lt $TimeoutS) { Start-Sleep -Milliseconds 500 }
schtasks /delete /tn $task /f | Out-Null
if (Test-Path $log) { Get-Content $log }
$ahead = (git -C $Repo rev-list --count "origin/$Branch..$Branch" 2>$null)
if ($ahead -eq "0") { "PUSHED: $Repo ($Branch) is level with origin"; exit 0 }
"NOT PUSHED: $Repo is $ahead commit(s) ahead of origin/$Branch"; exit 1
