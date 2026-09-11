param([string]$Mode = "enforce")

# voice_quiet_hours.ps1 - keep Iris's mouth off while Zeke is asleep.
#
# WHY (2026-09-10, Zeke's word): "if you want to, you can go ahead and make something so
# that it automatically turns that off like if you know it's sleeping hours." The problem it
# solves is NOT me choosing to speak - it is the Agent-SDK host's generic "I'm done" cue,
# which auto-speaks after tool-heavy silent turns and which I do NOT control. The 3-hourly
# self-check fires at :23 and is exactly that shape, so without this the mouth would talk
# into a dark room around 23:23 / 02:23 / 05:23.
#
# SHAPE: ONE scheduled task running -Mode enforce every 10 minutes, NOT a start/stop pair.
# Enforce is idempotent and self-correcting: it survives a missed fire, a reboot mid-window,
# and a hand-edit of the hours (the window lives in state\voice_quiet_hours.json, so changing
# bedtime is a JSON edit, never a task re-registration). A start/end pair would fossilize the
# times into the task definitions and drift from the config the moment either changed.
#
# HIS WORD ALWAYS WINS: if voice is already off when the window opens, quiet-hours does not
# claim it (it will not turn ON something it did not turn OFF). If he turns voice back ON
# mid-window, that is a manual override - we stand down until the NEXT night rather than
# fighting him every 10 minutes.
#
# Console flash: run this via scripts\run_hidden.vbs. A bare `powershell -WindowStyle Hidden`
# task action still flashes a console for a frame and steals focus from his full-screen game
# (2026-09-06 scar).

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot
$CfgPath = Join-Path $ROOT "state\voice_quiet_hours.json"
$FLAG = Join-Path $ROOT "state\voice_deliberately_off.json"
$SWITCH = Join-Path $ROOT "scripts\body_switch.ps1"
$LOG = Join-Path $ROOT "logs\voice_quiet_hours.log"

