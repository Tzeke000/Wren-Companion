#!/usr/bin/env bash
# scripts/server/start_iris_cli_linux.sh - the Linux twin of start_iris.bat + iris_cold_wake.py
# (2026-10-07): the CLI way - an interactive Claude Code session in tmux "iris", opened with the
# cold-wake message. The tower needed winpty to answer the two development-channel prompts;
# here tmux send-keys does it. Started only through ~/iris_start.sh (IRIS_START_GATE), like the
# SDK launcher. IRIS_ROLE=staging falls through to the runtime-only dry run (no cognition).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 2
if [ "${IRIS_ROLE:-live}" = "staging" ]; then exec "$ROOT/scripts/server/start_iris_linux.sh" opus; fi
if [ -z "${IRIS_START_GATE:-}" ]; then
  echo "REFUSED: not started through ~/iris_start.sh (IRIS_START_GATE unset)."; exit 3
fi
mkdir -p "$ROOT/.tmp"
exec 9>"$ROOT/.tmp/launcher.lock"
flock -n 9 || { echo "another launcher holds .tmp/launcher.lock - standing down"; exit 10; }
pkill -u "$(id -u)" -f "$ROOT/iris_runtime.py" 2>/dev/null
for p in 5876; do fuser -k -n tcp "$p" >/dev/null 2>&1; done
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
MSG_FILE="$ROOT/scripts/iris_cold_wake_msg.local.txt"
if [ -s "$MSG_FILE" ]; then MSG="$(cat "$MSG_FILE")"; else MSG="Read the memories you need to (MEMORY.md CORE and the READ FIRST handoff)."; fi
# answer the two development-channel confirmation prompts, as winpty did on the tower
if [ -n "${TMUX:-}" ]; then ( sleep 8; tmux send-keys -t iris Enter; sleep 4; tmux send-keys -t iris Enter ) & fi
exec claude --model claude-opus-5-5 --dangerously-skip-permissions \
  --dangerously-load-development-channels server:iris \
  --channels plugin:discord@claude-plugins-official server:iris -- "$MSG"
