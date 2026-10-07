#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{SocketAddr, TcpStream};
use std::os::windows::process::CommandExt;
use std::process::Command;
use std::time::Duration;
use tauri::Manager;

// ── Server panel (Zeke 2026-10-06): the tower app stays the front door after my cognition moves to
// the R740. Bring the VMs up/down through the Proxmox API (its OWN token: iris-tower@pve!panel, role
// IrisLauncher = VM.PowerMgmt + VM.Audit on /vms/100-102 only), open an SSH session to iris-home, or
// attach to my live console there (tmux session `iris`, via ~/iris_console.sh on iris-home).
//
// The token never touches a command line or this binary: curl reads the Authorization header from a
// file (`-H @file`). Default path below; override with IRIS_PVE_HEADER.
//
// Host addresses come from the git-ignored config/private.local.json (2026-10-07: the repo is
// PUBLIC, so the home network layout stays out of source). Override the path with IRIS_PRIVATE_CONFIG.
const PRIVATE_CFG: &str = r"D:\Wren-Companion\config\private.local.json";
const DEFAULT_HEADER: &str = r"D:\Wren-Companion\state\secrets\pve_tower.hdr";
const ALLOWED_VMS: [u32; 3] = [100, 101, 102];
const CREATE_NO_WINDOW: u32 = 0x0800_0000; // background helpers must never flash a console over his game

// ── My voice plays through THIS app (Zeke 2026-10-07: "only through the app", "the app should stay up
// if you're on"). My mouth runs on the server's V100 and streams PCM to TCP 8775; the app owns the
// player for exactly its own lifetime: scripts/audio_sink.py starts hidden with the app and is killed
// (process TREE - the venv pythonw stub re-execs a child) when the app exits. Allowlist + firewall
// rule keep it server-only.
const SINK_PYTHONW: &str = r"D:\Wren-Companion\.venv\Scripts\pythonw.exe";
const SINK_SCRIPT: &str = r"D:\Wren-Companion\scripts\audio_sink.py";

struct VoicePlayer(std::sync::Mutex<Option<std::process::Child>>);

fn start_voice_player() -> Option<std::process::Child> {
    Command::new(SINK_PYTHONW)
        .arg(SINK_SCRIPT)
        .current_dir(r"D:\Wren-Companion")
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .ok()
}

