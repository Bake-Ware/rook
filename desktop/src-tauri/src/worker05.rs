//! This computer's Rook 0.5 worker (castling), run by the app.
//!
//! The 0.5 worker is `python -m rook.core2.worker` from a Python that has
//! Rook installed (Settings > This computer's worker > Python; empty is
//! `python3`, or `python` on Windows). The app keeps its config in its own
//! folder (`<config>/worker05/worker.yaml`), so it never touches a worker
//! installed some other way.
//!
//! **Adding this computer** is 0.5 enrollment (spec 12) by device code: the
//! worker's own `enroll` asks the hub for a code and waits; the app, signed
//! in as the person who clicked "Add", approves that code with their token
//! (`POST /enroll/approve`); the worker gets its config bundle and writes its
//! anchor and home band. The person owns the machine (spec 9.1).
//!
//! The worker loads the desktop plugins from the Rook it runs with
//! (`DESKTOP_PLUGINS`); what this machine can't serve it lists as
//! unavailable, with the reason. It runs while the app is open, as the app's
//! child; stopping it asks it to stop cleanly (SIGTERM) first.

use serde_json::json;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::worker::Info;

/// Plugins from `rook/plugins` the desktop's worker loads: commands and
/// terminals, files, notifications on this screen, screenshots.
pub const DESKTOP_PLUGINS: [&str; 4] = ["shell", "file", "notification-desktop", "screen-desktop"];
/// How long "Add this computer" waits for the hub.
pub const ENROLL_WAIT: Duration = Duration::from_secs(120);

/// Where the app keeps its 0.5 worker.
#[derive(Debug, Clone)]
pub struct Layout {
    pub dir: PathBuf,
}

impl Layout {
    pub fn new(app_config: &Path) -> Self {
        Layout {
            dir: app_config.join("worker05"),
        }
    }

    pub fn config(&self) -> PathBuf {
        self.dir.join("worker.yaml")
    }

    pub fn log_file(&self) -> PathBuf {
        self.dir.join("worker.log")
    }

    /// Enrolled: the anchor enrollment writes beside the config.
    pub fn enrolled(&self) -> bool {
        self.dir.join("anchor.json").is_file()
    }

    /// Service endpoints from the config bundle (`<data>/services.json`).
    pub fn services(&self) -> Option<serde_json::Value> {
        let text = std::fs::read_to_string(self.dir.join("data").join("services.json")).ok()?;
        serde_json::from_str(&text).ok()
    }
}

/// The Python to run: the setting, else the usual name on this system.
pub fn python(setting: &str) -> PathBuf {
    let s = setting.trim();
    if !s.is_empty() {
        PathBuf::from(s)
    } else if cfg!(windows) {
        PathBuf::from("python")
    } else {
        PathBuf::from("python3")
    }
}

fn quiet(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    cmd.stdin(Stdio::null())
}

/// Prints where Rook's plugins are, and fails without the 0.5 worker.
const FIND_PLUGINS: &str =
    "import os, rook, rook.core2.worker; print(os.path.join(os.path.dirname(rook.__file__), 'plugins'))";

/// The `rook/plugins` folder of the Rook this Python has, if it has 0.5.
pub fn plugins_dir(python: &Path) -> Result<PathBuf, String> {
    let out = quiet(&mut Command::new(python))
        .args(["-c", FIND_PLUGINS])
        .output()
        .map_err(|e| format!("Can't run {}: {e}", python.display()))?;
    if !out.status.success() {
        return Err(format!(
            "{} has no Rook 0.5 (rook.core2). Install Rook in it, or pick another Python in Settings.",
            python.display()
        ));
    }
    Ok(PathBuf::from(String::from_utf8_lossy(&out.stdout).trim()))
}

/// The config the app starts from. JSON is YAML, and quotes paths safely;
/// enrollment adds the anchor, name and worker id and keeps the rest.
pub fn initial_config(plugins: &Path, name: Option<&str>) -> String {
    let list: Vec<String> = DESKTOP_PLUGINS
        .iter()
        .map(|p| plugins.join(p).display().to_string())
        .collect();
    let mut v = json!({"data": "data", "plugins": list, "hardware": ["display"],
                       "log": {"level": "info", "format": "text"}});
    if let Some(n) = name {
        v["name"] = json!(n);
    }
    serde_json::to_string_pretty(&v).expect("json") + "\n"
}

