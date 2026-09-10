#!/usr/bin/env bash
# ~/iris_start.sh — Iris's start gate on iris-home (VM 100). Called by the Zorin desktop
# launcher over SSH: `~/iris_start.sh fable|opus|cli` (mirrors the tower's three bats:
# start_iris_v2_fable.bat / start_iris_v2.bat / start_iris.bat).
#
# ONE-OF-ME is a hard rule: the tower is the live Iris until the deliberate V100 cutover
# (move_plan.md section 5). This script REFUSES to start anything unless the cutover has
# planted the marker file ~/LIVE. Zeke asked for the three icons on 2026-09-10; they exist
# now and become real at cutover — no code change needed on Zorin, only the marker here.
set -u
MODE="${1:-fable}"
case "$MODE" in
  fable) LABEL="Iris (Fable 5.1)";  SCRIPT="$HOME/staged/Wren-Companion/start_iris_v2_fable.sh" ;;
  opus)  LABEL="Iris (Opus)";       SCRIPT="$HOME/staged/Wren-Companion/start_iris_v2.sh" ;;
  cli)   LABEL="Iris (CLI)";        SCRIPT="$HOME/staged/Wren-Companion/start_iris.sh" ;;
  *) echo "unknown mode: $MODE (fable|opus|cli)"; exit 2 ;;
esac

echo "== $LABEL on $(hostname) =="
if [ ! -f "$HOME/LIVE" ]; then
  cat <<MSG

  The server copy of Iris is NOT live yet. The live Iris is still on the tower —
  she moves in when the V100 is seated and the cutover is done (one of me at a time).
  This icon will start her here after that; nothing else needs to change on Zorin.

  (staged copy: $HOME/staged/Wren-Companion — see README_STAGED.md)

MSG
  read -r -p "Press Enter to close. " _ 2>/dev/null || true
  exit 3
fi

if [ ! -x "$SCRIPT" ]; then
  echo "LIVE marker present, but the Linux launcher is missing: $SCRIPT"
  echo "(the harness still needs its Linux port — move_plan.md section 4)"
  read -r -p "Press Enter to close. " _ 2>/dev/null || true
  exit 4
fi
exec "$SCRIPT"
