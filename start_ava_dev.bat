@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ============================================================================
REM Ava DEV mode — instant hot-reload for frontend changes
REM
REM Order of operations:
REM   1) Clear stale restart flag
REM   2) Start py -3.11 avaagent.py in a minimized window (operator HTTP :5876)
REM   3) Wait until :5876 responds
REM   4) Start watchdog in a minimized window
REM   5) Run: cd apps\ava-control && npm run tauri:dev
REM
REM In tauri:dev mode:
REM   - Vite serves frontend at http://localhost:5173 with HMR
REM   - Any .tsx/.ts/.css change reflects instantly — NO exe rebuild needed
REM   - Only Rust/tauri.conf.json changes require npm run tauri:build
REM
REM For production (packaged exe): use start_ava_desktop.bat instead
REM ============================================================================

echo.
echo [ava-dev] === Ava DEV launch ^(hot-reload mode^) ================================
echo [ava-dev] Repo: %~dp0
echo.

REM ── Step 1: Clear stale restart flag ─────────────────────────────────────────
if exist "%~dp0state\restart_requested.flag" (
  del /f /q "%~dp0state\restart_requested.flag"
  echo [ava-dev] Cleared stale restart_requested.flag
)

REM ── Step 2: Start avaagent.py ────────────────────────────────────────────────
REM AVA_DEBUG=1 enables /api/v1/debug/inject_transcript and /api/v1/debug/tool_call,
REM which test harnesses + verify_*.py drivers depend on. Dev-only — production
REM (start_ava.bat) doesn't set this so debug endpoints stay locked.
REM
REM AVA_TTS_DEVICES routes Ava's Kokoro TTS to:
REM   - speakers   (Realtek — Zeke hears Ava live)
REM   - cable      (CABLE Input — feeds Ava's own STT path during test loops)
REM   - voicemeeter vaio3 input  (VM strip 7 → B3 — captured by faster-whisper
REM                               in scripts/audio_loopback_harness.py)
REM Without VAIO3, the test harness records silence on B3 and reports empty
REM replies even when Ava actually spoke. Vault: decisions/audio-routing-monitoring.md.
set AVA_DEBUG=1
set AVA_TTS_DEVICES=speakers,cable,voicemeeter vaio3 input
echo [ava-dev] Step 1/4: Starting py -3.11 avaagent.py ^(minimized, AVA_DEBUG=1, AVA_TTS_DEVICES=speakers+cable+vaio3^)...
start "Ava Python" /MIN /D "%~dp0" cmd /c "set AVA_DEBUG=1 && set AVA_TTS_DEVICES=speakers,cable,voicemeeter vaio3 input && py -3.11 avaagent.py"
if errorlevel 1 (
  echo [ava-dev] ERROR: could not start py -3.11 avaagent.py
  pause
  exit /b 1
)
echo [ava-dev] Step 1/4: OK — avaagent.py started.

REM ── Step 3: Wait for operator HTTP ───────────────────────────────────────────
if not exist "%~dp0wait_operator_http.ps1" (
  echo [ava-dev] WARNING: wait_operator_http.ps1 missing — skipping wait.
  goto skip_wait
)
echo [ava-dev] Step 2/4: Waiting for operator HTTP at http://127.0.0.1:5876 ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wait_operator_http.ps1"
set "OP_FAIL=0"
if errorlevel 1 set "OP_FAIL=1"
if "!OP_FAIL!"=="1" (
  echo [ava-dev] WARNING — operator HTTP not ready yet. UI will connect when Ava finishes starting.
) else (
  echo [ava-dev] Step 2/4: OK — operator HTTP responding.
)
:skip_wait

REM ── Step 4: Start watchdog ───────────────────────────────────────────────────
if exist "%~dp0scripts\watchdog.py" (
  start "Ava Watchdog" /MIN /D "%~dp0" py -3.11 scripts\watchdog.py
  echo [ava-dev] Step 3/4: Watchdog started.
)

REM ── Step 5: Tauri dev (Vite HMR) ─────────────────────────────────────────────
echo [ava-dev] Step 4/4: Starting tauri:dev ^(Vite + Tauri with hot-reload^)...
echo [ava-dev] Frontend changes refresh instantly. Ctrl+C here to stop the UI.
echo.
cd apps\ava-control
if errorlevel 1 (
  echo [ava-dev] ERROR: cannot cd to apps\ava-control
  pause
  exit /b 1
)

if not exist node_modules (
  echo [ava-dev] node_modules missing — running npm install...
  call npm install
  if errorlevel 1 (
    echo [ava-dev] ERROR: npm install failed.
    pause
    exit /b 1
  )
)

call npm run tauri:dev
set "TAURI_EC=!ERRORLEVEL!"
if not "!TAURI_EC!"=="0" (
  echo [ava-dev] tauri:dev exited with code !TAURI_EC!.
)
echo [ava-dev] === Dev session ended =============================================
pause
exit /b 0
