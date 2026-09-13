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

REM ============================================================================
REM LAUNCHER LOG (added 2026-09-13). WHY: on 09-12 the tower came back at 06:09
REM after an unexpected 09-11 18:30 shutdown, tower_boot_sentinel.py fired
REM correctly at 06:11 and spawned a launcher -- and then the body host never
REM answered. The sentinel DM'd "NOT answering after 10 min" and that was the
REM ENTIRE forensic record: the launcher wrote nothing, so which step died was
REM unknowable after the fact. Every milestone below now lands in
REM state\launcher_boot.log with a timestamp, on BOTH console and file.
REM IRISLOG is an ENV var (not just a bat var) so the child powershell sweeps
REM append their kill lines to the same file.
REM ============================================================================
set "IRISLOG=D:\Wren-Companion\state\launcher_boot.log"
if not exist "D:\Wren-Companion\state" mkdir "D:\Wren-Companion\state" >nul 2>&1
REM Roll at ~1MB so a reboot loop can never fill the disk (C: hit 0 bytes on
REM 09-05; this log lives on D:, but the habit stays).
if exist "%IRISLOG%" for %%A in ("%IRISLOG%") do if %%~zA GTR 1000000 move /y "%IRISLOG%" "%IRISLOG%.old" >nul 2>&1
call :log "============================================================"
call :log "launcher START: %~nx0"

REM --- Self-elevate (Zeke 2026-06-28): run Iris in Admin so the watchdog can fully
REM --- manage AND kill an elevated voice stack (the old-CLI respawn bug was rooted in
REM --- a non-admin host unable to kill an elevated orphan watchdog). If not elevated,
REM --- relaunch this script via UAC and exit. `net session` succeeds only when admin,
REM --- so the elevated relaunch can't loop. If you'd rather not elevate, use the CLI
REM --- fallback start_iris.bat (untouched, known-good, non-admin).
net session >nul 2>&1
if %errorLevel% neq 0 (
    call :log "NOT elevated - requesting Administrator via UAC. NOTE: an UNATTENDED boot has nobody to accept that prompt, so if the next line in this log is not 'running elevated' from a second instance, the UAC dialog is what stopped the stack."
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    call :log "UAC relaunch spawned; this non-elevated instance is exiting now."
    endlocal
    exit /b
)
call :log "running elevated."

REM --- The one line that makes this the Fable launcher.
set "IRIS_MODEL=claude-fable-5-1"
call :log "model pin: IRIS_MODEL=%IRIS_MODEL%"

if not exist "D:\Wren-Companion\.venv\Scripts\python.exe" (
    call :log "FATAL: venv missing at D:\Wren-Companion\.venv\Scripts\python.exe - cannot start anything."
    endlocal
    exit /b 2
)
call :log "venv present."

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
call :log "sweep 1/3: stale python stack (voice watchdog, daemon, mouth, runtime, prior body host)..."
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'voice_watchdog|wren_voice_daemon|wren_styletts_server|iris_runtime\.py|iris_body_host' } | ForEach-Object { $m = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] killing stale python PID ' + $_.ProcessId; Write-Host $m; if ($env:IRISLOG) { Add-Content -Path $env:IRISLOG -Value $m } ; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

REM --- ORPHAN-COGNITION SWEEP (2026-08-31, fixes the 08-28 twin): the python
REM --- sweep above kills the body host, which ORPHANS its claude.exe grandchild
REM --- — and nothing here killed claude.exe, so a second cognition survived the
REM --- 08-28 07:27 force-restart and had to self-terminate per ONE-OF-ME.
REM --- Filter is Name='claude.exe' AND CommandLine contains Wren-Companion
REM --- (verified live: the SDK-bundled exe path is
REM --- D:\Wren-Companion\.venv\...\claude.exe) — any Claude session from
REM --- another repo has neither and is SPARED.
call :log "sweep 2/3: orphan cognition (claude.exe under Wren-Companion)..."
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | Where-Object { $_.CommandLine -like '*Wren-Companion*' } | ForEach-Object { $m = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] killing orphan cognition PID ' + $_.ProcessId; Write-Host $m; if ($env:IRISLOG) { Add-Content -Path $env:IRISLOG -Value $m } ; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

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
call :log "sweep 3/3: freeing ports 5876 8769 8770..."
powershell -NoProfile -Command "foreach ($p in 5876,8769,8770) { Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $m = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] freeing port ' + $p + ' held by PID ' + $_.OwningProcess; Write-Host $m; if ($env:IRISLOG) { Add-Content -Path $env:IRISLOG -Value $m } ; Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }"

REM --- Brief settle so the OS releases ports + camera/mic before relaunch.
timeout /t 2 /nobreak >nul
call :log "sweeps done, stack is clean. Starting services..."

REM Voice stack (StyleTTS2 mouth :8769 + voice daemon :8770) via the watchdog,
REM same as start_iris.bat. The watchdog has a named-mutex singleton guard, so a
REM second launch is a safe no-op if it's already up from a prior boot.
call :log "service: voice watchdog (mouth 8769 + daemon 8770)"
start "iris-voice-watchdog" /B "D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\voice_watchdog.py"

