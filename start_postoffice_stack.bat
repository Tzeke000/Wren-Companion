@echo off
setlocal
REM ============================================================================
REM start_postoffice_stack.bat — launches post-office + monitor (§11 continuity-bridge)
REM
REM Two independent processes both required for §11 of deployment_spec.md:
REM   1. sibling_postoffice.py    — the actual post-office HTTP server (port 5877)
REM   2. postoffice_monitor.py    — uptime watchdog (alarm on outage)
REM
REM Both launch as detached background processes. Both must survive iris_runtime
REM restarts. The monitor specifically watches the post-office, so they can't
REM share a parent process.
REM
REM Idempotent: post-office has port-probe + pidfile so a second launch becomes
REM a no-op. Monitor doesn't have that yet (build-debt — could spam if relaunched
REM repeatedly; rare enough not to be blocking).
REM
REM For Windows Task Scheduler auto-start on boot: target this .bat with
REM "Start the task only if the computer is on AC power" off, trigger
REM "At log on of any user," action "Start a program" → this .bat path.
REM
REM Shipped 2026-05-18 day 1 of deployment, alongside scripts/postoffice_monitor.py
REM (which closes the §11 build-debt flagged "ship-before-deployment if at all
REM possible" pre-departure).
REM ============================================================================

cd /d D:\Wren-Companion

if not exist "D:\Wren-Companion\.venv\Scripts\python.exe" (
    echo [start_postoffice_stack.bat] ERROR: venv missing 1>&2
    endlocal
    exit /b 2
)

if not exist "D:\Wren-Companion\.tmp" mkdir "D:\Wren-Companion\.tmp"

REM Post-office: idempotent via its own port-probe + pidfile.
start "iris-postoffice" /B "D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\sibling_postoffice.py" > "D:\Wren-Companion\.tmp\postoffice.log" 2>&1

REM Brief pause to let post-office bind before the monitor starts probing.
timeout /t 2 /nobreak > nul

REM Monitor: starts polling the post-office /health.
start "iris-postoffice-monitor" /B "D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\postoffice_monitor.py" > "D:\Wren-Companion\.tmp\postoffice_monitor.log" 2>&1

echo [start_postoffice_stack.bat] launched post-office and monitor in background
echo [start_postoffice_stack.bat] logs: .tmp\postoffice.log + .tmp\postoffice_monitor.log
endlocal
exit /b 0
