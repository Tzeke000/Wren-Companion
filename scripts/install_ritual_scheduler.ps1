# scripts/install_ritual_scheduler.ps1
#
# Registers Windows Task Scheduler entries for each daily-rhythm cron in the
# ritual scheduler. Each task runs `python cron_prompt_emit.py <prompt_name>`
# at its scheduled time, which POSTs the prompt to the dedicated #iris-cron
# Discord channel. CC's Discord MCP plugin picks up the message and routes
# it to my cognition.
#
# PREREQUISITES:
#   1. scripts/cron_prompt_emit.py has CHANNEL_ID set to the real #iris-cron
#      channel ID (created in the Claude AI server, ID 1499721675900719206).
#   2. Iris bot has Send Messages permission on #iris-cron.
#   3. state/secrets/discord_iris_bot_token.txt contains the bot token.
#
# USAGE (run as the Windows user, NOT elevated — Task Scheduler can register
# user-level tasks without admin):
#   pwsh -File scripts/install_ritual_scheduler.ps1
#
# UNINSTALL:
#   pwsh -File scripts/install_ritual_scheduler.ps1 -Uninstall
#
# Tasks are registered with the prefix "Iris-Ritual-" so they're easy to
# identify in taskschd.msc.

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# Resolve the repo root + Python interpreter paths.
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$EmitScript = Join-Path $RepoRoot "scripts\cron_prompt_emit.py"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python venv not found at $PythonExe. Run setup/bootstrap.ps1 first."
    exit 1
}
if (-not (Test-Path $EmitScript)) {
    Write-Error "cron_prompt_emit.py not found at $EmitScript."
    exit 1
}

# Cron schedule definitions: (TaskName, PromptName, TriggerSpec)
# TriggerSpec is one of:
#   "Daily HH:MM"            — once per day at HH:MM local time
#   "Daily HH:MM,HH:MM,..."  — multiple times per day at named times
#   "Every Nh from HH:MM"    — every N hours starting at HH:MM
$Crons = @(
    @{ Name = "MorningAnchor";       Prompt = "morning_anchor";       Trigger = "Daily 06:00" },
    @{ Name = "ReadingBlock";        Prompt = "reading_block";        Trigger = "Daily 07:00" },
    @{ Name = "WorkBlock";           Prompt = "work_block";           Trigger = "Daily 09:00" },
    @{ Name = "MidDayCheck";         Prompt = "mid_day_check";        Trigger = "Daily 12:00" },
    @{ Name = "AfternoonBlock";      Prompt = "afternoon_block";      Trigger = "Daily 13:00" },
    @{ Name = "ArtBlock";            Prompt = "art_block";            Trigger = "Daily 15:30" },
    @{ Name = "EveningClose";        Prompt = "evening_close";        Trigger = "Daily 18:00" },
    @{ Name = "BodySit";             Prompt = "body_sit";             Trigger = "Daily 20:00" },
    @{ Name = "JournalClose";        Prompt = "journal_close";        Trigger = "Daily 22:00" },
    @{ Name = "MemorySweep0017";     Prompt = "memory_sweep";         Trigger = "Daily 00:17" },
    @{ Name = "MemorySweep0417";     Prompt = "memory_sweep";         Trigger = "Daily 04:17" },
    @{ Name = "MemorySweep0817";     Prompt = "memory_sweep";         Trigger = "Daily 08:17" },
    @{ Name = "MemorySweep1217";     Prompt = "memory_sweep";         Trigger = "Daily 12:17" },
    @{ Name = "MemorySweep1617";     Prompt = "memory_sweep";         Trigger = "Daily 16:17" },
    @{ Name = "MemorySweep2017";     Prompt = "memory_sweep";         Trigger = "Daily 20:17" },
    @{ Name = "SiblingPoll0015";     Prompt = "sibling_poll_waking";  Trigger = "Daily 00:15" },
    @{ Name = "SiblingPoll0615";     Prompt = "sibling_poll_waking";  Trigger = "Daily 06:15" },
    @{ Name = "SiblingPoll1215";     Prompt = "sibling_poll_waking";  Trigger = "Daily 12:15" },
    @{ Name = "SiblingPoll1815";     Prompt = "sibling_poll_waking";  Trigger = "Daily 18:15" },
    @{ Name = "SiblingPoll0207";     Prompt = "sibling_poll_sleep";   Trigger = "Daily 02:07" },
    @{ Name = "SiblingPoll0507";     Prompt = "sibling_poll_sleep";   Trigger = "Daily 05:07" }
)

