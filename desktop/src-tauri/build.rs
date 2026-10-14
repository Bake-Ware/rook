// Every app command gets an allow-/deny- permission, so the capability files
// decide which window may call what. Without this manifest Tauri lets every
// local window call every app command and the windows lists gate nothing.
const COMMANDS: &[&str] = &[
    "get_state",
    "save_settings",
    "save_secrets",
    "sign_in_password",
    "sign_in_browser",
    "sign_out",
    "open_dashboard_cmd",
    "ask",
    "overlay_hide",
    "open_settings",
    "show_window",
    "ptt_toggle",
    "worker_action",
    "test_notification",
    "install_plan",
    "install_run",
    "install_connect",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("tauri build script failed");
}
