#!/usr/bin/env bash
# scripts/server/install_supervision.sh - install + enable the boot-time supervisor on iris-home.
# Idempotent. Needs passwordless sudo once for linger. (2026-10-07, the server port)
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[ "$(uname -s)" = "Linux" ] || { echo "Linux only"; exit 2; }
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 "$ROOT/scripts/server/systemd/iris-stack.service" "$HOME/.config/systemd/user/iris-stack.service"
chmod +x "$ROOT/scripts/server/iris_supervise.sh" "$ROOT/scripts/server/start_iris_linux.sh" "$ROOT"/start_iris*.sh
sudo -n loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user enable iris-stack.service
echo "linger: $(loginctl show-user "$(id -un)" -p Linger)"
systemctl --user is-enabled iris-stack.service
