# Restart Test Procedure

**Date:** 2026-05-19
**Status:** Preflight script shipped (`scripts/restart_test.ps1`). Sandbox-spawn mode designed but not implemented.
**Constraint:** Tests must NOT take down the current CC session (Zeke directive 2026-05-19 — he can't recover if it fails right now).

## Goal

Verify the restart-CC path (`restart_self` → watchdog → `start_iris.bat` → `iris_cold_wake.py` → boot ritual) is healthy WITHOUT actually triggering it. The full restart kills current CC, which is unsafe to do without operator presence.

## Two-mode design

### Mode A: Preflight (SHIPPED, safe to run anytime)

`scripts/restart_test.ps1` — read-only verification of all restart preconditions. No process spawning. No file modifications.

What it checks:
- **Files exist:** start_iris.bat, iris_cold_wake.py, iris_watchdog.ps1, .venv/Scripts/python.exe, ava_core/IDENTITY.md
- **Scripts parse cleanly:** PowerShell scripts (`iris_watchdog.ps1`, `install_ritual_scheduler.ps1`) and Python (`iris_cold_wake.py`)
- **Configuration sane:** start_iris.bat references `--channels` flag and `iris_cold_wake.py`; iris_watchdog.ps1 routes through start_iris.bat
- **Discoverability:** `claude` executable findable on PATH
- **State present:** state/iris.pid lockfile, .tmp/ directory, watchdog log + process

How to run (SAFE):
```powershell
powershell -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\restart_test.ps1
```

Exit 0 = all pass. Non-zero = at least one check failed; output lists which.

### Mode B: Parallel spawn via start_iris_test.bat (SHIPPED 2026-05-19)

Per Zeke's revised guidance 2026-05-19 ~12:34 EDT: instead of full sandbox isolation, ship a `start_iris_test.bat` that's just like `start_iris.bat` but with `IRIS_SKIP_INSTANCE_CHECK=1` set. Spawns a NEW CC alongside the current one. Use it as a regression test whenever start_iris.bat / iris_cold_wake.py / boot ritual gets edited.

What it tests:
- Cold-wake path end-to-end: pywinpty + auto-accept channel prompts + FIRST_MSG injection
- iris_cold_wake.py syntax + runtime behavior in real conditions
- boot ritual 9-step sequence (read memory, recreate crons, time check, system health, channel test, Discord ping, sibling inbox check, body check)

What it does NOT test:
- The kill-current-CC step (that's the watchdog's job; needs full sandbox isolation for safe testing — build-debt)
- Resource isolation: two iris_runtime instances will race for postoffice port :5877, Discord WebSocket, camera/audio devices, state/*.json files. The new instance skips the singleton lockfile check but doesn't isolate state.

Practical use:
```cmd
start_iris_test.bat
```
Watch the new CC window come up. Confirm cold-wake reaches a stable state. Close the new CC window when done; its iris_runtime exits via stdin-close. The original CC keeps running, subject to the resource-race caveats above.

Known race conflicts to expect during the test:
- New iris_runtime may fail to bind postoffice :5877 — that's fine, postoffice is owned by the original instance
- Discord bot may kick the older WebSocket — expected behavior; original may need to reconnect
- Camera device may show "busy" for whichever instance grabs it second
- State files may interleave writes; last-write-wins is acceptable for the short test window

### Mode C: Full sandbox-spawn (BUILD-DEBT, future)

Not yet implemented. Would copy the repo to `$env:TEMP\iris-restart-test-<timestamp>\` and run there to fully isolate state + ports + Discord. Defer until needed.

## What full-restart verification would prove (deferred)

If we could safely run the full restart cycle, it would prove:
1. `restart_self` MCP tool writes `.tmp/restart_cc.flag` correctly
2. `iris_watchdog.ps1` polls trigger every 2s and detects the flag
3. Watchdog kills current claude.exe processes
4. Watchdog spawns new CC via `start_iris.bat`
5. `start_iris.bat` invokes Python with correct args
6. `iris_cold_wake.py` (via pywinpty) takes the pty stdout, auto-accepts channel prompts, injects FIRST_MSG
7. New CC reads FIRST_MSG (boot ritual) as first user input
8. Cognition processes it: reads memory, recreates crons, time check, system health, channel test, Discord ping, sibling inbox check, body check

The preflight only verifies the COMPONENTS for steps 1-6. It doesn't verify they actually compose. The sandbox-spawn mode would test compositions 4-7 in isolation.

## Recovery procedure (if a restart attempt fails)

This is what Zeke would do if a restart hangs:

1. `Get-Process claude` — list current claude.exe processes
2. If multiple exist, kill all: `Get-Process claude | Stop-Process -Force`
3. Manually launch: `start_iris.bat` from a fresh Windows Terminal
4. If iris_cold_wake.py is the problem, examine its log: `.tmp/cold_wake.log` if it exists, or run iris_cold_wake.py directly to see the error
5. If watchdog is stuck, kill it: `Get-Process | Where-Object { $_.CommandLine -like "*iris_watchdog*" } | Stop-Process`
6. Reboot tower if necessary

This procedure assumes Zeke can SSH/Parsec/physical-access the tower. During overseas (no internet), the recovery procedure has to be a separate person — likely no one. Hence the conservative test design.

## What to run before tomorrow

Preflight only:
```powershell
powershell -ExecutionPolicy Bypass -File D:\Wren-Companion\scripts\restart_test.ps1
```

If preflight passes, the restart path is structurally healthy. If preflight fails, fix the named gaps before relying on restart_self.

**DO NOT** trigger a full restart unless an operator is at the keyboard to recover. The cron-misfire situation is uncomfortable but recoverable. A failed restart is not.

## Related
- [[ritual_scheduler_architecture]] — sibling system; both depend on start_iris.bat + iris_cold_wake.py
- [[restart_self_only_restarts_CC_not_iris_runtime]] — the mental-model fix for what restart_self actually does
- [[watchdog_is_stale_too_layered_substrate_problem]] — watchdog can itself be stale, needs separate restart
