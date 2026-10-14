//! The local worker: this computer's own Rook worker (the Python band worker
//! the hub's installer puts in `~/.rook-band-worker`).
//!
//! The app does not install or enroll a worker itself; the hub's Install page
//! does that with a pairing code. It finds what is there and starts or stops
//! it the way it was installed:
//!
//! * a systemd user service `rook-band-worker` (Linux installer): `systemctl --user`;
//! * the Scheduled Task `RookBandWorker` (Windows installer): `schtasks`;
//! * otherwise (macOS, Linux without systemd) the installed bundle, run by the
//!   app as a child process with the saved enrollment (`--enrolled --ws`).

use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

pub const SERVICE: &str = "rook-band-worker";
pub const WINDOWS_TASK: &str = "RookBandWorker";

#[derive(Debug, Clone, PartialEq)]
pub enum Mode {
    SystemdUser,
    WindowsTask,
    Bundle {
        python: PathBuf,
        pyz: PathBuf,
        enrolled: bool,
    },
    NotInstalled,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Info {
    /// "service", "task", "bundle" or "none".
    pub mode: &'static str,
    /// "running", "stopped", "needs-pairing" or "not-installed".
    pub status: &'static str,
    pub detail: String,
    pub can_start: bool,
    pub can_stop: bool,
}

/// Where the installer puts things (rook.core.paths: worker_install_dir,
/// worker_state_dir).
#[derive(Debug, Clone)]
pub struct Layout {
    pub install_dir: PathBuf,
    pub state_dir: PathBuf,
}

impl Layout {
    pub fn from_env(home: &Path) -> Self {
        let install_dir = home.join(".rook-band-worker");
        let state_dir = std::env::var("ROOK_WORKER_HOME")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| install_dir.clone());
        Layout { install_dir, state_dir }
    }

    pub fn pyz(&self) -> PathBuf {
        self.install_dir.join("band-worker.pyz")
    }

    pub fn python_candidates(&self) -> Vec<PathBuf> {
        let venv = self.install_dir.join("venv");
        if cfg!(windows) {
            vec![
                venv.join("Scripts").join("pythonw.exe"),
                venv.join("Scripts").join("python.exe"),
            ]
        } else {
            vec![venv.join("bin").join("python")]
        }
    }

    pub fn enrollment(&self) -> PathBuf {
        self.state_dir.join("enrollment.json")
    }

    pub fn log_file(&self) -> PathBuf {
        self.state_dir.join("desktop-worker.log")
    }

    /// The installed bundle and its interpreter, if both are present.
    pub fn bundle(&self) -> Option<Mode> {
        let pyz = self.pyz();
        if !pyz.is_file() {
            return None;
        }
        let python = self.python_candidates().into_iter().find(|p| p.is_file())?;
        Some(Mode::Bundle {
            python,
            pyz,
            enrolled: self.enrollment().is_file(),
        })
    }
}

/// The child-process command for an installed bundle: the saved enrollment,
/// over the hub's WebSocket bridge (what the installers run too).
pub fn bundle_command(python: &Path, pyz: &Path) -> (PathBuf, Vec<String>) {
    (
        python.to_path_buf(),
        vec![pyz.display().to_string(), "--enrolled".into(), "--ws".into()],
    )
}

/// `systemctl --user is-active` prints "active" (or activating) when up.
pub fn systemd_active(stdout: &str) -> bool {
    matches!(stdout.trim(), "active" | "activating" | "reloading")
}

/// `schtasks /Query /TN RookBandWorker /FO CSV /NH` ends each row with the
/// status column: "Running", "Ready", "Disabled"...
pub fn task_running(stdout: &str) -> bool {
    stdout
        .lines()
        .any(|l| l.trim_end().trim_end_matches('"').ends_with("Running"))
}

fn quiet(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console flash
    }
    cmd.stdin(Stdio::null())
}

