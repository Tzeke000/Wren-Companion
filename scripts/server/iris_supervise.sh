#!/usr/bin/env bash
# scripts/server/iris_supervise.sh - keeps me running on the server (2026-10-07, the server port).
# Runs INSIDE tmux session "iris" (started at boot by systemd user unit iris-stack.service), so
# Zeke's "Open my console" button still attaches to the live me. The Linux replacement for the
# tower's Iris-Tower-AutoStart + iris_watchdog.ps1 restart poller.
#
#   loop: ~/iris_start.sh <mode>   (ONE-OF-ME gate: ~/LIVE + tower heartbeat/ping; then launcher)
#     gate refused (rc 5/6/7)  -> stop: the tower is me right now. Never loop against the gate.
#     launcher exited          -> restart after 30 s, at most 2 times per rolling hour, then stand down
#     .tmp/restart_cc.flag     -> (restart_self / safe_restart) stop the launcher; the loop restarts it
#   ~/IRIS_BOOT_TEST present   -> IRIS_ROLE=staging: the launcher runs the runtime-only dry run
#                                 (no claude, no cognition) - safe to test a power cut any time.
set -u
MODE="${1:-opus}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG="$ROOT/state/supervise.log"
mkdir -p "$ROOT/state" "$ROOT/.tmp"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
FLAG="$ROOT/.tmp/restart_cc.flag"
GATE="$HOME/iris_start.sh"
export IRIS_NO_PAUSE=1
STARTS=()

if [ -f "$HOME/IRIS_BOOT_TEST" ]; then
  export IRIS_ROLE=staging
  log "BOOT TEST marker present -> IRIS_ROLE=staging (runtime-only dry run, no cognition)"
fi
log "supervisor up: mode=$MODE role=${IRIS_ROLE:-live} host=$(hostname) boot=$(uptime -s)"

while :; do
  NOW=$(date +%s); KEEP=()
  for t in "${STARTS[@]:-}"; do [ -n "$t" ] && [ $((NOW - t)) -lt 3600 ] && KEEP+=("$t"); done
  STARTS=("${KEEP[@]:-}")
  N=0; for t in "${STARTS[@]:-}"; do [ -n "$t" ] && N=$((N+1)); done
  if [ "$N" -ge 3 ]; then
    log "STANDING DOWN: 3 starts within an hour (1 + 2 restarts). Not looping. Check state/launcher_boot.log."
    break
  fi
  STARTS+=("$NOW")
  rm -f "$FLAG"
  log "start #$((N+1)) this hour: $GATE $MODE"
  "$GATE" "$MODE" &
  GPID=$!
  while kill -0 "$GPID" 2>/dev/null; do
    if [ -f "$FLAG" ]; then
      log "restart flag seen ($(head -c 200 "$FLAG" 2>/dev/null | tr '\n' ' ')) - stopping the launcher for a restart"
      rm -f "$FLAG"
      pkill -TERM -P "$GPID" 2>/dev/null; kill -TERM "$GPID" 2>/dev/null
    fi
    sleep 2
  done
  wait "$GPID"; RC=$?
  log "gate/launcher exited rc=$RC"
  case "$RC" in
    5|6|7) log "the gate refused (rc=$RC: the tower copy is me right now) - supervisor exits, no loop."; break ;;
    4)     log "launcher missing (rc=4) - nothing to supervise."; break ;;
    10)    log "another launcher owns cognition (rc=10) - supervisor exits."; break ;;
  esac
  if [ "${IRIS_ROLE:-live}" = "staging" ]; then log "staging run finished - boot test complete, supervisor exits."; break; fi
  log "restarting in 30 s"
  sleep 30
done
log "supervisor END"
