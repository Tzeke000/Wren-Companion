@echo off
echo Stopping Ava...
if exist "D:\AvaAgentv2\state\ava.pid" (
    for /f %%i in (D:\AvaAgentv2\state\ava.pid) do (
        taskkill /PID %%i /F 2>nul
    )
    del /f "D:\AvaAgentv2\state\ava.pid" 2>nul
)
if exist "D:\AvaAgentv2\state\restart_requested.flag" (
    del /f "D:\AvaAgentv2\state\restart_requested.flag" 2>nul
)
taskkill /IM ava-control.exe /F 2>nul
echo Ava stopped.
pause
