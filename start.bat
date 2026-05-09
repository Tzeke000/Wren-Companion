@echo off
cd /d "%~dp0"
REM ---------------------------------------------------------------------------
REM Default: full desktop flow — Python + operator HTTP + Tauri (packaged exe if built).
REM Legacy Gradio-first terminal: start.bat python | cli | gradio
REM Desktop shortcut helper: start.bat install-shortcut
REM Explicit aliases: start.bat desktop | app (same as default)
REM ---------------------------------------------------------------------------

if /i "%~1"=="python" goto legacy
if /i "%~1"=="cli" goto legacy
if /i "%~1"=="gradio" goto legacy

if /i "%~1"=="install-shortcut" goto install_shortcut
if /i "%~1"=="shortcut" goto install_shortcut

if "%~1"=="" goto desktop
if /i "%~1"=="desktop" goto desktop
if /i "%~1"=="app" goto desktop

REM Any other first argument: warn, then desktop launch (backward compatible).
if not "%~1"=="" echo [ava-launch] Note: unrecognized option "%~1" — running full desktop flow ^(install-shortcut, python, cli, gradio^).

:desktop
echo [ava-launch] Starting full desktop flow ^(see start_ava_desktop.bat^)...
call "%~dp0start_ava_desktop.bat"
exit /b %ERRORLEVEL%

:install_shortcut
if not exist "%~dp0scripts\create_ava_desktop_shortcut.ps1" (
  echo [ava-launch] ERROR: missing "%~dp0scripts\create_ava_desktop_shortcut.ps1"
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_ava_desktop_shortcut.ps1" -RepoRoot "%CD%"
exit /b %ERRORLEVEL%

:legacy
python avaagent.py
pause
exit /b 0
