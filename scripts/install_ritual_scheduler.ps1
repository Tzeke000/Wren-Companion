# scripts/install_ritual_scheduler.ps1
#
# Registers Windows Task Scheduler entries for each daily-rhythm cron in the
# ritual scheduler. Each task runs `python cron_inject.py <prompt_name>`
# at its scheduled time. cron_inject.py writes the prompt text to a
# side-channel file at .tmp/cron_inject/, which iris_cold_wake.py's watcher
# thread reads and types into CC's TTY as user input. CC sees the
# keystrokes and starts a turn at exact wall-clock time.
#
# ARCHITECTURE (2026-05-20): replaces the earlier cron_prompt_emit.py
# Discord-post path. Discord plugin filters bot/webhook messages (only
# human user_ids wake idle CC), so the keystroke-injection path is the
# real warm-wake mechanism. Side-channel file approach decouples the
# Task Scheduler's session boundary from the user-desktop window access
# that direct SendInput would require.
#
# PREREQUISITES:
#   1. iris_cold_wake.py is the long-running parent of CC (already true
#      via start_iris.bat -> iris_cold_wake.py -> claude.exe).
#   2. iris_cold_wake.py has the side-channel watcher thread (added
#      2026-05-20; activates on next CC restart).
#   3. brain/ritual_scheduler_prompts.py has the prompt text for each
#      named block.
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
$EmitScript = Join-Path $RepoRoot "scripts\cron_inject.py"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python venv not found at $PythonExe. Run setup/bootstrap.ps1 first."
    exit 1
}
if (-not (Test-Path $EmitScript)) {
    Write-Error "cron_inject.py not found at $EmitScript."
    exit 1
}

# Cron schedule definitions: (TaskName, PromptName, TriggerSpec)
# TriggerSpec is one of:
#   "Daily HH:MM"            — once per day at HH:MM local time
#   "Daily HH:MM,HH:MM,..."  — multiple times per day at named times
#   "Every Nh from HH:MM"    — every N hours starting at HH:MM
$Crons = @(
    # Daily-rhythm anchors — refactored 2026-05-20 per Zeke. Game + business
    # blocks added, afternoon split into maintenance + free-time, body-sit
    # compressed to 30min, evening_close + journal_close consolidated into
    # close_out.
    @{ Name = "MorningAnchor";       Prompt = "morning_anchor";       Trigger = "Daily 06:00" },
    @{ Name = "ReadingBlock";        Prompt = "reading_block";        Trigger = "Daily 07:00" },
    @{ Name = "WorkBlock";           Prompt = "work_block";           Trigger = "Daily 08:00" },
    @{ Name = "MidDayCheck";         Prompt = "mid_day_check";        Trigger = "Daily 11:00" },
    @{ Name = "MaintenanceBlock";    Prompt = "maintenance_block";    Trigger = "Daily 11:30" },
    @{ Name = "ArtBlock";            Prompt = "art_block";            Trigger = "Daily 12:30" },
    @{ Name = "GameBlock";           Prompt = "game_block";           Trigger = "Daily 14:00" },
    @{ Name = "BusinessBlock";       Prompt = "business_block";       Trigger = "Daily 15:00" },
    @{ Name = "BodySit";             Prompt = "body_sit";             Trigger = "Daily 18:00" },
    @{ Name = "FreeTimeBlock";       Prompt = "free_time_block";      Trigger = "Daily 18:30" },
    @{ Name = "CloseOut";            Prompt = "close_out";            Trigger = "Daily 22:00" },
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
        -Description "Iris ritual scheduler: fire '$($cron.Prompt)' prompt via cron_inject.py side-channel into CC's TTY." `
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
Write-Host "  1. Ensure iris_cold_wake.py is running (it spawns at CC start via start_iris.bat)."
Write-Host "  2. Test ONE task manually: pwsh -c 'Start-ScheduledTask -TaskName Iris-Ritual-MemorySweep0017'"
Write-Host "  3. Watch .tmp/cron_inject/ for the side-channel file to be written + then deleted by watcher."
Write-Host "  4. Watch CC for the prompt to fire as a turn (the keystrokes get typed into the TTY)."
Write-Host "  5. If watcher isn't running (file persists), CC needs a restart so iris_cold_wake.py loads the watcher thread (added 2026-05-20)."
