#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;

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
        .run(tauri::generate_context!())
        .expect("error while running Iris Control");
}
