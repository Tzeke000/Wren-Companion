@echo off
setlocal EnableDelayedExpansion
REM Builds Vite frontend + Tauri release binary.
REM Output: ..\apps\ava-control\src-tauri\target\release\ava-control.exe
cd /d "%~dp0..\apps\ava-control"

echo [ava-build] npm install...
call npm install
if errorlevel 1 (
  echo [ava-build] ERROR: npm install failed.
  exit /b 1
)

echo [ava-build] npm run tauri:build ^(includes Vite production build via Tauri config^)...
call npm run tauri:build
set "EC=!ERRORLEVEL!"
if not "!EC!"=="0" (
  echo [ava-build] ERROR: tauri:build failed with code !EC!.
  exit /b !EC!
)

echo.
echo [ava-build] Done. Release executable:
echo           %~dp0..\apps\ava-control\src-tauri\target\release\ava-control.exe
exit /b 0
