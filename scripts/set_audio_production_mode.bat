@echo off
REM Set Windows default mic to GAIA HD so Zeke can speak to Ava with his
REM real voice. Inverse of set_audio_test_mode.bat.
REM
REM See D:\ClaudeCodeMemory\decisions\dual-audio-path.md for the full
REM rationale (pattern b: default-mic toggle).

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Import-Module AudioDeviceCmdlets; Set-AudioDevice -Index 15 -ErrorAction Stop; Write-Host '[audio] default mic -> GAIA HD (production mode)'"