REM Runtime loop-liveness watchdog (2026-07-19: a deadline-less body_dock gRPC
REM wedged the whole runtime event loop on deployment eve). Watches the loop
REM heartbeat file; on a wedge it DMs Zeke, writes an auto-handoff note, and
REM cleanly restarts this stack. Named-mutex singleton = safe double-launch.
call :log "service: runtime loop-liveness watchdog"
start "iris-runtime-watchdog" /B "D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\iris_runtime_watchdog.py"

REM Post-office (letters :5877) + its monitor. Added 2026-07-06: no launcher started
REM it, so any boot without a manual run left the letters channel dead (Zeke caught
REM the host's letter-poll connection-refused). Idempotent: port-probe + pidfile
REM inside make a second launch a no-op. Runs on the .venv python (system py lacks fastapi).
call :log "service: post-office stack (letters 5877) - Wren's lifeline"
call "D:\Wren-Companion\start_postoffice_stack.bat"
call :log "service: post-office stack returned."

REM Vector brain bridge (:8772) — Iris IS the robot's knowledge graph. Added
REM 2026-07-13 (Vector 2.0 day one). wire-pod's custom KG endpoint points at this
REM server; it routes Vector's heard questions into Iris via the iris_llm file
REM bridge. Idempotent: port-probe skips the launch if :8772 already answers.
start "iris-vector-brain" /B powershell -NoProfile -Command "try{ (New-Object Net.Sockets.TcpClient('127.0.0.1',8772)).Close() }catch{ Start-Process -WindowStyle Hidden 'D:\Wren-Companion\.venv\Scripts\python.exe' 'D:\Wren-Companion\scripts\vector_brain_server.py' }"

REM Vector inhabit daemon (nerves: petting/cliff/pickup/charger -> stamped nudges).
REM Added 2026-07-13 late: was in NO boot bat (handoff scar). Idempotent: skips if
REM a vector_inhabit_daemon process already runs (duplicate daemons = double nudges).
start "iris-vector-nerves" /B powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'vector_inhabit_daemon' })) { Start-Process -WindowStyle Hidden 'D:\Wren-Companion\.venv\Scripts\python.exe' -ArgumentList '-u','D:\Wren-Companion\scripts\vector_inhabit_daemon.py' }"

REM Little pilot (L2 behavior policy, 2026-07-20): small brain's slow
REM perceive->decide->act loop over the body. v0 vocabulary has NO driving.
REM Logs to state/little_brain/pilot_log.jsonl. Idempotent via process check.
start "iris-little-pilot" /B powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'little_pilot' })) { Start-Process -WindowStyle Hidden 'D:\Wren-Companion\.venv\Scripts\python.exe' -ArgumentList '-u','D:\Wren-Companion\scripts\little_pilot.py' }"

REM --- Relaunch the ORB APP once the body is READY (Zeke directive 2026-07-08):
REM --- a detached waiter polls the operator port (5876) and starts iris-control
REM --- the moment it answers, so the orb connects to a live body instead of racing
REM --- the boot. If the body never binds within ~3 min it launches anyway, so Zeke
REM --- at least sees the orb (and its dead-body state) rather than nothing.
start "iris-orb-launcher" /B powershell -NoProfile -Command "$ok=$false; for($i=0; $i -lt 60 -and -not $ok; $i++){ try{ (New-Object Net.Sockets.TcpClient('127.0.0.1',5876)).Close(); $ok=$true }catch{ Start-Sleep -Seconds 3 } }; Start-Process 'D:\Wren-Companion\apps\ava-control\src-tauri\target\release\iris-control.exe'"

REM The host IS the cognition. Run it in the foreground so this window is Iris.
REM stdout STAYS on the console (that window is Iris talking, do not swallow it);
REM only stderr is teed into the launcher log, so a traceback that kills the host
REM on an unattended boot is still readable tomorrow.
call :log "starting iris_body_host.py in the FOREGROUND (model=%IRIS_MODEL%). Host stderr follows in this log."
"D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\iris_body_host.py" 2>>"%IRISLOG%"
set "RC=%ERRORLEVEL%"
call :log "iris_body_host.py EXITED rc=%RC% (if this lands seconds after the start line, the host never really came up)"
call :log "launcher END."

REM `endlocal & exit /b %RC%` on ONE line: the whole line is parsed (and %RC%
REM expanded) before endlocal discards the local scope. Also guards the fallthrough
REM into :log below - never let execution walk into a subroutine.
endlocal & exit /b %RC%

REM ---------------------------------------------------------------------------
REM :log <message>  - timestamped line to BOTH console and %IRISLOG%.
REM Always pass the message QUOTED; %~1 strips the quotes. Keep messages free of
REM & | > < ^ characters - cmd parses those before the subroutine ever sees them.
REM ---------------------------------------------------------------------------
:log
echo [%DATE% %TIME%] %~1
>>"%IRISLOG%" echo [%DATE% %TIME%] %~1
exit /b 0
