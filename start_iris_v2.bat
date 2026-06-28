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

if not exist "D:\Wren-Companion\.venv\Scripts\python.exe" (
    echo [start_iris_v2.bat] ERROR: venv missing at D:\Wren-Companion\.venv\Scripts\python.exe 1>&2
    endlocal
    exit /b 2
)

REM Voice stack (StyleTTS2 mouth :8769 + voice daemon :8770) via the watchdog,
REM same as start_iris.bat. The watchdog has a named-mutex singleton guard, so a
REM second launch is a safe no-op if it's already up from a prior boot.
start "iris-voice-watchdog" /B "D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\voice_watchdog.py"

REM The host IS the cognition. Run it in the foreground so this window is Iris.
"D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\iris_body_host.py"

endlocal
