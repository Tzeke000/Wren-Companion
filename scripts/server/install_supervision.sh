#!/usr/bin/env bash
# scripts/server/install_supervision.sh - install + enable the boot-time supervisor on iris-home.
# Idempotent. Needs passwordless sudo once for linger. (2026-10-07, the server port)
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[ "$(uname -s)" = "Linux" ] || { echo "Linux only"; exit 2; }
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 "$ROOT/scripts/server/systemd/iris-stack.service" "$HOME/.config/systemd/user/iris-stack.service"
install -m 0644 "$ROOT/scripts/server/systemd/iris-mouth.service" "$HOME/.config/systemd/user/iris-mouth.service"
install -m 0644 "$ROOT/scripts/server/systemd/iris-ears.service" "$HOME/.config/systemd/user/iris-ears.service"
# the mouth's sink address comes from the git-ignored private config, never a tracked file
mkdir -p "$HOME/.config/iris"
TOWER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tower_lan",""))' "$ROOT/config/private.local.json")"
[ -n "$TOWER" ] || { echo "tower_lan missing from config/private.local.json"; exit 3; }
printf 'IRIS_AUDIO_SINK=%s:8775
PYTHONUNBUFFERED=1
IRIS_MIC_SOURCE=%s:8776
' "$TOWER" "$TOWER" > "$HOME/.config/iris/voice.env"
chmod +x "$ROOT/scripts/server/iris_supervise.sh" "$ROOT/scripts/server/start_iris_linux.sh" "$ROOT"/start_iris*.sh
sudo -n loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user enable iris-stack.service iris-mouth.service iris-ears.service
echo "linger: $(loginctl show-user "$(id -un)" -p Linger)"
systemctl --user is-enabled iris-stack.service iris-mouth.service iris-ears.service
