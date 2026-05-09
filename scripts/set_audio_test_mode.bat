@echo off
REM Set Windows default mic to "CABLE Output" so Claude Code's automated
REM test harness can drive Ava through the virtual cable.
REM
REM See D:\ClaudeCodeMemory\decisions\dual-audio-path.md for the full
REM rationale (pattern b: default-mic toggle).
REM
REM Run set_audio_production_mode.bat to switch back to GAIA HD for
REM Zeke's actual usage.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Import-Module AudioDeviceCmdlets; Set-AudioDevice -Index 13 -ErrorAction Stop; Write-Host '[audio] default mic -> CABLE Output (test mode)'"
