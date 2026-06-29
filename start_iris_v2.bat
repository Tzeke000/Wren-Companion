@echo off
setlocal
REM ============================================================================
REM start_iris_v2.bat - PARALLEL Agent-SDK BODY-host launcher (cutover-held).
REM
REM Brings Iris up on iris_body_host.py (the Agent-SDK host = own runtime loop,
REM streaming voice) INSTEAD of the interactive claude CLI. This is the v2 path.
REM
REM It does NOT replace start_iris.bat - that stays the clean CLI fallback
REM (mirrors how Zeke runs Wren's start_wren_v2.bat alongside her M1 fallback).
REM If v2 misbehaves, run start_iris.bat for the known-good interactive CLI.
REM
REM The host spawns the bundled claude.exe (--output-format stream-json) itself
REM via the Agent SDK, authenticating with oauth from ~/.claude/.credentials.json
REM (no API key, draws on the Max subscription). No pywinpty - the SDK is headless.
REM ============================================================================

cd /d D:\Wren-Companion

REM --- Self-elevate (Zeke 2026-06-28): run Iris in Admin so the watchdog can fully
REM --- manage AND kill an elevated voice stack (the old-CLI respawn bug was rooted in
REM --- a non-admin host unable to kill an elevated orphan watchdog). If not elevated,
REM --- relaunch this script via UAC and exit. `net session` succeeds only when admin,
REM --- so the elevated relaunch can't loop. If you'd rather not elevate, use the CLI
REM --- fallback start_iris.bat (untouched, known-good, non-admin).
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [start_iris_v2.bat] not elevated - requesting Administrator via UAC...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    endlocal
    exit /b
)
echo [start_iris_v2.bat] running elevated.

if not exist "D:\Wren-Companion\.venv\Scripts\python.exe" (
    echo [start_iris_v2.bat] ERROR: venv missing at D:\Wren-Companion\.venv\Scripts\python.exe 1>&2
    endlocal
    exit /b 2
)

REM --- Kill the STALE voice stack BEFORE the watchdog launches, so an orphaned old
REM --- watchdog can never keep the singleton mutex (which makes our fresh watchdog
REM --- no-op) and keep owning/respawning voice. We match by SCRIPT NAME (watchdog +
REM --- daemon + StyleTTS2 mouth), not by port, so it also clears a stuck-loading mouth
REM --- and the mutex-holding watchdog itself. Now elevated, this reaches an elevated
REM --- orphan too. Scoped to the 3 voice scripts: spares post-office, operator API,
REM --- and the body host (different script names).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'voice_watchdog|wren_voice_daemon|wren_styletts_server' } | ForEach-Object { Write-Host ('[start_iris_v2.bat] killing stale voice PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

REM Voice stack (StyleTTS2 mouth :8769 + voice daemon :8770) via the watchdog,
REM same as start_iris.bat. The watchdog has a named-mutex singleton guard, so a
REM second launch is a safe no-op if it's already up from a prior boot.
start "iris-voice-watchdog" /B "D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\voice_watchdog.py"

REM The host IS the cognition. Run it in the foreground so this window is Iris.
"D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\iris_body_host.py"

endlocal