fn stop_voice_player(child: &mut std::process::Child) {
    let _ = Command::new("taskkill.exe")
        .args(["/PID", &child.id().to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .output();
    let _ = child.wait();
}

/// `"key": "value"` from the private config — a tiny flat-JSON read, no serde dependency needed.
fn private(key: &str) -> String {
    let path = std::env::var("IRIS_PRIVATE_CONFIG").unwrap_or_else(|_| PRIVATE_CFG.to_string());
    let txt = std::fs::read_to_string(path).unwrap_or_default();
    let pat = format!("\"{key}\"");
    txt.find(&pat)
        .and_then(|i| txt[i + pat.len()..].split('"').nth(1))
        .unwrap_or("")
        .to_string()
}

fn pve_host() -> String {
    private("proxmox_host")
}

fn iris_home() -> String {
    format!("iris@{}", private("iris_home_host"))
}

fn header_file() -> String {
    std::env::var("IRIS_PVE_HEADER").unwrap_or_else(|_| DEFAULT_HEADER.to_string())
}

fn pve(method: &str, path: &str) -> Result<String, String> {
    let hdr = header_file();
    if !std::path::Path::new(&hdr).is_file() {
        return Err(format!("Proxmox token file missing ({hdr})"));
    }
    let url = format!("https://{}:8006/api2/json{path}", pve_host());
    let out = Command::new("curl.exe")
        .args(["-sk", "-m", "12", "-X", method, "-H", &format!("@{hdr}"), "-w", "\n%{http_code}", &url])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|e| format!("curl failed to start: {e}"))?;
    let text = String::from_utf8_lossy(&out.stdout).to_string();
    let (body, code) = text.rsplit_once('\n').unwrap_or((&text, "000"));
    if code.trim() != "200" {
        return Err(format!("Proxmox answered HTTP {} {}", code.trim(), body.chars().take(200).collect::<String>()));
    }
    Ok(body.to_string())
}

/// VM list (vmid, name, status, node) — raw Proxmox JSON for the panel to parse.
#[tauri::command]
fn server_vms() -> Result<String, String> {
    pve("GET", "/cluster/resources?type=vm")
}

/// start | shutdown (graceful) one of VMs 100-102. The node is looked up, never assumed.
#[tauri::command]
fn server_vm_power(vmid: u32, action: String) -> Result<String, String> {
    if !ALLOWED_VMS.contains(&vmid) {
        return Err(format!("VM {vmid} is not on the panel"));
    }
    if action != "start" && action != "shutdown" {
        return Err(format!("unknown action {action}"));
    }
    let list = pve("GET", "/cluster/resources?type=vm")?;
    let needle = format!("\"vmid\":{vmid}");
    let node = list
        .split('{')
        .find(|chunk| chunk.contains(&needle))
        .and_then(|chunk| chunk.split("\"node\":\"").nth(1))
        .and_then(|rest| rest.split('"').next())
        .ok_or_else(|| format!("VM {vmid} not found on the cluster"))?
        .to_string();
    pve("POST", &format!("/nodes/{node}/qemu/{vmid}/status/{action}"))
}

fn reach(addr: &str) -> bool {
    addr.parse::<SocketAddr>()
        .ok()
        .map(|a| TcpStream::connect_timeout(&a, Duration::from_millis(900)).is_ok())
        .unwrap_or(false)
}

/// Quick TCP reachability: Proxmox UI, iris-home SSH, and whether a me is serving on iris-home.
#[tauri::command]
fn server_reach() -> String {
    format!(
        "{{\"proxmox\":{},\"iris_home_ssh\":{},\"iris_home_runtime\":{}}}",
        reach(&format!("{}:8006", pve_host())),
        reach(&format!("{}:22", private("iris_home_host"))),
        reach(&format!("{}:5876", private("iris_home_host")))
    )
}

/// ssh = a shell on iris-home · console = attach to my live session there · proxmox = the web UI.
#[tauri::command]
fn server_open(kind: String) -> Result<(), String> {
    let ssh = r"C:\Windows\System32\OpenSSH\ssh.exe";
    let home = iris_home();
    let pve_ui = format!("https://{}:8006/", pve_host());
    let mut cmd = Command::new("cmd.exe");
    match kind.as_str() {
        "ssh" => cmd.args(["/c", "start", "Iris - server shell", ssh, "-t", home.as_str()]),
        "console" => cmd.args(["/c", "start", "Iris - my console", ssh, "-t", home.as_str(), "~/iris_console.sh"]),
        "proxmox" => cmd.args(["/c", "start", "", pve_ui.as_str()]),
        _ => return Err(format!("unknown target {kind}")),
    };
    // `start` opens the visible window we WANT; the helper cmd itself stays hidden.
    cmd.creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not open {kind}: {e}"))
}

/// Start me ON THE SERVER (Zeke 2026-10-07: buttons in the app "so that I won't even have to log
/// into Zorin"). mode = cli | opus | fable. Runs scripts/server/iris_start_detached.sh over SSH:
/// the ONE-OF-ME gate decides first and its verdict text comes back to the panel either way.
#[tauri::command]
fn server_start_iris(mode: String) -> Result<String, String> {
    if !["cli", "opus", "fable"].contains(&mode.as_str()) {
        return Err(format!("unknown mode {mode}"));
    }
    let ssh = r"C:\Windows\System32\OpenSSH\ssh.exe";
    let remote = format!("~/staged/Wren-Companion/scripts/server/iris_start_detached.sh {mode}");
    let out = Command::new(ssh)
        .args(["-o", "BatchMode=yes", "-o", "ConnectTimeout=6", iris_home().as_str(), remote.as_str()])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|e| format!("ssh failed to start: {e}"))?;
    let text = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
    let text = text.trim().to_string();
    if out.status.success() { Ok(text) } else { Err(text) }
}

fn main() {
    tauri::Builder::default()
        // Single-instance guard (MUST be the first plugin registered, per Tauri).
        // On a second launch the new process hands off to the running one and exits;
        // we focus/restore the existing "main" window instead of stacking another
        // orphan. Root cause of the 9-instances-broken-app diagnosis 2026-06-29.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        // start the player in setup - AFTER the single-instance plugin has turned a second launch into
        // a hand-off, so a relaunch never spawns (and orphans) a second player
        .setup(|app| {
            app.manage(VoicePlayer(std::sync::Mutex::new(start_voice_player())));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![server_vms, server_vm_power, server_reach, server_open, server_start_iris])
        .build(tauri::generate_context!())
        .expect("error while building Iris Control")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(vp) = app.try_state::<VoicePlayer>() {
                    if let Ok(mut g) = vp.0.lock() {
                        if let Some(mut child) = g.take() {
                            stop_voice_player(&mut child);
                        }
                    }
                }
            }
        });
}