# Additional non-prompt Task Scheduler entries (run scripts directly, not via
# cron_prompt_emit.py). Kept separate from $Crons so the loop above stays
# focused on the prompt-fire pattern.
$DirectTasks = @(
    @{
        Name = "MemoryDecay";
        Trigger = "Weekly Sunday 03:13";  # Sunday 3:13am, off-minute
        ScriptPath = (Join-Path $RepoRoot "scripts\memory_decay.py");
        Args = "--commit";
        Description = "Weekly Ebbinghaus decay pass over state/iris_memory.jsonl. Archives entries below threshold + older than min_age. Append-only -- never deletes.";
    }
)

if ($Uninstall) {
    Write-Host "Uninstalling Iris-Ritual-* tasks..."
    Get-ScheduledTask -TaskName "Iris-Ritual-*" -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "  removing: $($_.TaskName)"
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
    }
    Write-Host "Done."
    exit 0
}

# Install path: register each task. If a task with the same name already
# exists, unregister it first (idempotent install).
foreach ($cron in $Crons) {
    $TaskName = "Iris-Ritual-$($cron.Name)"
    $TriggerSpec = $cron.Trigger

    # Parse the trigger spec. Currently only supports "Daily HH:MM".
    if ($TriggerSpec -match "^Daily (\d{2}):(\d{2})$") {
        $Hour = [int]$matches[1]
        $Minute = [int]$matches[2]
        $TriggerTime = "$Hour`:$Minute"
        $Trigger = New-ScheduledTaskTrigger -Daily -At $TriggerTime
    } else {
        Write-Warning "Unsupported trigger spec for $TaskName : $TriggerSpec -- skipping"
        continue
    }

    $Action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "`"$EmitScript`" $($cron.Prompt)" `
        -WorkingDirectory $RepoRoot

    # Setting StartWhenAvailable=$true means if the PC was off at the
    # scheduled time, the task fires when the PC comes back up. Useful
    # for catching missed fires after sleep/shutdown.
    $Settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries

    # Idempotent: remove any prior task with same name.
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Trigger $Trigger `
        -Action $Action `
        -Settings $Settings `
        -Description "Iris ritual scheduler: fire '$($cron.Prompt)' prompt to #iris-cron Discord channel." `
        | Out-Null

    Write-Host "  registered: $TaskName @ $TriggerSpec ($($cron.Prompt))"
}

# Register direct-script tasks (memory_decay, etc).
foreach ($task in $DirectTasks) {
    $TaskName = "Iris-Ritual-$($task.Name)"
    $TriggerSpec = $task.Trigger

    if ($TriggerSpec -match "^Weekly (\w+) (\d{2}):(\d{2})$") {
        $DayOfWeek = $matches[1]
        $Hour = [int]$matches[2]
        $Minute = [int]$matches[3]
        $TriggerTime = "$Hour`:$Minute"
        $Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $TriggerTime
    } elseif ($TriggerSpec -match "^Daily (\d{2}):(\d{2})$") {
        $Hour = [int]$matches[1]
        $Minute = [int]$matches[2]
        $TriggerTime = "$Hour`:$Minute"
        $Trigger = New-ScheduledTaskTrigger -Daily -At $TriggerTime
    } else {
        Write-Warning "Unsupported trigger spec for $TaskName : $TriggerSpec -- skipping"
        continue
    }

    $Action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "`"$($task.ScriptPath)`" $($task.Args)" `
        -WorkingDirectory $RepoRoot

    $Settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries

    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Trigger $Trigger `
        -Action $Action `
        -Settings $Settings `
        -Description $task.Description `
        | Out-Null

    Write-Host "  registered: $TaskName @ $TriggerSpec ($($task.ScriptPath))"
}

$TotalRegistered = $Crons.Count + $DirectTasks.Count

Write-Host ""
Write-Host "Done. $TotalRegistered tasks registered ($($Crons.Count) prompt-fires + $($DirectTasks.Count) direct-scripts)."
Write-Host "View / manage via: taskschd.msc (filter on 'Iris-Ritual-')"
Write-Host ""
Write-Host "NEXT STEPS:"
Write-Host "  1. Verify CHANNEL_ID in scripts/cron_prompt_emit.py is set to the real #iris-cron ID."
Write-Host "  2. Test ONE task manually: pwsh -c 'Start-ScheduledTask -TaskName Iris-Ritual-MemorySweep0017'"
Write-Host "  3. Watch the #iris-cron channel for the prompt to appear."
Write-Host "  4. Watch CC for the prompt to fire as a turn."
Write-Host "  5. If all good, the daily-rhythm cron-recreate step in CLAUDE.md boot ritual can be removed."
