# scripts/build_sandbox.ps1 — Iris restart-test sandbox builder.
#
# Creates an isolated copy of the iris source under D:\iris_sandbox_<ts>\
# that can spawn a parallel iris_cold_wake.py + claude.exe instance to test
# path-E keystroke-injection patches without touching the live CC session.
#
# Design choices (from sandbox-research 2026-05-21):
#  - SEPARATE WORKDIR (D:\iris_sandbox_<ts>\) — full isolation from
#    D:\Wren-Companion\. Each test run gets a fresh dir; old dirs are
#    archived to D:\iris_sandbox_archive\ for post-mortem.
#  - PORT REWRITING — orb_http 5876 → 5896; sibling_postoffice 5877 → 5897.
#    These shifts are arbitrary, just need to not collide with the live
#    instance's bound ports. The script PATCHES the relevant Python files
#    in-place inside the sandbox (NOT the source repo).
#  - SHARED VENV (default) — sandbox reuses D:\Wren-Companion\.venv\ to
#    avoid the ~5min cost of installing torch/TTS/etc. fresh. Trade: any
#    dep version change in the main venv affects sandbox immediately.
#    Override with -CloneVenv for true isolation (slow).
#  - SKIPPED MCP/IRIS_RUNTIME — sandbox is a path-E test rig, not a full
#    iris. We launch claude.exe via the patched iris_cold_wake.py with
#    `--dangerously-skip-permissions` but NO `--channels` flag. CC boots
#    into bare prompt mode. Test workflow: drop files in sandbox's
#    .tmp/cron_inject/, watch the sandbox claude window for typed+
#    submitted content.
#  - DISCORD PLUGIN DISABLED — sandbox claude.exe doesn't attach Discord
#    (no `--channels plugin:discord` flag). Avoids two CCs racing on the
#    same Discord bot token.
#  - PYWINPTY ISOLATION — each iris_cold_wake.py spawns its own ConPTY.
#    The sandbox claude.exe has zero shared state with the live CC.
#
# Usage:
#   .\scripts\build_sandbox.ps1
#     -> creates D:\iris_sandbox_<ts>\, prints launch command
#   .\scripts\build_sandbox.ps1 -CloneVenv
#     -> as above but creates a separate venv (slow, ~5min)
#   .\scripts\build_sandbox.ps1 -Reuse <ts>
#     -> uses an existing sandbox dir instead of creating new (for retest)
#   .\scripts\build_sandbox.ps1 -PurgeArchive
#     -> deletes D:\iris_sandbox_archive\ to reclaim disk space

