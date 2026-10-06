#!/usr/bin/env bash
# ~/iris_console.sh — the tower app's "Open my console" button (Zeke 2026-10-06) lands here over SSH.
# Attach to my live session (tmux "iris", started by ~/iris_start.sh); if I'm not running here, say so
# plainly instead of dropping him into an empty shell.
if tmux has-session -t iris 2>/dev/null; then
  exec tmux attach -t iris
fi
cat <<MSG

  Iris isn't running on the server right now.
  Until the cutover she lives on the tower; after it, start her with the
  server buttons (or:  ~/iris_start.sh opus ) and this window will show her.

MSG
read -r -p "  Press Enter to close. " _ 2>/dev/null || true