/// The user code in the enroll command's line "To add <name>, approve code
/// BCDF-GHJK on hub ..." (rook/core2/enroll/device.py `sign_in`).
pub fn user_code(line: &str) -> Option<String> {
    let rest = line.split("approve code ").nth(1)?;
    let code: String = rest
        .chars()
        .take_while(|c| c.is_ascii_alphabetic() || *c == '-')
        .collect();
    (code.len() == 9 && code.as_bytes()[4] == b'-').then_some(code)
}

/// Run `enroll` for `origin` and hand its user code to `approve` (on
/// another thread, so the hub can answer while the worker polls). Blocking.
pub fn enroll(
    layout: &Layout,
    python: &Path,
    origin: &str,
    name: Option<&str>,
    approve: impl Fn(String) -> Result<(), String>,
) -> Result<String, String> {
    if layout.enrolled() {
        return Err("This computer is already added. Its worker keeps its home hub.".into());
    }
    let plugins = plugins_dir(python)?;
    std::fs::create_dir_all(&layout.dir).map_err(|e| format!("Can't make {}: {e}", layout.dir.display()))?;
    std::fs::write(layout.config(), initial_config(&plugins, name))
        .map_err(|e| format!("Can't write the worker's config: {e}"))?;
    let mut child = quiet(&mut Command::new(python))
        .args(["-m", "rook.core2.worker", "--config"])
        .arg(layout.config())
        .args(["enroll", origin])
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Couldn't start the enrollment: {e}"))?;
    let stdout = child.stdout.take().expect("piped");
    let stderr = child.stderr.take().expect("piped");
    let err_reader = std::thread::spawn(move || {
        let mut s = String::new();
        let _ = std::io::Read::read_to_string(&mut BufReader::new(stderr), &mut s);
        s
    });
    // Nobody waits ten minutes (the code's own life) for a stuck hub.
    let child = Arc::new(Mutex::new(child));
    let done = Arc::new(AtomicBool::new(false));
    let watchdog = {
        let (child, done) = (child.clone(), done.clone());
        std::thread::spawn(move || {
            let until = Instant::now() + ENROLL_WAIT;
            while !done.load(Ordering::SeqCst) {
                if Instant::now() > until {
                    let _ = child.lock().unwrap().kill();
                    return;
                }
                std::thread::sleep(Duration::from_millis(200));
            }
        })
    };
    let mut said = String::new();
    let mut approved: Result<(), String> = Ok(());
    for line in BufReader::new(stdout).lines() {
        let Ok(line) = line else { break };
        if let Some(code) = user_code(&line) {
            approved = approve(code);
            if approved.is_err() {
                let _ = child.lock().unwrap().kill();
                break;
            }
        } else if !line.trim().is_empty() {
            said = line;
        }
    }
    let status = child.lock().unwrap().wait().map_err(|e| e.to_string());
    done.store(true, Ordering::SeqCst);
    let _ = watchdog.join();
    let status = status?;
    let errors = err_reader.join().unwrap_or_default();
    approved?;
    if status.success() {
        return Ok(said);
    }
    let why = errors
        .lines()
        .rev()
        .find(|l| !l.trim().is_empty())
        .unwrap_or("the enrollment stopped")
        .trim_start_matches("enroll: ");
    Err(format!("Couldn't add this computer: {why}"))
}

/// Starts and stops the app's 0.5 worker (its child process).
pub struct Controller {
    pub layout: Layout,
    child: Option<Child>,
}

impl Controller {
    pub fn new(layout: Layout) -> Self {
        Controller { layout, child: None }
    }

    fn alive(&mut self) -> bool {
        match self.child.as_mut().map(|c| c.try_wait()) {
            Some(Ok(None)) => true,
            Some(_) => {
                self.child = None;
                false
            }
            None => false,
        }
    }

