@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ============================================================================
REM Ava desktop — operator console + Python runtime
REM
REM Order of operations:
REM   1) Start avaagent.py (Gradio + operator HTTP on :5876) in a separate window.
REM   2) Wait until operator HTTP responds (wait_operator_http.ps1).
REM   3) Primary UI: packaged ava-control.exe when present.
REM   4) Fallback: npm run tauri:dev (Vite dev server + Tauri) if exe is missing.
REM
REM Release build (produces ava-control.exe):
REM   - Requires Windows icon: apps\ava-control\src-tauri\icons\icon.ico
REM     (Tauri embeds it for the Windows resource step.)
REM   - From repo:  scripts\build_desktop_release.bat
REM     or:         cd apps\ava-control && npm install && npm run tauri:build
REM   - Output:     apps\ava-control\src-tauri\target\release\ava-control.exe
REM
REM Dev fallback needs: Node, npm, Rust toolchain; first run may run npm install.
REM ============================================================================

set "REL_EXE=%~dp0apps\ava-control\src-tauri\target\release\ava-control.exe"

echo.
echo [ava-launch] === Ava desktop launch ==========================================
echo [ava-launch] Repo: %~dp0
echo.

echo [ava-launch] Step 1/4: Starting Python backend ^(avaagent.py + watchdog, minimized^)...
REM Clear stale restart flag so watchdog doesn't immediately relaunch avaagent
if exist "%~dp0state\restart_requested.flag" (
  del /f /q "%~dp0state\restart_requested.flag"
  echo [ava-launch] Cleared stale restart_requested.flag
)
start "Ava Python" /MIN /D "%~dp0" py -3.11 avaagent.py
if errorlevel 1 (
  echo [ava-launch] ERROR: could not start py -3.11 avaagent.py ^(is Python 3.11 on PATH?^).
  pause
  exit /b 1
)
echo [ava-launch] Step 1/4: OK — Python process started ^(separate window^).
if exist "%~dp0scripts\watchdog.py" (
  start "Ava Watchdog" /MIN /D "%~dp0" py -3.11 scripts\watchdog.py
  echo [ava-launch] Step 1/4: Watchdog started.
)

if not exist "%~dp0wait_operator_http.ps1" (
  echo [ava-launch] ERROR: missing "%~dp0wait_operator_http.ps1" ^(required for startup wait^).
  pause
  exit /b 1
)

echo [ava-launch] Step 2/4: Waiting for operator HTTP at http://127.0.0.1:5876 ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wait_operator_http.ps1"
set "OP_FAIL=0"
if errorlevel 1 set "OP_FAIL=1"
if "!OP_FAIL!"=="1" (
  echo [ava-launch] Step 2/4: WARNING — operator HTTP not ready in time.
  echo           UI will still start; operator console may show ^"offline^" until fixed.
) else (
  echo [ava-launch] Step 2/4: OK — operator HTTP is responding.
)

echo [ava-launch] Step 3/4: Choosing desktop UI ^(packaged exe vs dev server^)...

if exist "!REL_EXE!" (
  echo [ava-launch] Primary interface: PACKAGED — !REL_EXE!
  echo [ava-launch] Step 4/4: Launching ava-control.exe ^(working dir: repo root^)...
  start "" /D "%~dp0" "!REL_EXE!"
  echo [ava-launch] Step 4/4: OK — packaged app start requested.
  if "!OP_FAIL!"=="1" echo [ava-launch] Reminder: resolve operator HTTP if the window stays offline.
  echo [ava-launch] === Launch sequence complete ^(packaged^) =========================
  pause
  exit /b 0
)

echo [ava-launch] Packaged exe NOT found at:
echo           apps\ava-control\src-tauri\target\release\ava-control.exe
echo [ava-launch] FALLBACK: development mode — npm run tauri:dev
echo [ava-launch] Build release when ready: scripts\build_desktop_release.bat
echo.

cd apps\ava-control
if errorlevel 1 (
  echo [ava-launch] ERROR: cannot cd to apps\ava-control
  pause
  exit /b 1
)

if not exist node_modules (
  echo [ava-launch] Step 4/4: node_modules missing — running npm install...
  call npm install
  if errorlevel 1 (
    echo [ava-launch] ERROR: npm install failed. Fix Node/npm and retry.
    pause
    exit /b 1
  )
  echo [ava-launch] Step 4/4: npm install finished.
) else (
  echo [ava-launch] Step 4/4: node_modules present — skipping npm install.
)

echo [ava-launch] Step 4/4: Starting Tauri dev ^(Vite + tauri dev^)...
call npm run tauri:dev
set "TAURI_EC=!ERRORLEVEL!"
if not "!TAURI_EC!"=="0" (
  echo [ava-launch] ERROR: tauri:dev exited with code !TAURI_EC!.
  echo          Check Rust/MSVC, Node, and errors above.
  pause
  exit /b !TAURI_EC!
)

echo [ava-launch] tauri:dev ended.
echo [ava-launch] === Launch sequence complete ^(dev fallback^) =====================
pause
exit /b 0