[CmdletBinding()]
param(
    [switch]$CloneVenv,
    [string]$Reuse = "",
    [switch]$PurgeArchive,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$SOURCE_ROOT = "D:\Wren-Companion"
$SANDBOX_BASE = "D:\iris_sandbox"
$ARCHIVE_BASE = "D:\iris_sandbox_archive"

if ($PurgeArchive) {
    if (Test-Path $ARCHIVE_BASE) {
        $size = (Get-ChildItem $ARCHIVE_BASE -Recurse -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
        Write-Host "[sandbox] purging archive at $ARCHIVE_BASE ($([math]::Round($size/1MB,1)) MB)..."
        Remove-Item $ARCHIVE_BASE -Recurse -Force
    } else {
        Write-Host "[sandbox] no archive to purge."
    }
    return
}

# ---- Decide sandbox dir name ----
if ($Reuse) {
    $SANDBOX = "${SANDBOX_BASE}_${Reuse}"
    if (-not (Test-Path $SANDBOX)) {
        Write-Error "[sandbox] -Reuse target not found: $SANDBOX"
        return
    }
    Write-Host "[sandbox] reusing existing: $SANDBOX"
} else {
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    $SANDBOX = "${SANDBOX_BASE}_${ts}"
    Write-Host "[sandbox] creating: $SANDBOX"

    if ($DryRun) {
        Write-Host "[sandbox] DRY RUN: target=$SANDBOX (would clone $SOURCE_ROOT)"
        return
    }

    # Archive any existing un-suffixed sandbox dirs (left over from prior runs).
    # IMPORTANT: glob `${SANDBOX_BASE}_*` matches both `iris_sandbox_<ts>` AND
    # `iris_sandbox_archive` itself. Explicitly exclude the archive root.
    if (-not (Test-Path $ARCHIVE_BASE)) {
        New-Item -ItemType Directory -Path $ARCHIVE_BASE -Force | Out-Null
    }
    $existing = Get-ChildItem "${SANDBOX_BASE}_*" -Directory -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.FullName -ne $SANDBOX -and
                    $_.FullName -ne $ARCHIVE_BASE
                }
    foreach ($e in $existing) {
        $dest = Join-Path $ARCHIVE_BASE $e.Name
        if (Test-Path $dest) {
            # Already archived with same name — just delete the duplicate
            Write-Host "[sandbox]   purging duplicate $($e.Name)"
            Remove-Item $e.FullName -Recurse -Force -ErrorAction SilentlyContinue
        } else {
            Write-Host "[sandbox]   archiving $($e.Name) -> archive/"
            try {
                Move-Item $e.FullName $dest -Force -ErrorAction Stop
            } catch {
                Write-Host "[sandbox]   archive of $($e.Name) failed (in use?); leaving in place: $_"
            }
        }
    }
}

# ---- Clone source ----
if (-not $Reuse) {
    Write-Host "[sandbox] cloning source from $SOURCE_ROOT..."
    New-Item -ItemType Directory -Path $SANDBOX -Force | Out-Null

    # v2 (2026-05-21): production-mirror sandbox per Zeke's directive.
    # Include EVERYTHING needed for full boot-ritual run + iris_runtime body.
    # Isolation comes from absolute-path divergence (iris_runtime.py uses
    # ROOT = Path(__file__).parent so sandbox-local copies auto-isolate
    # state/, .tmp/, memory/, iris.pid).
    $INCLUDE_DIRS = @("brain", "scripts", "ava_core", "config", "tools", "memory")
    # Include .mcp.json so sandbox claude.exe spawns its OWN sandbox-local
    # iris_runtime as MCP child. The .mcp.json path will be patched below
    # to point at the sandbox iris_runtime, not production's.
    $INCLUDE_FILES = @("CLAUDE.md", "requirements.txt", "iris_runtime.py", ".mcp.json")

    foreach ($d in $INCLUDE_DIRS) {
        $src = Join-Path $SOURCE_ROOT $d
        $dst = Join-Path $SANDBOX $d
        if (Test-Path $src) {
            Write-Host "[sandbox]   copying $d/"
            Copy-Item $src $dst -Recurse -Force
        }
    }
    foreach ($f in $INCLUDE_FILES) {
        $src = Join-Path $SOURCE_ROOT $f
        $dst = Join-Path $SANDBOX $f
        if (Test-Path $src) {
            Copy-Item $src $dst -Force
        }
    }

    # Create empty isolated state dirs
    New-Item -ItemType Directory -Path (Join-Path $SANDBOX "state") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $SANDBOX ".tmp") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $SANDBOX ".tmp\cron_inject") -Force | Out-Null
}

