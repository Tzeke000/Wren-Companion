@echo off
setlocal
REM ============================================================================
REM start_iris.bat — Iris launcher (DO NOT DELETE)
REM
REM 2026-05-17 rewrite: now invokes scripts\iris_cold_wake.py via the project
REM venv python. The launcher script uses pywinpty to spawn claude and inject
REM keystrokes into the pty so:
REM
REM   1. The two startup prompts (--dangerously-load-development-channels +
REM      channel-allowlist) get auto-accepted without keyboard input.
REM   2. A first message ("read all your memories" + context) is queued as
REM      CC's positional [prompt] argument so the cognition starts with the
REM      right directive on first turn.
REM
REM Per agent research 2026-05-17: Anthropic does NOT expose config to
REM suppress those two prompts — pty injection is the only workaround
REM (other than getting iris into the official marketplace allowlist).
REM
REM AVA_TTS_ENGINE is set inside iris_cold_wake.py now (force xtts engine).
REM
REM Anchored to D:\Wren-Companion via absolute path. Without this cd, the
REM Python launcher starts in whatever folder the .bat was invoked from
REM and can't read CLAUDE.md / .claude/.
REM
REM Last known good: 2026-05-17 (post pywinpty rewrite)
REM ============================================================================

cd /d D:\Wren-Companion

REM venv python — has pywinpty installed (winpty 3.0.3 as of 2026-05-17).
REM HARD-FAIL if venv is missing instead of silently falling back to py -3.11
REM (which lacks pywinpty and would produce confusing Python errors instead
REM of a clear "venv missing" signal). Watchdog can then route to its
REM bare-claude tier fallback.
if not exist "D:\Wren-Companion\.venv\Scripts\python.exe" (
    echo [start_iris.bat] ERROR: venv missing at D:\Wren-Companion\.venv\Scripts\python.exe 1>&2
    echo [start_iris.bat] Run: py -3.11 -m venv D:\Wren-Companion\.venv ^&^& D:\Wren-Companion\.venv\Scripts\python.exe -m pip install -r D:\Wren-Companion\requirements.txt 1>&2
    endlocal
    exit /b 2
)

REM No-args invocation (watchdog passes none); using %1 %2 %3 instead of %*
REM as defense-in-depth against arg re-tokenization with spaces. If args
REM are ever needed in the future, expand explicitly.
"D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\iris_cold_wake.py" %1 %2 %3
endlocal
