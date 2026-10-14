//! "Install a hub on this computer": runs the hub installer
//! (scripts/install-hub.sh, bundled with the app) and connects the app to
//! the new hub.
//!
//! The installer is Linux-only (it says so itself on other systems). On a
//! Mac the app explains the Docker route; on Windows, WSL or Docker.
//!
//! The installer runs the hub as one service, `rook-hub` (dashboard, /mcp
//! and /band on one port), next to the relay `rook-relay`; installs from
//! before the single-process hub have `rook-dashboard` + `rook-mcp`
//! instead. It records what it did in `<app dir>/install.env`, which is how
//! the wizard finds a hub already installed here.

use serde::Serialize;
use std::collections::HashMap;
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
    /// A hub this installer already put on this computer.
    pub existing: Option<Existing>,
}

/// A hub installed here by scripts/install-hub.sh.
#[derive(Debug, Clone, Serialize, PartialEq, Default)]
pub struct Existing {
    /// "user" (~/.local/share/rook-hub) or "system" (/opt/rook).
    pub mode: String,
    /// "merged": the one `rook-hub` service; "split": dashboard + MCP.
    pub layout: String,
    pub systemd: bool,
    /// Whether its hub service is up (None: unknown, e.g. no systemd).
    pub running: Option<bool>,
    /// Its dashboard address (`ROOK_DOMAIN`), when readable.
    pub dashboard: String,
    /// The dashboard password, when readable (user installs). Never sent to
    /// the page; `install_connect` reads it here.
    #[serde(skip)]
    pub password: String,
    pub can_connect: bool,
    /// The service to look at with systemctl.
    pub unit: String,
    /// How to check on it by hand.
    pub manage: String,
}

/// `KEY=value` lines (the installer's env files; no quoting is used there).
pub fn read_env(text: &str) -> HashMap<String, String> {
    text.lines()
        .filter_map(|l| {
            let l = l.trim();
            if l.starts_with('#') {
                return None;
            }
            let (k, v) = l.split_once('=')?;
            Some((k.trim().to_string(), v.trim().trim_matches('"').to_string()))
        })
        .collect()
}

/// The install recorded under `user_app` (per-user) or `system_app`
/// (/opt/rook), user first: that is the one this app can manage.
pub fn find_existing(user_app: &Path, system_app: &Path) -> Option<Existing> {
    for (mode, dir) in [("user", user_app), ("system", system_app)] {
        let Ok(text) = std::fs::read_to_string(dir.join("install.env")) else {
            continue;
        };
        let env = read_env(&text);
        let data = env
            .get("DATA_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| dir.join("data"));
        let hub_env = std::fs::read_to_string(data.join("hub.env"))
            .map(|t| read_env(&t))
            .unwrap_or_default();
        let secrets = std::fs::read_to_string(data.join("secrets.env"))
            .map(|t| read_env(&t))
            .unwrap_or_default();
        let layout = if env.get("LAYOUT").map(String::as_str) == Some("split") {
            "split"
        } else {
            "merged"
        };
        let unit = if layout == "merged" {
            "rook-hub"
        } else {
            "rook-dashboard"
        };
        let systemd = env.get("SYSTEMD").map(String::as_str) != Some("0");
        let dashboard = hub_env.get("ROOK_DOMAIN").cloned().unwrap_or_default();
        let password = secrets.get("ROOK_WEB_PASS").cloned().unwrap_or_default();
        let manage = match (systemd, mode) {
            (true, "user") => format!("systemctl --user status {unit}"),
            (true, _) => format!("sudo systemctl status {unit}"),
            _ => format!("logs in {}/logs", data.display()),
        };
        return Some(Existing {
            mode: mode.into(),
            layout: layout.into(),
            systemd,
            running: None,
            can_connect: !dashboard.is_empty() && !password.is_empty(),
            dashboard,
            password,
            unit: unit.into(),
            manage,
        });
    }
    None
}

pub fn user_app_dir(home: &Path) -> PathBuf {
    std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| home.join(".local/share"))
        .join("rook-hub")
}

pub const SYSTEM_APP_DIR: &str = "/opt/rook";

