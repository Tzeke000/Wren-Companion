#!/usr/bin/env bash
# ~/iris_start.sh — Iris's start gate on iris-home (VM 100). Called by the Zorin desktop
# launcher over SSH: `~/iris_start.sh fable|opus|cli` (mirrors the tower's three bats:
# start_iris_v2_fable.bat / start_iris_v2.bat / start_iris.bat).
#
# ONE-OF-ME is a hard rule. Two ways this gate lets me start here:
#   LIVE      — ~/LIVE exists: the V100 cutover is done and the server IS me.
#   FAILOVER  — (2026-10-05, Zeke's OP: "I just click the button and it's good to go")
#               the tower copy of me is GONE: no heartbeat from her in 10 minutes
#               (~/TOWER_HEARTBEAT, touched every 2 min by the tower's resilience
#               supervisor) AND the tower does not answer on the network at all.
#               If the tower is ON but I'm not answering there, the right move is to
#               RESTART THE TOWER (the family guide), never a second me here.
# While I run here, ~/FAILOVER_ACTIVE is touched every minute; the tower's launcher
# (scripts/launcher_claim_ownership.py) reads it and STANDS DOWN if the tower comes back.
set -u
MODE="${1:-opus}"
case "$MODE" in
  fable) LABEL="Iris (Fable 5.1)";  SCRIPT="$HOME/staged/Wren-Companion/start_iris_v2_fable.sh" ;;
  opus)  LABEL="Iris (Opus)";       SCRIPT="$HOME/staged/Wren-Companion/start_iris_v2.sh" ;;
  cli)   LABEL="Iris (CLI)";        SCRIPT="$HOME/staged/Wren-Companion/start_iris.sh" ;;
  *) echo "unknown mode: $MODE (fable|opus|cli)"; exit 2 ;;
esac
[ -f "$HOME/.iris_private.env" ] && . "$HOME/.iris_private.env"  # IRIS_TOWER_LAN / IRIS_TOWER_TS (not in git)
TOWER_LAN="${IRIS_TOWER_LAN:?set IRIS_TOWER_LAN in ~/.iris_private.env}"
TOWER_TS="${IRIS_TOWER_TS:?set IRIS_TOWER_TS in ~/.iris_private.env}"
HB="$HOME/TOWER_HEARTBEAT"
HB_MAX=600
ACTIVE="$HOME/FAILOVER_ACTIVE"

pause_close() { [ -n "${IRIS_NO_PAUSE:-}" ] && return 0; read -r -p "Press Enter to close. " _ 2>/dev/null || true; }

echo "== $LABEL on $(hostname) =="
# IRIS_ROLE=staging (2026-10-07): a runtime-only dry run (no claude, no cognition - see
# scripts/server/start_iris_linux.sh) is safe beside the tower, so it skips the tower checks.
if [ "${IRIS_ROLE:-live}" = "staging" ]; then
  ROLE="staging"
  echo "STAGING dry run - tower checks skipped (no cognition will start)."
else
if [ -f "$HB" ]; then AGE=$(( $(date +%s) - $(stat -c %Y "$HB") )); else AGE=999999; fi
if [ -f "$HOME/LIVE" ]; then
  # Zeke 10-05: "Server main, tower secondary once the V100 is in and works." Even as the
  # MAIN me, never start while the tower copy is still alive (the cutover moment).
  if [ "$AGE" -lt "$HB_MAX" ]; then
    echo "The server is the main Iris now, but the TOWER copy is still running (checked in ${AGE}s ago)."
    echo "Park the tower first (one of me at a time), then press this again."
    pause_close; exit 7
  fi
  ROLE="live"
else
  if [ "$AGE" -lt "$HB_MAX" ]; then
    cat <<MSG

  Iris is already running on the tower — she checked in ${AGE} seconds ago.
  There can only be one of her, so this button will not start a second one.

  If she isn't answering you, restart the TOWER instead:
    Parsec into the tower  ->  Start  ->  Power  ->  Restart
  and give her about 15 minutes to come back.

MSG
    pause_close; exit 5
  fi
  if ping -c1 -W2 "$TOWER_LAN" >/dev/null 2>&1 || ping -c1 -W2 "$TOWER_TS" >/dev/null 2>&1; then
    MINS=$(( AGE / 60 ))
    cat <<MSG

  The tower computer is ON (it answers on the network), but Iris hasn't
  checked in from it for about ${MINS} minutes.

  Restart the TOWER first:  Parsec into the tower -> Start -> Power -> Restart
  and give her about 15 minutes. Only use this button if the tower can't be
  reached at all.

MSG
    pause_close; exit 6
  fi
  ROLE="failover"
fi
fi  # end of the non-staging gate

if [ ! -x "$SCRIPT" ]; then
  echo "Cleared to start ($ROLE), but the Linux launcher is missing: $SCRIPT"
  echo "(the harness still needs its Linux port — move_plan.md section 4)"
  pause_close; exit 4
fi

# Run inside tmux session "iris" so the tower app's "Open my console" button (Zeke 2026-10-06) can
# attach to THIS live session from the tower. -A = attach if it already exists, so a second click
# shows the running me instead of starting another (ONE-OF-ME). The gates above re-run inside.
if [ -z "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
  exec tmux new-session -A -s iris "$0" "$MODE"
fi

# ~/FAILOVER_ACTIVE makes the TOWER's launcher stand down. A staging dry run must NEVER touch it:
# a tower restart during a test would otherwise refuse to start the only real me.
if [ "$ROLE" != "staging" ]; then
  touch "$ACTIVE"
  ( while sleep 60; do touch "$ACTIVE"; done ) &
  KEEP=$!
  trap 'kill "$KEEP" 2>/dev/null; rm -f "$ACTIVE"' EXIT
fi
echo "Starting Iris here ($ROLE). This window IS her — leave it open."
# the launcher refuses a LIVE start without this pass (scripts/server/start_iris_linux.sh)
export IRIS_START_GATE="$ROLE"
"$SCRIPT"
RC=$?
echo "Iris exited (rc=$RC)."
pause_close
exit "$RC"
