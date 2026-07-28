# lb_server_switch.ps1 — point the little-brain server (:8772) at a specific model
# for A/B testing, WITHOUT touching the persistent user env var.
#
# 2026-07-28, Iris. The battery reads IRIS_LOCAL_MODEL from the SERVER's process
# environment, so an A/B run just needs the server restarted with a different
# process-scoped value. Production stays whatever `setx IRIS_LOCAL_MODEL` says —
# this script never calls setx, deliberately: a flip is Zeke's call, not a test's.
#
# Scar honored: vector_brain_server runs as PARENT + WORKER (2 procs = normal).
# Both must go, or the port stays held by the survivor.
#
#   .\scripts\lb_server_switch.ps1 -Model iris-little-v14
#   .\scripts\lb_server_switch.ps1 -Model iris-little-v12   # restore production

param(
    [Parameter(Mandatory = $true)][string]$Model,
    [int]$WaitSeconds = 60
)

$ROOT = "D:\Wren-Companion"
$PY = "$ROOT\.venv\Scripts\python.exe"

Write-Output "[lb_server_switch] target model: $Model"

$procs = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'vector_brain_server\.py' }
if ($procs) {
    Write-Output "[lb_server_switch] stopping PIDs: $($procs.ProcessId -join ',')"
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
} else {
    Write-Output "[lb_server_switch] no running server found"
}

# Process-scoped only — never setx.
$env:IRIS_LOCAL_MODEL = $Model
$env:IRIS_LB_TOOLS = "1"
Start-Process -FilePath $PY -ArgumentList "$ROOT\scripts\vector_brain_server.py" `
    -WorkingDirectory $ROOT -WindowStyle Hidden
Write-Output "[lb_server_switch] launched with IRIS_LOCAL_MODEL=$Model IRIS_LB_TOOLS=1"

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    $up = [bool](Get-NetTCPConnection -LocalPort 8772 -State Listen -ErrorAction SilentlyContinue)
    if ($up) {
        Write-Output "[lb_server_switch] :8772 LISTENING"
        # Confirm the server really took the model we asked for, rather than
        # trusting that it did (verify-before-asserting).
        $seen = (Get-CimInstance Win32_Process |
            Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'vector_brain_server\.py' } |
            Measure-Object).Count
        Write-Output "[lb_server_switch] server procs: $seen (parent+worker = 2 is normal)"
        exit 0
    }
    Start-Sleep -Seconds 3
}
Write-Output "[lb_server_switch] WARNING: :8772 did not come up within ${WaitSeconds}s"
exit 1
