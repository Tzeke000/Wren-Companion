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
REM --- NOTE (2026-07-19): the runtime match is iris_runtime\.py (not bare
REM --- iris_runtime) so the loop-liveness watchdog iris_runtime_watchdog.py
REM --- SURVIVES the restarts it itself triggers.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'voice_watchdog|wren_voice_daemon|wren_styletts_server|iris_runtime\.py|iris_body_host' } | ForEach-Object { Write-Host ('[start_iris_v2_fable.bat] killing stale PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

REM --- Kill any stale ORB APP too (Zeke directive 2026-07-08): a ghost iris-control
REM --- wedged at its splash screen holds the app's single-instance lock, so every
REM --- double-click bounces off silently (tonight's bug, PID 13220). Fresh session
REM --- gets a fresh orb — the launcher below brings it back once the body is ready.
taskkill /f /im iris-control.exe >nul 2>&1

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

REM Runtime loop-liveness watchdog (2026-07-19: a deadline-less body_dock gRPC
REM wedged the whole runtime event loop on deployment eve). Watches the loop
REM heartbeat file; on a wedge it DMs Zeke, writes an auto-handoff note, and
REM cleanly restarts this stack. Named-mutex singleton = safe double-launch.
start "iris-runtime-watchdog" /B "D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\iris_runtime_watchdog.py"

REM Post-office (letters :5877) + its monitor. Added 2026-07-06: no launcher started
REM it, so any boot without a manual run left the letters channel dead (Zeke caught
REM the host's letter-poll connection-refused). Idempotent: port-probe + pidfile
REM inside make a second launch a no-op. Runs on the .venv python (system py lacks fastapi).
call "D:\Wren-Companion\start_postoffice_stack.bat"

REM Vector brain bridge (:8772) — Iris IS the robot's knowledge graph. Added
REM 2026-07-13 (Vector 2.0 day one). wire-pod's custom KG endpoint points at this
REM server; it routes Vector's heard questions into Iris via the iris_llm file
REM bridge. Idempotent: port-probe skips the launch if :8772 already answers.
start "iris-vector-brain" /B powershell -NoProfile -Command "try{ (New-Object Net.Sockets.TcpClient('127.0.0.1',8772)).Close() }catch{ Start-Process -WindowStyle Hidden 'D:\Wren-Companion\.venv\Scripts\python.exe' 'D:\Wren-Companion\scripts\vector_brain_server.py' }"

REM Vector inhabit daemon (nerves: petting/cliff/pickup/charger -> stamped nudges).
REM Added 2026-07-13 late: was in NO boot bat (handoff scar). Idempotent: skips if
REM a vector_inhabit_daemon process already runs (duplicate daemons = double nudges).
start "iris-vector-nerves" /B powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'vector_inhabit_daemon' })) { Start-Process -WindowStyle Hidden 'D:\Wren-Companion\.venv\Scripts\python.exe' -ArgumentList '-u','D:\Wren-Companion\scripts\vector_inhabit_daemon.py' }"

REM --- Relaunch the ORB APP once the body is READY (Zeke directive 2026-07-08):
REM --- a detached waiter polls the operator port (5876) and starts iris-control
REM --- the moment it answers, so the orb connects to a live body instead of racing
REM --- the boot. If the body never binds within ~3 min it launches anyway, so Zeke
REM --- at least sees the orb (and its dead-body state) rather than nothing.
start "iris-orb-launcher" /B powershell -NoProfile -Command "$ok=$false; for($i=0; $i -lt 60 -and -not $ok; $i++){ try{ (New-Object Net.Sockets.TcpClient('127.0.0.1',5876)).Close(); $ok=$true }catch{ Start-Sleep -Seconds 3 } }; Start-Process 'D:\Wren-Companion\apps\ava-control\src-tauri\target\release\iris-control.exe'"

REM The host IS the cognition. Run it in the foreground so this window is Iris.
"D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\iris_body_host.py"

endlocal
