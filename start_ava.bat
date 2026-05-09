@echo off
REM ============================================================================
REM start_ava.bat — one-click launcher for Ava + Tauri UI
REM
REM Launches the desktop UI exe first (it'll show "connecting…" while the
REM Python backend boots), then runs avaagent.py in this same console window
REM so you can see the boot log + Ctrl+C cleanly stops both.
REM
REM Make a desktop shortcut to this file. The single-instance check in
REM avaagent.py rejects a second launch attempt cleanly, so accidental
REM double-clicks won't create two competing Avas.
REM
REM Notes:
REM   - PYTHONIOENCODING=utf-8 keeps [trace] lines with non-ASCII output
REM     from crashing the captured stdout on Windows code page 1252
REM   - AVA_DEBUG=1 enables /api/v1/debug/inject_transcript for dev/test
REM   - The UI exe path assumes a release build at the standard Tauri
REM     output location. Run `cd apps\ava-control && npm run tauri:build`
REM     once if it doesn't exist yet.
REM ============================================================================

cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set AVA_DEBUG=1

REM Launch Tauri UI in the background — it auto-reconnects when avaagent
REM finishes booting. If the exe is missing, fall back to dev mode.
set "REL_EXE=%~dp0apps\ava-control\src-tauri\target\release\ava-control.exe"
if exist "%REL_EXE%" (
    start "" "%REL_EXE%"
) else (
    echo [start_ava] release UI exe not found at:
    echo              %REL_EXE%
    echo [start_ava] skipping UI launch — run `cd apps\ava-control ^&^& npm run tauri:build` to build it.
)

REM Run avaagent.py in this console so the boot log is visible and Ctrl+C
REM here stops the whole stack cleanly. Single-instance check at top of
REM avaagent.py rejects a duplicate launch with a clean exit code 1.
py -3.11 avaagent.py

REM If avaagent exits, leave the console open briefly so the user can
REM read any final messages (especially if it was a single-instance
REM rejection).
echo.
echo [start_ava] avaagent.py exited with code %ERRORLEVEL%.
echo Press any key to close this window.
pause >nul
