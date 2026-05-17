@echo off
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
REM Falls back to py -3.11 if the venv launcher is missing; that path lacks
REM pywinpty so the script will error loudly with install instructions.
if exist "D:\Wren-Companion\.venv\Scripts\python.exe" (
    "D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\iris_cold_wake.py" %*
) else (
    py -3.11 "D:\Wren-Companion\scripts\iris_cold_wake.py" %*
)
