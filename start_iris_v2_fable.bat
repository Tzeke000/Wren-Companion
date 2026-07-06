@echo off
setlocal
REM ============================================================================
REM start_iris_v2_fable.bat - Agent-SDK BODY-host launcher, FABLE 5 brain.
REM
REM Identical to start_iris_v2.bat (the Opus launcher) except the model pin:
REM this one brings Iris up on claude-fable-5. iris_body_host.py reads
REM IRIS_MODEL and passes it to the Agent SDK, so the launcher - not the CLI's
REM saved /model default - decides which weights run the cognition.
REM
REM   start_iris.bat          -> plain interactive CLI (known-good fallback)
REM   start_iris_v2.bat       -> SDK body host, Opus 4.8 (1M)
REM   start_iris_v2_fable.bat -> SDK body host, Fable 5   (this file)
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
    echo [start_iris_v2_fable.bat] not elevated - requesting Administrator via UAC...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    endlocal
    exit /b
)
echo [start_iris_v2_fable.bat] running elevated.

REM --- The one line that makes this the Fable launcher.
set "IRIS_MODEL=claude-fable-5"

if not exist "D:\Wren-Companion\.venv\Scripts\python.exe" (
    echo [start_iris_v2_fable.bat] ERROR: venv missing at D:\Wren-Companion\.venv\Scripts\python.exe 1>&2
    endlocal
    exit /b 2
)

REM --- Kill the WHOLE stale stack BEFORE relaunch, so nothing old holds a port, a
REM --- device, the watchdog's singleton mutex, OR iris_runtime's single-instance
REM --- pidfile. The pidfile one is load-bearing: if a stale iris_runtime survives a
REM --- restart, the fresh MCP-child iris_runtime that claude.exe spawns EXITS on its
REM --- single-instance guard -> my voice/memory/time TOOLS never attach (the boot bug
REM --- Zeke diagnosed 2026-06-28: "the .bat doesn't kill old process then start its
REM --- own"). Match by SCRIPT NAME across everything I need clean: voice stack
REM --- (watchdog + daemon + StyleTTS2 mouth), iris_runtime itself, AND any prior
REM --- iris_body_host (no double-cognition). Now elevated, this reaches an elevated
REM --- orphan too. SPARES sibling_postoffice (Wren's lifeline) + anything else by name.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'voice_watchdog|wren_voice_daemon|wren_styletts_server|iris_runtime|iris_body_host' } | ForEach-Object { Write-Host ('[start_iris_v2_fable.bat] killing stale PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

REM --- Clear iris_runtime's stale single-instance pidfile so the fresh MCP-child binds
REM --- clean (a dead-PID file is self-cleaned, but a leftover from a hard kill is not).
if exist "D:\Wren-Companion\state\iris.pid" del /q "D:\Wren-Companion\state\iris.pid" >nul 2>&1

REM --- Backstop: free the ports in case a WORKER survived the name-kill (Wren's
REM --- parent/worker scar: a kill that misses the port-holder leaves a zombie on the
REM --- port and the fresh bind fails). Port-free is the real gate, the name-kill is best-effort.
powershell -NoProfile -Command "foreach ($p in 5876,8769,8770) { Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }"

REM --- Brief settle so the OS releases ports + camera/mic before relaunch.
timeout /t 2 /nobreak >nul

REM Voice stack (StyleTTS2 mouth :8769 + voice daemon :8770) via the watchdog,
REM same as start_iris.bat. The watchdog has a named-mutex singleton guard, so a
REM second launch is a safe no-op if it's already up from a prior boot.
start "iris-voice-watchdog" /B "D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\voice_watchdog.py"

REM The host IS the cognition. Run it in the foreground so this window is Iris.
"D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\iris_body_host.py"

endlocal