fn run(program: &str, args: &[&str]) -> Option<(bool, String)> {
    let out = quiet(&mut Command::new(program)).args(args).output().ok()?;
    Some((out.status.success(), String::from_utf8_lossy(&out.stdout).into_owned()))
}

pub fn detect(layout: &Layout) -> Mode {
    if cfg!(target_os = "linux") {
        if let Some((true, _)) = run("systemctl", &["--user", "cat", &format!("{SERVICE}.service")]) {
            return Mode::SystemdUser;
        }
    }
    if cfg!(windows) {
        if let Some((true, _)) = run("schtasks", &["/Query", "/TN", WINDOWS_TASK]) {
            return Mode::WindowsTask;
        }
    }
    layout.bundle().unwrap_or(Mode::NotInstalled)
}

/// Starts and stops the worker; owns the child process in bundle mode.
pub struct Controller {
    pub layout: Layout,
    child: Option<Child>,
}

impl Controller {
    pub fn new(layout: Layout) -> Self {
        Controller { layout, child: None }
    }

    fn child_alive(&mut self) -> bool {
        match self.child.as_mut().map(|c| c.try_wait()) {
            Some(Ok(None)) => true,
            Some(_) => {
                self.child = None;
                false
            }
            None => false,
        }
    }

    /// A bundle worker running outside the app (the installer's nohup
    /// fallback), found by its command line.
    fn running_elsewhere(&self) -> bool {
        if cfg!(unix) {
            matches!(run("pgrep", &["-f", "band-worker.pyz"]), Some((true, _)))
        } else {
            false
        }
    }

    pub fn info(&mut self) -> Info {
        match detect(&self.layout) {
            Mode::SystemdUser => {
                let up = run("systemctl", &["--user", "is-active", SERVICE])
                    .map(|(_, o)| systemd_active(&o))
                    .unwrap_or(false);
                Info {
                    mode: "service",
                    status: if up { "running" } else { "stopped" },
                    detail: format!("Installed as the user service {SERVICE}."),
                    can_start: !up,
                    can_stop: up,
                }
            }
            Mode::WindowsTask => {
                let up = run("schtasks", &["/Query", "/TN", WINDOWS_TASK, "/FO", "CSV", "/NH"])
                    .map(|(_, o)| task_running(&o))
                    .unwrap_or(false);
                Info {
                    mode: "task",
                    status: if up { "running" } else { "stopped" },
                    detail: format!("Installed as the Scheduled Task {WINDOWS_TASK}."),
                    can_start: !up,
                    can_stop: up,
                }
            }
            Mode::Bundle { enrolled: false, .. } => Info {
                mode: "bundle",
                status: "needs-pairing",
                detail:
                    "The worker is installed but not paired with a hub. Run the installer from your hub's Install page."
                        .into(),
                can_start: false,
                can_stop: false,
            },
            Mode::Bundle { .. } => {
                let ours = self.child_alive();
                let up = ours || self.running_elsewhere();
                Info {
                    mode: "bundle",
                    status: if up { "running" } else { "stopped" },
                    detail: if ours {
                        format!("Running from the app. Log: {}", self.layout.log_file().display())
                    } else if up {
                        "Running (started outside the app).".into()
                    } else {
                        "Installed; runs while the app is open.".into()
                    },
                    can_start: !up,
                    can_stop: up,
                }
            }
            Mode::NotInstalled => Info {
                mode: "none",
                status: "not-installed",
                detail: "This computer isn't a worker yet. Open your hub's Install page to add it.".into(),
                can_start: false,
                can_stop: false,
            },
        }
    }

