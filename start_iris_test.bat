@echo off
setlocal
REM ============================================================================
REM start_iris_test.bat -- TEST launcher (DO NOT REPLACE start_iris.bat).
REM
REM PURPOSE: spawn a fresh CC + iris_cold_wake.py path WITHOUT killing the
REM current CC session. Lets us validate edits to start_iris.bat /
REM iris_cold_wake.py / boot ritual / cold-wake behavior side-by-side with
REM the running instance.
REM
REM Per Zeke directive 2026-05-19 ~12:34 EDT: when start_iris.bat or any
REM piece of the cold-wake chain gets edited, run this test FIRST. Watch
REM the new CC window come up, run through cold-wake, and reach a stable
REM state. Only after that succeeds, trust the edit for real restart_self
REM invocations.
REM
REM KNOWN LIMITATIONS:
REM   1. Does NOT test the kill-current-CC step (that's the watchdog's job).
REM      For that, use a sandbox repo copy + sandbox spawn (build-debt).
REM   2. Two iris_runtime instances will race for:
REM      - state/iris_sibling/postoffice port :5877 (newer will fail to bind)
REM      - Discord WebSocket connection (newer will kick older off)
REM      - Camera + audio devices (newer may get "device busy")
REM      - state/*.json files (interleaved writes, last-write-wins)
REM   3. The new iris_runtime skips the singleton lockfile check
REM      (IRIS_SKIP_INSTANCE_CHECK=1) so it doesn't refuse to start.
REM      Old iris_runtime's lockfile remains; new instance writes its own
REM      PID over it during shutdown (or not, depending on timing).
REM
REM Practical use: run this in a separate window, watch the new CC come up.
REM When you're done, close the new CC window (its iris_runtime exits with
REM the stdin close). The current/original CC continues unaffected (subject
REM to the resource-race caveats above).
REM
REM Last reviewed: 2026-05-19
REM ============================================================================

cd /d D:\Wren-Companion

REM venv check (same as start_iris.bat).
if not exist "D:\Wren-Companion\.venv\Scripts\python.exe" (
    echo [start_iris_test.bat] ERROR: venv missing at D:\Wren-Companion\.venv\Scripts\python.exe 1>&2
    echo [start_iris_test.bat] Run: py -3.11 -m venv D:\Wren-Companion\.venv ^&^& D:\Wren-Companion\.venv\Scripts\python.exe -m pip install -r D:\Wren-Companion\requirements.txt 1>&2
    endlocal
    exit /b 2
)

REM Skip the singleton lockfile check so we don't refuse-to-start when the
REM original iris_runtime owns state/iris.pid. The original keeps running.
set IRIS_SKIP_INSTANCE_CHECK=1

REM Optional: skip the identity guard (in case test env has IDENTITY.md
REM differences). Comment this line if you WANT to verify the guard fires.
REM set IRIS_SKIP_IDENTITY_GUARD=1

REM Mark this process so logs are distinguishable from the original.
set IRIS_TEST_MODE=1

REM Set a unique IRIS_TEST_ID (timestamp-based) so the test_complete flag
REM file written by the new CC is uniquely-named per test run. Avoids
REM races between concurrent test runs.
REM IMPORTANT: respect inherited IRIS_TEST_ID so the watcher script
REM (restart_test_watch.ps1) and this .bat agree on the flag filename.
REM Original bug 2026-05-19: .bat unconditionally overwrote env var, watcher
REM and .bat diverged by 2s, watcher never found the .bat's flag.
if "%IRIS_TEST_ID%"=="" (
    for /f "tokens=2 delims==" %%I in ('wmic os get LocalDateTime /value') do set DT=%%I
    set IRIS_TEST_ID=%DT:~0,14%
    echo [start_iris_test.bat] IRIS_TEST_ID computed locally: %IRIS_TEST_ID% 1>&2
) else (
    echo [start_iris_test.bat] IRIS_TEST_ID inherited from env: %IRIS_TEST_ID% 1>&2
)
echo [start_iris_test.bat] expect flag file at: D:\Wren-Companion\.tmp\test_complete_%IRIS_TEST_ID%.flag 1>&2

echo [start_iris_test.bat] starting TEST CC session (IRIS_SKIP_INSTANCE_CHECK=1) 1>&2
echo [start_iris_test.bat] original CC will not be affected directly; resource conflicts may surface 1>&2
echo. 1>&2

REM Invoke iris_cold_wake.py same way start_iris.bat does.
"D:\Wren-Companion\.venv\Scripts\python.exe" "D:\Wren-Companion\scripts\iris_cold_wake.py" %1 %2 %3
endlocal
