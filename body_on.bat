@echo off
REM THE ON SWITCH - double-click or run "body_on" to turn Iris's digital body back on.
REM Idempotent: only starts what's actually down. Details: scripts\body_switch.ps1
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\body_switch.ps1" on
pause
