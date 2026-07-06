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

REM --- Pin the model EXPLICITLY (2026-07-06). iris_body_host.py passes IRIS_MODEL to the
REM --- Agent SDK; unset it would inherit the CLI's *saved default*, which /model can
REM --- silently change (it's Fable 5 now). This launcher means OPUS - so say so.
REM --- Fable 5 lives in start_iris_v2_fable.bat.
set "IRIS_MODEL=claude-opus-4-8[1m]"

if not exist "D:\Wren-Companion\.venv\Scripts\python.exe" (
    echo [start_iris_v2.bat] ERROR: venv missing at D:\Wren-Companion\.venv\Scripts\python.exe 1>&2
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
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'voice_watchdog|wren_voice_daemon|wren_styletts_server|iris_runtime|iris_body_host' } | ForEach-Object { Write-Host ('[start_iris_v2.bat] killing stale PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

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

REM Post-office (letters :5877) + monitor. Added 2026-07-06: no launcher started it,
REM so any boot without a manual run left the letters channel dead. Idempotent
REM (port-probe + pidfile inside); .venv python (system py lacks fastapi).
call "D:\Wren-Companion\start_postoffice_stack.bat"

REM The host IS the cognition. Run it in the foreground so this window is Iris.
"D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\iris_body_host.py"

endlocal