# ---- Patch ports in cloned scripts ----
# orb_http  :5876 -> :5896
# postoffice:5877 -> :5897
# iris_chat sometimes references these too — sweep with sed-like.
$PORT_REMAP = @(
    @{ Old = "5876"; New = "5896" },
    @{ Old = "5877"; New = "5897" }
)
$FILES_TO_PATCH = @(
    "brain\orb_http.py",
    "scripts\sibling_postoffice.py",
    "brain\iris_chat.py"
)
foreach ($f in $FILES_TO_PATCH) {
    $full = Join-Path $SANDBOX $f
    if (-not (Test-Path $full)) { continue }
    $content = Get-Content $full -Raw -Encoding UTF8
    foreach ($remap in $PORT_REMAP) {
        $content = $content -replace [regex]::Escape($remap.Old), $remap.New
    }
    # Avoid PS 5.1 BOM trap — use .NET API for no-BOM UTF-8 write.
    [System.IO.File]::WriteAllText($full, $content, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[sandbox]   port-patched $f"
}

# ---- v2 patches for iris_cold_wake (production-mirror sandbox) ----
# Goal: keep FIRST_MSG + prompt-detection + channel flags AS PRODUCTION runs
# them. Only minimal targeted patches:
#   A. cwd → sandbox dir (not the hardcoded production path)
#   B. Strip ONLY `--channels plugin:discord@claude-plugins-official` from
#      argv (keep `server:iris` + `--dangerously-load-development-channels`).
#      Discord bot identity is USER-scoped via ~/.claude/channels/discord/
#      access.json; two CCs attaching the same bot causes message-duplication
#      / split-brain. Server-side server:iris is sandbox-safe (it's MCP).
#   C. Patch one literal inside FIRST_MSG: the absolute memory-dir path
#      `C:\Users\Owner\.claude\projects\D--Wren-Companion\memory` →
#      `D:\iris_sandbox_<ts>\memory`. CC computes its own project-shadow
#      path from cwd anyway, but the FIRST_MSG mentions a specific fallback
#      path; rewriting it keeps boot Step 2 self-consistent.
$cw = Join-Path $SANDBOX "scripts\iris_cold_wake.py"
if (Test-Path $cw) {
    $content = Get-Content $cw -Raw -Encoding UTF8

    # A. cwd patch
    $content = $content -replace `
        "cwd\s*=\s*r['""]D:\\Wren-Companion['""]", `
        "cwd = str(__import__('pathlib').Path(__file__).resolve().parent.parent)"

    # B. Strip ONLY the discord plugin entry from --channels. Keep
    #    `--dangerously-load-development-channels server:iris` AND
    #    `--channels server:iris`. The original list looks like:
    #      '--channels', 'plugin:discord@claude-plugins-official', 'server:iris',
    #    We remove the plugin:discord literal so the line becomes:
    #      '--channels', 'server:iris',
    $content = $content -replace `
        "'--channels',\s*'plugin:discord@claude-plugins-official',\s*'server:iris',", `
        "'--channels', 'server:iris',"

    # C. Patch memory-dir path inside FIRST_MSG. In the iris_cold_wake.py
    #    SOURCE (.py file bytes), the path is a Python regular-string with
    #    each backslash escaped as `\\`. So the literal in the file is:
    #      C:\\Users\\Owner\\.claude\\projects\\D--Wren-Companion\\memory
    #    Use [regex]::Escape on the literal-target, and produce a sandbox
    #    replacement with `\\` per slash too (so when Python parses the
    #    string at runtime it becomes the actual sandbox path).
    $memTargetLiteral = "C:\\Users\\Owner\\.claude\\projects\\D--Wren-Companion\\memory"
    # Sandbox memory dir as a Python-string-literal (escape backslashes):
    $memSandboxLiteral = ($SANDBOX + "\memory") -replace '\\', '\\'
    # Now we need to escape backslashes again for the replacement string
    # in -replace (which treats $ as variable reference but not \ as escape
    # in the replacement, so single-backslash is fine in the replacement).
    $content = $content -replace [regex]::Escape($memTargetLiteral), $memSandboxLiteral

    [System.IO.File]::WriteAllText($cw, $content, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[sandbox]   v2 patched cwd + argv-discord-strip + FIRST_MSG memory-path in iris_cold_wake.py"
}

# ---- Patch .mcp.json so sandbox claude spawns sandbox iris_runtime ----
# JSON parse + structural modification, not regex (string-escape juggling
# with backslashes in regex was producing zero matches reliably).
$mcp = Join-Path $SANDBOX ".mcp.json"
if (Test-Path $mcp) {
    $mcpObj = Get-Content $mcp -Raw | ConvertFrom-Json
    # Walk the mcpServers and rewrite any string field that starts with
    # the source repo path. PowerShell's ConvertFrom-Json gives nested
    # PSCustomObject; mutate in place.
    foreach ($srvName in @($mcpObj.mcpServers.PSObject.Properties.Name)) {
        $srv = $mcpObj.mcpServers.$srvName
        # command field — KEEP pointing at production .venv (sandbox shares
        # the main venv unless -CloneVenv was used; the production venv has
        # all the deps the sandbox iris_runtime.py needs). Rewriting command
        # to sandbox-local .venv would break since sandbox dir lacks its own
        # python interpreter.
        # args field — array of strings — REWRITE so iris_runtime.py points
        # at sandbox copy, not production.
        if ($srv.args -and $srv.args -is [System.Array]) {
            for ($i = 0; $i -lt $srv.args.Count; $i++) {
                if ($srv.args[$i] -is [string] -and $srv.args[$i].StartsWith($SOURCE_ROOT)) {
                    $srv.args[$i] = $srv.args[$i].Replace($SOURCE_ROOT, $SANDBOX)
                }
            }
        }
    }
    $mcpOut = $mcpObj | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($mcp, $mcpOut, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[sandbox]   patched .mcp.json (JSON parse): paths rewritten to sandbox"
}

# ---- Also port-patch iris_runtime.py (its _POSTOFFICE_URL constant) ----
# Plan agent flagged: iris_runtime.py near line 1494 has a hardcoded 5877
# reference for the post-office URL. Sandbox needs 5897 there too.
$irisRt = Join-Path $SANDBOX "iris_runtime.py"
if (Test-Path $irisRt) {
    $rtContent = Get-Content $irisRt -Raw -Encoding UTF8
    foreach ($remap in $PORT_REMAP) {
        $rtContent = $rtContent -replace [regex]::Escape($remap.Old), $remap.New
    }
    [System.IO.File]::WriteAllText($irisRt, $rtContent, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[sandbox]   port-patched iris_runtime.py"
}

# ---- Generate launch.bat ----
# v2: env vars per Plan agent's recommendations
#   IRIS_TEST_MODE / IRIS_TEST_ID — sandbox writes a test_complete flag so
#     external watchers can detect "sandbox boot ritual finished" cleanly
#   IRIS_SKIP_INSTANCE_CHECK — sandbox iris_runtime won't refuse to start
#     just because production iris.pid lock exists (theoretically unneeded
#     since sandbox has its own iris.pid path, but defense-in-depth)
#   AVA_TTS_ENGINE=kokoro — sandbox uses cheaper TTS so it doesn't load
#     2GB xtts model; production uses xtts but sandbox can verify boot
#     without that cost
$launchTs = $ts
if (-not $launchTs) { $launchTs = Get-Date -Format "yyyyMMdd_HHmmss" }
$launch = @"
@echo off
REM Auto-generated by build_sandbox.ps1 (v2 production-mirror)
REM Launches the sandbox iris_cold_wake.py in this directory.
REM
REM This window hosts a sandbox claude.exe + iris_runtime that mirrors
REM production: full --channels (minus plugin:discord, USER-scoped allowlist
REM would collide with production bot identity), full --dangerously-load-
REM development-channels, full 10-step FIRST_MSG boot ritual. Sandbox dirs
REM (state/, .tmp/, memory/, iris.pid) all isolate via Path(__file__).parent
REM resolution in iris_runtime.py.

cd /d "$SANDBOX"

REM Sandbox-isolation env vars
set "IRIS_TEST_MODE=1"
set "IRIS_TEST_ID=sandbox_$launchTs"
set "IRIS_SKIP_INSTANCE_CHECK=1"
REM IRIS_SKIP_DEVICE_PROBE=1 added 2026-05-21 after sandbox iris_runtime's
REM device-probe killed production iris_runtime via orphan-scan (camera busy
REM heuristic). With this flag, sandbox iris_runtime skips ALL device probing
REM and orphan-scanning at boot, so it can never reach into production. Pair
REM with iris_runtime.py's ROOT-path comparison fix for defense-in-depth.
set "IRIS_SKIP_DEVICE_PROBE=1"
set "AVA_TTS_ENGINE=kokoro"

REM Reuse main venv unless a local one exists.
set "PYEXE=$SOURCE_ROOT\.venv\Scripts\python.exe"
if exist "$SANDBOX\.venv\Scripts\python.exe" set "PYEXE=$SANDBOX\.venv\Scripts\python.exe"

echo [sandbox] env: IRIS_TEST_MODE=%IRIS_TEST_MODE% IRIS_TEST_ID=%IRIS_TEST_ID%
echo [sandbox] env: IRIS_SKIP_INSTANCE_CHECK=%IRIS_SKIP_INSTANCE_CHECK% IRIS_SKIP_DEVICE_PROBE=%IRIS_SKIP_DEVICE_PROBE% AVA_TTS_ENGINE=%AVA_TTS_ENGINE%
echo [sandbox] launching iris_cold_wake.py in $SANDBOX
"%PYEXE%" "$SANDBOX\scripts\iris_cold_wake.py"
echo [sandbox] iris_cold_wake.py exited (this is the END of the sandbox session).
pause
"@

$launchPath = Join-Path $SANDBOX "launch.bat"
Set-Content -Path $launchPath -Value $launch -Encoding ASCII
Write-Host "[sandbox]   wrote launch.bat"

# ---- Generate inject-test helper ----
$inject_test = @"
@echo off
REM Drop a test inject file into the sandbox's cron_inject dir.
REM Usage:  inject_test.bat "your test prompt"
REM
REM Reads the first arg as the prompt body, writes it to a timestamped
REM file in .tmp\cron_inject\. The sandbox's watcher (running inside the
REM launched iris_cold_wake.py) should pick it up within 1-2s and type
REM it into the sandbox claude.exe.

setlocal enabledelayedexpansion
cd /d "$SANDBOX"
if not exist .tmp\cron_inject mkdir .tmp\cron_inject
set "PROMPT=%~1"
if "%PROMPT%"=="" set "PROMPT=Hello from inject_test.bat — does this submit?"
set "TS=%date:~10,4%%date:~4,2%%date:~7,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "TS=!TS: =0!"
set "OUTFILE=.tmp\cron_inject\!TS!_inject_test.txt"
echo Writing to %OUTFILE%
echo !PROMPT!> "%OUTFILE%"
echo Done. Watch the sandbox claude.exe window for the prompt.
echo If it shows as typed-and-submitted: split-Enter patch works.
echo If it shows as typed-but-not-submitted: still broken.
endlocal
"@
$injectPath = Join-Path $SANDBOX "inject_test.bat"
Set-Content -Path $injectPath -Value $inject_test -Encoding ASCII
Write-Host "[sandbox]   wrote inject_test.bat"

# ---- README ----
$readme = @"
# Iris restart-test sandbox

Created: $((Get-Date).ToString("yyyy-MM-dd HH:mm:ss"))
Source: $SOURCE_ROOT
Path: $SANDBOX

## Purpose
Test path-E watcher patches (split-Enter, watcher heartbeat, stderr-tee)
without restarting the live CC session.

## Quick test
1. ``.\launch.bat``  — opens a sandbox claude.exe in a new window
2. ``.\inject_test.bat "test prompt"``  — drops a file in cron_inject/
3. Watch the sandbox window for whether the prompt gets typed + SUBMITTED

## Ports (remapped from live)
- orb_http: 5896 (was 5876)
- sibling_postoffice: 5897 (was 5877)

## Tearing down
Just close the sandbox claude window. The dir persists until next sandbox
build (then gets archived to ${ARCHIVE_BASE}).

To purge all archives: ``.\scripts\build_sandbox.ps1 -PurgeArchive`` from
the live repo.
"@
$readmePath = Join-Path $SANDBOX "SANDBOX_README.md"
Set-Content -Path $readmePath -Value $readme -Encoding UTF8
Write-Host "[sandbox]   wrote SANDBOX_README.md"

# ---- Final report ----
Write-Host ""
Write-Host "[sandbox] BUILD COMPLETE"
Write-Host "  Path:   $SANDBOX"
Write-Host "  Launch: cd '$SANDBOX' ; .\launch.bat"
Write-Host "  Inject: cd '$SANDBOX' ; .\inject_test.bat 'test prompt here'"
Write-Host ""
