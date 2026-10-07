#!/usr/bin/env bash
# scripts/server/start_iris_linux.sh - the Linux twin of start_iris_v2.bat (2026-10-07, the
# server port). usage: start_iris_linux.sh opus|fable
# Normally called by ~/iris_start.sh (scripts/zorin_launcher/iris_start.sh) INSIDE tmux
# session "iris", after that gate has checked ONE-OF-ME (tower heartbeat + ~/LIVE).
set -u
MODE="${1:-opus}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 2
mkdir -p "$ROOT/state" "$ROOT/.tmp"
LOG="$ROOT/state/launcher_boot.log"
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1000000 ]; then mv -f "$LOG" "$LOG.old"; fi
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
log "============================================================"
log "launcher START: $0 $MODE (role=${IRIS_ROLE:-live}, gate=${IRIS_START_GATE:-none})"

# ONE-OF-ME: a LIVE me starts only through ~/iris_start.sh, which exports IRIS_START_GATE after
# checking that the tower copy is parked/gone. A staging dry run is the only other way in.
if [ "${IRIS_ROLE:-live}" != "staging" ] && [ -z "${IRIS_START_GATE:-}" ]; then
  log "REFUSED: not started through ~/iris_start.sh (IRIS_START_GATE unset) and not IRIS_ROLE=staging."
  exit 3
fi

# single launcher per machine (the .bat used a Windows named mutex)
exec 9>"$ROOT/.tmp/launcher.lock"
if ! flock -n 9; then log "another launcher holds .tmp/launcher.lock - standing down"; exit 10; fi

case "$MODE" in
  opus)  IRIS_MODEL="claude-opus-5-5" ;;
  fable) IRIS_MODEL="claude-fable-5-1" ;;
  *) log "unknown mode: $MODE (opus|fable)"; exit 2 ;;
esac
export IRIS_MODEL
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
if [ -x "$HOME/.local/bin/claude" ]; then export IRIS_CLI_PATH="$HOME/.local/bin/claude"; fi
log "cli: IRIS_CLI_PATH=${IRIS_CLI_PATH:-<sdk bundled>}  model pin: IRIS_MODEL=$IRIS_MODEL"
PY="${IRIS_PYTHON:-$HOME/venvs/iris-v100/bin/python}"
if [ ! -x "$PY" ]; then log "FATAL: python missing at $PY"; exit 2; fi
log "python: $PY"

# sweeps - THIS user's processes for THIS repo only (the .bat swept by Win32 command line)
log "sweep 1/3: stale runtime / body host for $ROOT"
pkill -u "$(id -u)" -f "$ROOT/iris_runtime.py" 2>/dev/null && log "  killed stale iris_runtime"
pkill -u "$(id -u)" -f "$ROOT/iris_body_host.py" 2>/dev/null && log "  killed stale body host"
log "sweep 2/3: orphan cognition (claude with the Agent-SDK BODY host prompt)"
pkill -u "$(id -u)" -f "Agent-SDK BODY host" 2>/dev/null && log "  killed orphan claude"
rm -f "$ROOT/state/iris.pid"
log "sweep 3/3: freeing ports 5876 8769 8770"
for p in 5876 8769 8770; do fuser -k -n tcp "$p" >/dev/null 2>&1 && log "  freed :$p"; done
sleep 2

# Services the tower's .bat starts that are NOT ported yet - named so nobody assumes they run:
log "NOT started on Linux yet: voice watchdog (mouth :8769 + daemon :8770 - waits on Zeke's audio choice),"
log "  runtime watchdog (-> systemd), post-office :5877 (stays on the tower until cutover),"
log "  vector brain/nerves/little pilot (Vector moves at cutover), orb (Tauri app stays on the tower)."

log "starting iris_body_host.py (model=$IRIS_MODEL). Host stderr follows in this log."
# 2026-10-07: run the host as a WAITED child, not a plain foreground command - bash defers traps
# until a foreground child exits, so a stopped launcher (timeout, systemctl stop, tmux kill)
# used to leave the host AND its claude running as orphans (seen in the first staging test).
# fd 8 = the launcher's real stdin, so the host still sees the tmux tty (a background job's
# stdin would otherwise be /dev/null and the console reader would switch off).
exec 8<&0
"$PY" "$ROOT/iris_body_host.py" 0<&8 2>>"$LOG" &
HPID=$!
stop_stack() {
  log "launcher got a stop signal - stopping host $HPID and its children"
  pkill -TERM -P "$HPID" 2>/dev/null
  kill -TERM "$HPID" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$HPID" 2>/dev/null || break; sleep 1; done
  pkill -KILL -P "$HPID" 2>/dev/null; kill -KILL "$HPID" 2>/dev/null
  pkill -u "$(id -u)" -f "$ROOT/iris_runtime.py" 2>/dev/null
}
trap 'stop_stack; log "launcher END (stopped)."; exit 143' TERM INT HUP
wait "$HPID"
RC=$?
log "iris_body_host.py EXITED rc=$RC (if this lands seconds after the start line, the host never really came up)"
log "launcher END."
exit "$RC"
