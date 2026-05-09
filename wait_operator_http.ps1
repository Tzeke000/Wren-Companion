# Ava desktop launch helper: wait until operator HTTP responds (used by start_ava_desktop.bat).
param(
    [string]$Url = "http://127.0.0.1:5876/api/v1/health",
    [int]$MaxAttempts = 120,
    [int]$IntervalMs = 750
)
$sw = [Diagnostics.Stopwatch]::StartNew()
for ($i = 0; $i -lt $MaxAttempts; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) {
            $sec = [math]::Round($sw.Elapsed.TotalSeconds, 1)
            Write-Host "[ava-launch] operator HTTP ready (${sec}s)."
            exit 0
        }
    }
    catch {
        if (($i -eq 0) -or (($i % 10) -eq 0)) {
            $sec = [math]::Round($sw.Elapsed.TotalSeconds, 1)
            Write-Host "[ava-launch] waiting for operator HTTP... (${sec}s elapsed, attempt $($i + 1)/$MaxAttempts)"
        }
    }
    Start-Sleep -Milliseconds $IntervalMs
}
$elapsedFail = [math]::Round($sw.Elapsed.TotalSeconds, 1)
Write-Host "[ava-launch] ERROR: operator HTTP not reachable at $Url after ${elapsedFail}s ($MaxAttempts tries, ~${IntervalMs}ms pause between tries)."
Write-Host "         Check: minimized Python window for tracebacks; pip install fastapi uvicorn; AVA_OPERATOR_HTTP must not be 0."
exit 1
