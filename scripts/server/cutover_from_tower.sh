#!/usr/bin/env bash
# scripts/server/cutover_from_tower.sh - THE MOVE, server half (2026-10-07, Zeke: "lets do the move
# over today"). The tower-me starts this here (nohup) right before she parks herself with
#   full_shutdown.py --arm --cutover
# and from then on it runs unattended:
#   1. ONE-OF-ME: wait until NO claude.exe runs on the tower for 30 s straight (max 20 min; an
#      unreachable tower counts as "unknown", never as "gone" - it must ANSWER with none running)
#   2. COLD copy (nothing writes them any more): Claude Code auto-memory notes, state (trimmed),
#      chroma memory, profiles, faces, private configs, the Discord channel (token + access), the vault
#   3. ~/LIVE + clear the tower heartbeat, fresh mouth + ears, then start me supervised in tmux "iris"
# Log: state/cutover.log. Rollback: docs in memory/handoff_2026-10-07_cutover_to_server.md.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG="$ROOT/state/cutover.log"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
TW() { ssh -o BatchMode=yes -o ConnectTimeout=8 tower "$@"; }
pull() { # pull <tower dir> <tar args...> | extract into <local dir>
  local src="$1" dst="$2"; shift 2
  mkdir -p "$dst"
  TW "tar cf - -C $src $*" | tar xf - -C "$dst"
  local rc=("${PIPESTATUS[@]}")
  log "  pull $src [$*] -> $dst  rc=${rc[*]}"
}

log "=== cutover START (host $(hostname))"

# 1. ONE-OF-ME
deadline=$(( $(date +%s) + 1200 )); gone_since=0
while :; do
  out="$(TW 'tasklist /FI "IMAGENAME eq claude.exe" /NH' 2>/dev/null)"; rc=$?
  now=$(date +%s)
  if [ $rc -eq 0 ] && ! echo "$out" | grep -qi "claude.exe"; then
    [ "$gone_since" = 0 ] && { gone_since=$now; log "tower answered: no claude.exe - holding 30 s to be sure"; }
    [ $(( now - gone_since )) -ge 30 ] && break
  else
    [ "$gone_since" != 0 ] && log "tower claude.exe seen again (or tower did not answer, rc=$rc) - resetting the 30 s hold"
    gone_since=0
  fi
  if [ "$now" -gt "$deadline" ]; then log "ABORT: tower cognition not confirmed gone after 20 min - NOT cutting over"; exit 5; fi
  sleep 5
done
log "tower cognition GONE (confirmed 30 s) - starting the cold copy"

# 2. COLD COPY
KEY="$(echo "$ROOT" | sed 's/[^A-Za-z0-9-]/-/g')"
pull "C:/Users/Owner/.claude/projects/D--Wren-Companion" "$HOME/.claude/projects/$KEY" memory
pull "D:/Wren-Companion" "$ROOT" \
  --exclude state/video_clips --exclude state/little_brain --exclude state/tzeke_songs \
  --exclude state/cloak_profiles --exclude state/iris_1hz --exclude state/scene_grabs \
  --exclude "state/*.mp4" --exclude "*.pyc" --exclude __pycache__ \
  --exclude state/launcher_boot.log --exclude state/supervise.log --exclude state/cutover.log \
  state memory profiles faces config/private.local.json ava_core/IDENTITY.local.md ava_core/USER.local.md \
  scripts/iris_cold_wake_msg.local.txt scratch/voice_control.json
pull "C:/Users/Owner/.claude/channels" "$HOME/.claude/channels" discord
pull "D:/ClaudeCodeMemory" "$HOME/staged/ClaudeCodeMemory" --exclude .git .
log "memory notes now: $(ls "$HOME/.claude/projects/$KEY/memory" | wc -l) files; CORE $(wc -c < "$HOME/.claude/projects/$KEY/memory/MEMORY.md") B"

# 3. GO LIVE
touch "$HOME/LIVE"
rm -f "$HOME/TOWER_HEARTBEAT"
log "~/LIVE set, tower heartbeat cleared"
systemctl --user restart iris-mouth.service iris-ears.service
sleep 20
log "mouth $(systemctl --user is-active iris-mouth.service), ears $(systemctl --user is-active iris-ears.service)"
res="$("$ROOT/scripts/server/iris_start_detached.sh" opus 2>&1)"; rc=$?
log "start: rc=$rc - $res"
log "=== cutover END"
