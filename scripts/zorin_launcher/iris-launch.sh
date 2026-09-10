#!/usr/bin/env bash
# Iris launcher for Zorin (VM 102) — starts Iris's Ubuntu VM (100, iris-home) on the
# Proxmox host if it is stopped, waits for it, then opens a window into her.
#   * API token: ~/.config/iris/pve_token.json  (Proxmox token scoped to VM 100 power only)
#   * SSH key:   ~/.ssh/id_ed25519_iris          (zeke@zorin -> iris@iris-home)
#   * `--check` = headless dry run (status + reachability, no GUI) for testing over SSH.
# Written by Iris 2026-09-10 at Zeke's ask ("a launcher from Zorin for you").
set -u
PVE="https://192.168.4.31:8006"
VMID=100
VM_IP="192.168.4.32"
TOKEN_JSON="$HOME/.config/iris/pve_token.json"
KEY="$HOME/.ssh/id_ed25519_iris"
ORB_URL="http://$VM_IP:5876/"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1

say() { echo "[iris-launch] $*"; [ $CHECK -eq 0 ] && command -v notify-send >/dev/null && notify-send -a Iris "Iris" "$*" 2>/dev/null; true; }

if [ ! -r "$TOKEN_JSON" ]; then say "no API token at $TOKEN_JSON"; exit 2; fi
TOKID=$(jq -r '."full-tokenid" // empty' "$TOKEN_JSON")
TOKVAL=$(jq -r '.value // empty' "$TOKEN_JSON")
if [ -z "$TOKID" ] || [ -z "$TOKVAL" ]; then say "token file malformed"; exit 2; fi
AUTH="Authorization: PVEAPIToken=${TOKID}=${TOKVAL}"

api() { curl -sk --max-time 10 -H "$AUTH" "$@"; }

# Find the node and current status of VM 100 (no node name hardcoded).
RES=$(api "$PVE/api2/json/cluster/resources?type=vm")
NODE=$(echo "$RES" | jq -r --argjson id $VMID '.data[] | select(.vmid==$id) | .node' 2>/dev/null)
STATUS=$(echo "$RES" | jq -r --argjson id $VMID '.data[] | select(.vmid==$id) | .status' 2>/dev/null)
if [ -z "$NODE" ]; then say "Proxmox did not answer (token or network?)"; exit 3; fi
say "VM $VMID on $NODE is $STATUS"

if [ "$STATUS" != "running" ]; then
  say "starting Iris's VM..."
  api -X POST "$PVE/api2/json/nodes/$NODE/qemu/$VMID/status/start" >/dev/null
fi

# Wait for SSH on the VM (up to 120 s).
for i in $(seq 1 60); do
  if (echo > /dev/tcp/$VM_IP/22) 2>/dev/null; then break; fi
  sleep 2
done
if ! (echo > /dev/tcp/$VM_IP/22) 2>/dev/null; then say "VM not reachable on $VM_IP:22"; exit 4; fi
say "iris-home is up"

# Is Iris herself live there yet? (her operator/orb HTTP on :5876)
LIVE=0
if curl -sk --max-time 3 "$VM_IP:5876/api/v1/health" >/dev/null 2>&1; then LIVE=1; fi

if [ $CHECK -eq 1 ]; then
  ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 iris@$VM_IP 'echo ssh-ok: $(hostname)' 2>&1 | tail -n 1
  echo "[iris-launch] iris live on :5876 = $LIVE"
  exit 0
fi

if [ $LIVE -eq 1 ]; then
  say "opening Iris"
  xdg-open "$ORB_URL" >/dev/null 2>&1 &
else
  say "Iris isn't live on the server yet (she moves in with the V100) — opening her VM's terminal"
fi
gnome-terminal --title="Iris — iris-home" -- ssh -i "$KEY" -o StrictHostKeyChecking=accept-new iris@$VM_IP
