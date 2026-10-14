//! "Install a hub on this computer": runs the hub installer
//! (scripts/install-hub.sh, bundled with the app) and connects the app to
//! the new hub.
//!
//! The installer is Linux-only (it says so itself on other systems). On a
//! Mac the app explains the Docker route; on Windows, WSL or Docker.

use serde::Serialize;
use std::path::{Path, PathBuf};

pub const SCRIPT_URL: &str = "https://raw.githubusercontent.com/Bake-Ware/rook/master/scripts/install-hub.sh";

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Plan {
    pub supported: bool,
    pub os: &'static str,
    pub title: String,
    pub message: String,
    /// A command to copy for doing it by hand (or in WSL).
    pub manual: String,
}

pub fn plan(os: &str) -> Plan {
    let terminal = format!("curl -fsSL {SCRIPT_URL} | bash -s -- --user");
    match os {
        "linux" => Plan {
            supported: true,
            os: "linux",
            title: "Install a hub on this computer".into(),
            message: "This sets up a Rook hub for your user account: it downloads Rook, starts it as a background service, and connects this app to it. It takes a few minutes. No administrator password is needed unless Python is missing.".into(),
            manual: terminal,
        },
        "macos" => Plan {
            supported: false,
            os: "macos",
            title: "Run a hub with Docker".into(),
            message: "The one-step hub installer is for Linux. On a Mac, run the hub in Docker Desktop with the compose file from the Rook repository (docker compose up -d), or use a Linux machine or VM on your network, then connect this app to it.".into(),
            manual: "git clone https://github.com/Bake-Ware/rook && cd rook && docker compose up -d".into(),
        },
        "windows" => Plan {
            supported: false,
            os: "windows",
            title: "Run a hub with WSL or Docker".into(),
            message: "The hub runs on Linux. On Windows, install WSL (Windows Subsystem for Linux) and run the installer inside it, or run the hub in Docker Desktop. Then connect this app to the address it prints.".into(),
            manual: format!("wsl --install    (once, then in the Ubuntu window:)  {terminal}"),
        },
        _ => Plan {
            supported: false,
            os: "other",
            title: "Install a hub".into(),
            message: "The hub installer runs on Linux. Run it on a Linux machine on your network, then connect this app to it.".into(),
            manual: terminal,
        },
    }
}

pub fn current_os() -> &'static str {
    if cfg!(target_os = "linux") {
        "linux"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(windows) {
        "windows"
    } else {
        "other"
    }
}

/// Installer arguments for what the person chose. `local_only` keeps the hub
/// on this computer (127.0.0.1); otherwise it listens on the home network.
pub fn args(local_only: bool, systemd_user: bool) -> Vec<String> {
    let mut a: Vec<String> = vec!["--user".into(), "--yes".into()];
    if local_only {
        a.extend(["--bind".into(), "127.0.0.1".into()]);
    }
    if !systemd_user {
        a.push("--no-systemd".into());
    }
    a
}

/// The installer script: bundled next to the app, else (a dev build) the
/// repository's copy.
pub fn script_path(resource_dir: Option<&Path>) -> Option<PathBuf> {
    let bundled = resource_dir.map(|d| d.join("install-hub.sh"));
    let dev = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../scripts/install-hub.sh");
    bundled.into_iter().chain([dev]).find(|p| p.is_file())
}

#[derive(Debug, Clone, Serialize, PartialEq, Default)]
pub struct Summary {
    pub dashboard: String,
    pub password: String,
}

/// The dashboard address and password from the installer's closing lines:
/// `  Dashboard   http://...` and `  Password    xyz      (any username)`.
pub fn parse_summary<'a>(lines: impl IntoIterator<Item = &'a str>) -> Option<Summary> {
    let mut s = Summary::default();
    for line in lines {
        let t = line.trim();
        if let Some(rest) = t.strip_prefix("Dashboard") {
            let v = rest.split_whitespace().next().unwrap_or("");
            if v.starts_with("http://") || v.starts_with("https://") {
                s.dashboard = v.to_string();
            }
        } else if let Some(rest) = t.strip_prefix("Password") {
            if let Some(v) = rest.split_whitespace().next() {
                s.password = v.to_string();
            }
        }
    }
    (!s.dashboard.is_empty()).then_some(s)
}

/// Whether `systemctl --user` works here (a desktop session usually has it;
/// containers and some minimal installs don't).
pub fn has_systemd_user() -> bool {
    std::process::Command::new("systemctl")
        .args(["--user", "show-environment"])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plans_per_os() {
        assert!(plan("linux").supported);
        let mac = plan("macos");
        assert!(!mac.supported && mac.message.contains("Docker"));
        let win = plan("windows");
        assert!(!win.supported && win.message.contains("WSL") && win.manual.contains("wsl --install"));
        assert!(!plan("freebsd").supported);
    }

    #[test]
    fn installer_args() {
        assert_eq!(args(false, true), ["--user", "--yes"]);
        assert_eq!(
            args(true, false),
            ["--user", "--yes", "--bind", "127.0.0.1", "--no-systemd"]
        );
    }

    #[test]
    fn summary_from_installer_output() {
        let out = "[rook] rook-hub is up\n\n[rook] The Rook hub is running.\n\n  Dashboard   http://192.0.2.10:7005\n  Password    s3cret-Pw      (any username)\n  MCP         http://192.0.2.10:8765/mcp    bearer token: ...\n";
        assert_eq!(
            parse_summary(out.lines()),
            Some(Summary {
                dashboard: "http://192.0.2.10:7005".into(),
                password: "s3cret-Pw".into()
            })
        );
        assert_eq!(parse_summary(["[rook] ERROR: the hub did not start"]), None);
    }

    #[test]
    fn dev_build_finds_the_repo_script() {
        let p = script_path(None).expect("scripts/install-hub.sh in the repo");
        let text = std::fs::read_to_string(p).unwrap();
        assert!(text.contains("Rook hub installer"));
        // The flags the wizard passes exist in this copy of the installer.
        for flag in ["--user)", "--yes)", "--bind)", "--no-systemd)"] {
            assert!(text.contains(flag), "installer lacks {flag}");
        }
    }
}
