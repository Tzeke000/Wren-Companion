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
const PVE_API: &str = "https://10.0.0.31:8006/api2/json";
const PVE_UI: &str = "https://10.0.0.31:8006/";
const IRIS_HOME: &str = "iris@10.0.0.32";
const DEFAULT_HEADER: &str = r"D:\Wren-Companion\state\secrets\pve_tower.hdr";
const ALLOWED_VMS: [u32; 3] = [100, 101, 102];
const CREATE_NO_WINDOW: u32 = 0x0800_0000; // background helpers must never flash a console over his game

fn header_file() -> String {
    std::env::var("IRIS_PVE_HEADER").unwrap_or_else(|_| DEFAULT_HEADER.to_string())
}

fn pve(method: &str, path: &str) -> Result<String, String> {
    let hdr = header_file();
    if !std::path::Path::new(&hdr).is_file() {
        return Err(format!("Proxmox token file missing ({hdr})"));
    }
    let url = format!("{PVE_API}{path}");
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
        reach("10.0.0.31:8006"),
        reach("10.0.0.32:22"),
        reach("10.0.0.32:5876")
    )
}

/// ssh = a shell on iris-home · console = attach to my live session there · proxmox = the web UI.
#[tauri::command]
fn server_open(kind: String) -> Result<(), String> {
    let ssh = r"C:\Windows\System32\OpenSSH\ssh.exe";
    let mut cmd = Command::new("cmd.exe");
    match kind.as_str() {
        "ssh" => cmd.args(["/c", "start", "Iris - server shell", ssh, "-t", IRIS_HOME]),
        "console" => cmd.args(["/c", "start", "Iris - my console", ssh, "-t", IRIS_HOME, "~/iris_console.sh"]),
        "proxmox" => cmd.args(["/c", "start", "", PVE_UI]),
        _ => return Err(format!("unknown target {kind}")),
    };
    // `start` opens the visible window we WANT; the helper cmd itself stays hidden.
    cmd.creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not open {kind}: {e}"))
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
        .invoke_handler(tauri::generate_handler![server_vms, server_vm_power, server_reach, server_open])
        .run(tauri::generate_context!())
        .expect("error while running Iris Control");
}
