#!/usr/bin/env bash
# scripts/server/iris_start_detached.sh <cli|opus|fable> - start me ON THE SERVER from the tower
# app's buttons (Zeke 2026-10-07: "so that I won't even have to log into Zorin").
# 1) already running here? say so.  2) ONE-OF-ME gate in check-only mode; a refusal is
# returned as plain text for the app to show.  3) cleared -> supervised, in tmux "iris"
# (what the app's "Open my console" attaches to). Never starts anything the gate refused.
set -u
MODE="${1:-opus}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
case "$MODE" in cli|opus|fable) ;; *) echo "unknown mode: $MODE"; exit 2 ;; esac
if tmux has-session -t iris 2>/dev/null; then
  echo "I'm already running on the server. Use \"Open my console\" to see me."
  exit 10
fi
OUT="$(IRIS_GATE_CHECK_ONLY=1 IRIS_NO_PAUSE=1 TMUX=gate-check "$HOME/iris_start.sh" "$MODE" 2>&1)"
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "$OUT" | grep -v '^== ' | sed '/^[[:space:]]*$/d'
  exit "$RC"
fi
tmux new-session -d -s iris "$ROOT/scripts/server/iris_supervise.sh $MODE"
echo "Starting me on the server ($MODE). Give me a minute, then press \"Open my console\"."