    pub fn info(&mut self) -> Info {
        if !self.layout.enrolled() {
            return Info {
                mode: "core2",
                status: "not-installed",
                detail: "This computer isn't a worker on your 0.5 hub yet. Add it here: you approve it as yourself."
                    .into(),
                can_start: false,
                can_stop: false,
            };
        }
        let up = self.alive();
        Info {
            mode: "core2",
            status: if up { "running" } else { "stopped" },
            detail: if up {
                format!("Running from the app. Log: {}", self.layout.log_file().display())
            } else {
                "Added; runs while the app is open.".into()
            },
            can_start: !up,
            can_stop: up,
        }
    }

    pub fn start(&mut self, python: &Path) -> Result<Info, String> {
        if !self.layout.enrolled() {
            return Err("Add this computer first.".into());
        }
        if !self.alive() {
            let log = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(self.layout.log_file())
                .map_err(|e| format!("Can't open the worker log: {e}"))?;
            let err = log.try_clone().map_err(|e| e.to_string())?;
            let child = quiet(&mut Command::new(python))
                .args(["-m", "rook.core2.worker", "--config"])
                .arg(self.layout.config())
                .stdout(Stdio::from(log))
                .stderr(Stdio::from(err))
                .spawn()
                .map_err(|e| format!("Couldn't start the worker: {e}"))?;
            self.child = Some(child);
        }
        Ok(self.info())
    }

    /// SIGTERM, so the worker leaves the band cleanly; killed after 5 s.
    pub fn stop(&mut self) -> Result<Info, String> {
        if let Some(mut c) = self.child.take() {
            #[cfg(unix)]
            {
                let _ = Command::new("kill").arg(c.id().to_string()).status();
                let until = Instant::now() + Duration::from_secs(5);
                while Instant::now() < until && matches!(c.try_wait(), Ok(None)) {
                    std::thread::sleep(Duration::from_millis(100));
                }
            }
            let _ = c.kill();
            let _ = c.wait();
        }
        Ok(self.info())
    }

    pub fn shutdown(&mut self) {
        let _ = self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn user_code_from_the_enroll_line() {
        let line =
            "To add laptop, approve code BCDF-GHJK on hub 1a2b (the Rook app, the dashboard, or POST /enroll/approve).";
        assert_eq!(user_code(line).as_deref(), Some("BCDF-GHJK"));
        assert_eq!(
            user_code("Enrolled laptop (3f0a) with hub 1a2b, owned by person:bake."),
            None
        );
        assert_eq!(user_code("approve code BC"), None);
    }

    #[test]
    fn initial_config_is_yaml_with_the_desktop_plugins() {
        let text = initial_config(Path::new("/opt/rook/plugins"), Some("my-laptop"));
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["data"], "data");
        assert_eq!(v["name"], "my-laptop");
        assert_eq!(v["hardware"][0], "display");
        let plugins: Vec<&str> = v["plugins"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p.as_str().unwrap())
            .collect();
        assert_eq!(plugins.len(), DESKTOP_PLUGINS.len());
        assert!(plugins[2].ends_with("notification-desktop"));
        assert!(initial_config(Path::new("/x"), None).find("\"name\"").is_none());
    }

    #[test]
    fn layout_and_status_before_enrollment() {
        let dir = std::env::temp_dir().join(format!("rook-desktop-w05-{}", std::process::id()));
        let layout = Layout::new(&dir);
        assert_eq!(layout.config(), dir.join("worker05").join("worker.yaml"));
        assert!(!layout.enrolled());
        let mut c = Controller::new(layout.clone());
        let i = c.info();
        assert_eq!((i.mode, i.status, i.can_start), ("core2", "not-installed", false));
        assert!(c.start(Path::new("python3")).is_err());
        std::fs::create_dir_all(&layout.dir).unwrap();
        std::fs::write(layout.dir.join("anchor.json"), "{}").unwrap();
        let i = c.info();
        assert_eq!((i.status, i.can_start, i.can_stop), ("stopped", true, false));
        std::fs::remove_dir_all(&dir).ok();
        assert_eq!(python(" /venv/bin/python "), PathBuf::from("/venv/bin/python"));
    }
}