/// The plan for this machine, including a hub already installed here and
/// whether its service is running. Blocking (reads files, runs systemctl).
pub fn plan_here(os: &str) -> Plan {
    let mut p = plan(os);
    if os != "linux" {
        return p;
    }
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    if let Some(mut e) = find_existing(&user_app_dir(&home), Path::new(SYSTEM_APP_DIR)) {
        if e.systemd {
            let mut args = vec!["is-active", "--quiet", e.unit.as_str()];
            if e.mode == "user" {
                args.insert(0, "--user");
            }
            e.running = std::process::Command::new("systemctl")
                .args(&args)
                .stdin(std::process::Stdio::null())
                .status()
                .ok()
                .map(|s| s.success());
        }
        p.title = "A hub is already installed here".into();
        p.message = if e.mode == "user" {
            "Rook's hub is installed on this computer for your user account. Connect this app to it, or update it to the latest release (your data and passwords are kept).".into()
        } else {
            "Rook's hub is installed on this computer system-wide. Connect this app to it with the dashboard password, or update it from a terminal with the command below.".into()
        };
        if e.mode == "system" {
            p.manual = format!("curl -fsSL {SCRIPT_URL} | sudo bash -s -- --system --yes");
        }
        p.existing = Some(e);
    }
    p
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
            existing: None,
        },
        "macos" => Plan {
            supported: false,
            os: "macos",
            title: "Run a hub with Docker".into(),
            message: "The one-step hub installer is for Linux. On a Mac, run the hub in Docker Desktop with the compose file from the Rook repository (docker compose up -d), or use a Linux machine or VM on your network, then connect this app to it.".into(),
            manual: "git clone https://github.com/Bake-Ware/rook && cd rook && docker compose up -d".into(),
            existing: None,
        },
        "windows" => Plan {
            supported: false,
            os: "windows",
            title: "Run a hub with WSL or Docker".into(),
            message: "The hub runs on Linux. On Windows, install WSL (Windows Subsystem for Linux) and run the installer inside it, or run the hub in Docker Desktop. Then connect this app to the address it prints.".into(),
            manual: format!("wsl --install    (once, then in the Ubuntu window:)  {terminal}"),
            existing: None,
        },
        _ => Plan {
            supported: false,
            os: "other",
            title: "Install a hub".into(),
            message: "The hub installer runs on Linux. Run it on a Linux machine on your network, then connect this app to it.".into(),
            manual: terminal,
            existing: None,
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
/// on this computer (127.0.0.1); otherwise it listens on the home network
/// (the installer's default). An update passes neither: a re-run changes
/// only the network settings it is given, so the hub keeps its own. The
/// single-process hub serves MCP on the dashboard port, so no --mcp-port.
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

/// The installer script bundled with the app. A debug build (`cargo run`,
/// no bundle) falls back to the repository's copy; a release build never
/// runs a script from a path baked in at compile time.
pub fn script_path(resource_dir: Option<&Path>) -> Option<PathBuf> {
    let bundled = resource_dir.map(|d| d.join("install-hub.sh"));
    #[cfg(debug_assertions)]
    let bundled = bundled
        .filter(|p| p.is_file())
        .or_else(|| Some(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../scripts/install-hub.sh")));
    bundled.filter(|p| p.is_file())
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
    fn existing_installs_are_found() {
        let root = std::env::temp_dir().join(format!("rook-desktop-inst-{}", std::process::id()));
        let user = root.join("user/rook-hub");
        let system = root.join("opt");
        assert_eq!(find_existing(&user, &system), None);
        let data = root.join("data");
        std::fs::create_dir_all(&user).unwrap();
        std::fs::create_dir_all(&data).unwrap();
        std::fs::write(
            user.join("install.env"),
            format!("MODE=user\nDATA_DIR={}\nSYSTEMD=1\nLAYOUT=merged\n", data.display()),
        )
        .unwrap();
        let e = find_existing(&user, &system).unwrap();
        assert_eq!(
            (e.mode.as_str(), e.unit.as_str(), e.can_connect),
            ("user", "rook-hub", false)
        );
        assert_eq!(e.manage, "systemctl --user status rook-hub");
        std::fs::write(data.join("hub.env"), "ROOK_DOMAIN=http://192.0.2.10:7005\n").unwrap();
        std::fs::write(data.join("secrets.env"), "ROOK_WEB_PASS=pw-123\nROOK_BAND_PSK=x\n").unwrap();
        let e = find_existing(&user, &system).unwrap();
        assert!(e.can_connect);
        assert_eq!(
            (e.dashboard.as_str(), e.password.as_str()),
            ("http://192.0.2.10:7005", "pw-123")
        );
        // The password never goes to the page.
        assert!(!serde_json::to_string(&e).unwrap().contains("pw-123"));
        // An install from before the single-process hub.
        std::fs::write(user.join("install.env"), "LAYOUT=split\nSYSTEMD=0\n").unwrap();
        let e = find_existing(&user, &system).unwrap();
        assert_eq!(
            (e.layout.as_str(), e.unit.as_str(), e.systemd),
            ("split", "rook-dashboard", false)
        );
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn env_files_parse() {
        let env = read_env("# comment\nA=1\nB = two words \nC=\"q\"\njunk\n");
        assert_eq!(env["A"], "1");
        assert_eq!(env["B"], "two words");
        assert_eq!(env["C"], "q");
        assert_eq!(env.len(), 3);
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