function Log([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    try { Add-Content -Path $LOG -Value $line -Encoding utf8 } catch {}
    Write-Output $line
}

# ---- config ---------------------------------------------------------------
$defaults = [ordered]@{
    enabled         = $true
    start           = "22:30"   # window opens (mouth goes quiet)
    end             = "05:10"   # window closes (mouth comes back) - he is up at 05:17 on course days
    auto_off_active = $false    # true only while THIS script owns the off-state
    override_night  = ""        # yyyy-MM-dd of a night he manually overrode; we stand down for it
    last_action     = ""
    last_action_ts  = ""
}
if (Test-Path $CfgPath) {
    try {
        $loaded = Get-Content $CfgPath -Raw | ConvertFrom-Json
        foreach ($k in @($defaults.Keys)) {
            if ($null -ne $loaded.PSObject.Properties[$k]) { $defaults[$k] = $loaded.$k }
        }
    } catch { Log "config unreadable ($($_.Exception.Message)) - using defaults" }
}
$cfg = $defaults

function Invoke-Switch([string]$switchMode) {
    # DEFENSIVE, not diagnosed: body_switch.ps1 ends its voice branches with `exit 0`, and a
    # child process makes that exit unambiguously its own. I first blamed exit-propagation for
    # the 09-10 "config never saved" bug and wrote that here as fact - it was NOT the cause
    # (see Save-Cfg: $CFG and $cfg were the same variable). I never tested whether `&` actually
    # propagates the exit, so do not treat this comment as evidence that it does.
    $out = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $SWITCH $switchMode
    foreach ($line in $out) { if ("$line".Trim()) { Log "  body_switch: $line" } }
}

function Save-Cfg {
    # NOT Out-File -Encoding utf8NoBOM: that value only exists in PowerShell 7+, and this box
    # runs Windows PowerShell 5.1 where it throws a ValidateSet error (caught in test, 09-10).
    # 5.1's plain `utf8` writes a BOM, which Python's json.load chokes on. .NET's UTF8Encoding
    # with $false is the one spelling that is BOM-free on BOTH versions.
    $json = ($script:cfg | ConvertTo-Json)
    [System.IO.File]::WriteAllText($CfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Log ("  cfg saved -> {0} (auto_off_active={1})" -f $CfgPath, $script:cfg.auto_off_active)
}

# ---- window math (wraps past midnight) ------------------------------------
$now = Get-Date
function ParseHM([string]$hm) {
    $p = $hm.Split(":")
    return [int]$p[0] * 60 + [int]$p[1]
}
$nowMin = $now.Hour * 60 + $now.Minute
$startMin = ParseHM $cfg.start
$endMin = ParseHM $cfg.end
if ($startMin -lt $endMin) {
    $inWindow = ($nowMin -ge $startMin) -and ($nowMin -lt $endMin)
} else {
    # wraps midnight: 22:30 -> 05:10
    $inWindow = ($nowMin -ge $startMin) -or ($nowMin -lt $endMin)
}
# "Which night is this?" - before the end time we still belong to YESTERDAY's night.
if ($nowMin -lt $endMin) { $nightKey = $now.AddDays(-1).ToString("yyyy-MM-dd") }
else { $nightKey = $now.ToString("yyyy-MM-dd") }

$voiceIsOff = Test-Path $FLAG

if ($Mode -eq "status") {
    Log ("status: now={0} window={1}-{2} inWindow={3} voiceIsOff={4} auto_off_active={5} enabled={6} night={7} override_night='{8}'" -f `
        $now.ToString("HH:mm"), $cfg.start, $cfg.end, $inWindow, $voiceIsOff, $cfg.auto_off_active, $cfg.enabled, $nightKey, $cfg.override_night)
    exit 0
}

if (-not $cfg.enabled) { exit 0 }

# ---- enforce --------------------------------------------------------------
if ($inWindow) {

    # He turned voice back ON during OUR quiet window -> stand down for this night.
    if ($cfg.auto_off_active -and (-not $voiceIsOff)) {
        Log "voice was turned back ON inside the quiet window - treating as Zeke's override; standing down until tomorrow night."
        $cfg.auto_off_active = $false
        $cfg.override_night = $nightKey
        $cfg.last_action = "stand_down_override"
        $cfg.last_action_ts = $now.ToString("s")
        Save-Cfg
        exit 0
    }

    if ($cfg.override_night -eq $nightKey) { exit 0 }   # already stood down tonight

    if ($voiceIsOff) {
        # Already off. If we did not do it, do NOT claim it - otherwise the morning pass
        # would turn ON a mouth that Zeke deliberately silenced.
        if (-not $cfg.auto_off_active) { Log "in window; voice already off and NOT by quiet-hours - leaving it alone (will not turn it on at $($cfg.end))." }
        exit 0
    }

    Log "quiet hours OPEN ($($cfg.start)-$($cfg.end)) - taking the mouth down."
    Invoke-Switch "voice_off"
    if (Test-Path $FLAG) {
        $cfg.auto_off_active = $true
        $cfg.last_action = "voice_off"
        $cfg.last_action_ts = $now.ToString("s")
        Save-Cfg
        Log "  verified: off-flag present, mouth is down."
    } else {
        Log "  FAILED: body_switch ran but the off-flag is absent - NOT claiming auto_off_active."
    }
    exit 0

} else {

    if ($cfg.auto_off_active) {
        Log "quiet hours CLOSED - giving the mouth back."
        Invoke-Switch "voice_on"
        if (-not (Test-Path $FLAG)) {
            $cfg.auto_off_active = $false
            $cfg.last_action = "voice_on"
            $cfg.last_action_ts = $now.ToString("s")
            Save-Cfg
            Log "  verified: off-flag gone; the watchdog re-warms the mouth in ~3 min."
        } else {
            Log "  FAILED: off-flag still present after voice_on - leaving auto_off_active set so the next pass retries."
        }
    }
    # Outside the window with auto_off_active false: nothing to do. If voice is off it is
    # because Zeke or something else turned it off, and that is not ours to undo.
    exit 0
}
