# Zorin desktop launcher for Iris-on-the-server (built 2026-09-10)

Installed on VM 102 (`zorin`, 192.168.4.34) at Zeke's ask: *"I'll need a launcher from Zorin for you so that you can start in the server, and be in your Ubuntu VM."*

- `iris-launch.sh` → `/home/zeke/.local/bin/iris-launch.sh` — queries the Proxmox API for VM 100's node/status, starts it if stopped, waits for :22 on 192.168.4.32, then opens `gnome-terminal` with `ssh iris@iris-home` (and `xdg-open http://192.168.4.32:5876/` once Iris is live there). `--check` = headless dry run.
- `Iris.desktop` → `~/Desktop/Iris.desktop` (+ `~/.local/share/applications/iris-server.desktop`), marked trusted via `gio`.
- `iris.png` → `~/.local/share/icons/iris.png` (upscaled from the orb's 16 px .ico — the only icon in the repo; replace when a real one exists).
- Secrets (NOT in the repo): Proxmox API token `iris-launcher@pve!zorin` (role `IrisLauncher` = `VM.PowerMgmt VM.Audit` on `/vms/100` only), stored on Zorin at `~/.config/iris/pve_token.json` (600); copy on pve at `/root/iris_launcher_token.json`. SSH key `~/.ssh/id_ed25519_iris` on Zorin, authorized for `iris@192.168.4.32`.
- Tested 09-10 09:1x headless: VM 100 running on r740, SSH ok. The GUI click itself untested (needs Zeke at the desktop).