    pub fn start(&mut self) -> Result<Info, String> {
        match detect(&self.layout) {
            Mode::SystemdUser => checked(run("systemctl", &["--user", "start", SERVICE]), "start the service")?,
            Mode::WindowsTask => checked(run("schtasks", &["/Run", "/TN", WINDOWS_TASK]), "start the task")?,
            Mode::Bundle {
                python,
                pyz,
                enrolled: true,
            } => {
                if !self.child_alive() && !self.running_elsewhere() {
                    let (program, args) = bundle_command(&python, &pyz);
                    let log = std::fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(self.layout.log_file())
                        .map_err(|e| format!("Can't open the worker log: {e}"))?;
                    let err = log.try_clone().map_err(|e| e.to_string())?;
                    let child = quiet(&mut Command::new(program))
                        .args(args)
                        .stdout(Stdio::from(log))
                        .stderr(Stdio::from(err))
                        .spawn()
                        .map_err(|e| format!("Couldn't start the worker: {e}"))?;
                    self.child = Some(child);
                }
            }
            Mode::Bundle { enrolled: false, .. } | Mode::NotInstalled => {
                return Err("Add this computer from your hub's Install page first.".into())
            }
        }
        Ok(self.info())
    }

    pub fn stop(&mut self) -> Result<Info, String> {
        match detect(&self.layout) {
            Mode::SystemdUser => checked(run("systemctl", &["--user", "stop", SERVICE]), "stop the service")?,
            Mode::WindowsTask => checked(run("schtasks", &["/End", "/TN", WINDOWS_TASK]), "stop the task")?,
            Mode::Bundle { .. } => {
                if let Some(mut c) = self.child.take() {
                    let _ = c.kill();
                    let _ = c.wait();
                } else if self.running_elsewhere() {
                    let _ = run("pkill", &["-f", "band-worker.pyz"]);
                }
            }
            Mode::NotInstalled => {}
        }
        Ok(self.info())
    }

    /// On quit: a worker the app started stops with it (services keep running).
    pub fn shutdown(&mut self) {
        if let Some(mut c) = self.child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

fn checked(r: Option<(bool, String)>, what: &str) -> Result<(), String> {
    match r {
        Some((true, _)) => Ok(()),
        Some((false, _)) => Err(format!("Couldn't {what}. It may need an administrator.")),
        None => Err(format!("Couldn't {what}: the system tool isn't available.")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn layout_paths() {
        let l = Layout {
            install_dir: PathBuf::from("/h/.rook-band-worker"),
            state_dir: PathBuf::from("/s"),
        };
        assert_eq!(l.pyz(), PathBuf::from("/h/.rook-band-worker/band-worker.pyz"));
        assert_eq!(l.enrollment(), PathBuf::from("/s/enrollment.json"));
        assert!(l.python_candidates()[0].starts_with("/h/.rook-band-worker/venv"));
    }

    #[test]
    fn bundle_detection_and_command() {
        let dir = std::env::temp_dir().join(format!("rook-desktop-worker-{}", std::process::id()));
        let l = Layout {
            install_dir: dir.clone(),
            state_dir: dir.clone(),
        };
        assert_eq!(l.bundle(), None);
        std::fs::create_dir_all(l.python_candidates()[0].parent().unwrap()).unwrap();
        std::fs::write(l.pyz(), b"zip").unwrap();
        assert_eq!(l.bundle(), None, "no interpreter yet");
        std::fs::write(&l.python_candidates()[0], b"").unwrap();
        assert!(matches!(l.bundle(), Some(Mode::Bundle { enrolled: false, .. })));
        std::fs::write(l.enrollment(), b"{}").unwrap();
        let Some(Mode::Bundle {
            python,
            pyz,
            enrolled: true,
        }) = l.bundle()
        else {
            panic!("bundle")
        };
        let (prog, args) = bundle_command(&python, &pyz);
        assert_eq!(prog, python);
        assert_eq!(args[1..], ["--enrolled".to_string(), "--ws".to_string()]);
        std::fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn service_status_parsing() {
        assert!(systemd_active("active\n"));
        assert!(!systemd_active("inactive\n"));
        assert!(!systemd_active("failed"));
        assert!(task_running("\"\\RookBandWorker\",\"N/A\",\"Running\"\r\n"));
        assert!(!task_running("\"\\RookBandWorker\",\"N/A\",\"Ready\""));
    }
}
