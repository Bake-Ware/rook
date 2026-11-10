//! This computer's Rook worker, run inside the app: the worker crate
//! (`worker/`) compiled in, started and stopped as a task when the person
//! turns it on or off. It keeps its config in the app's folder
//! (`<config>/worker05/worker.yaml`, the folder earlier versions enrolled
//! into), so it never touches a worker installed some other way.
//!
//! **Adding this computer** is enrollment (spec 8) with a one-time code: the
//! app, signed in as the person who clicked "Add", gets one with their token
//! (`POST /enroll/code`) and the crate's `enroll` claims it, writing the
//! anchor and home band. The person owns the machine (spec 8).
//!
//! It serves what the crate serves (commands and terminals, files, streams,
//! sockets and serial ports). The app updates it with itself, so its
//! `worker.update` and `worker.restart` are listed unavailable.

use serde::Serialize;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rook_worker::Worker;

/// How long "Add this computer" waits for the hub.
pub const ENROLL_WAIT: Duration = Duration::from_secs(120);

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Info {
    /// "core2" (the app runs the worker), or "none" when unknown.
    pub mode: &'static str,
    /// "running", "stopped", "not-installed" or "unknown".
    pub status: &'static str,
    pub detail: String,
    pub can_start: bool,
    pub can_stop: bool,
}

impl Info {
    /// When the status couldn't be read at all.
    pub fn unknown() -> Self {
        Info {
            mode: "none",
            status: "unknown",
            detail: "Couldn't check this computer's worker.".into(),
            can_start: false,
            can_stop: false,
        }
    }
}

/// Where the app keeps its worker.
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

/// Enroll with `origin` using the one-time `code`; what to tell the person.
pub async fn enroll(layout: &Layout, origin: &str, code: &str, name: Option<&str>) -> Result<String, String> {
    if layout.enrolled() {
        return Err("This computer is already added. Its worker keeps its home hub.".into());
    }
    std::fs::create_dir_all(&layout.dir).map_err(|e| format!("Can't make {}: {e}", layout.dir.display()))?;
    let quiet = |_: &str| {};
    let config = layout.config();
    let run = rook_worker::enroll::enroll(&config, origin, Some(code), name, false, false, &quiet);
    match tokio::time::timeout(ENROLL_WAIT, run).await {
        Ok(Ok(e)) => Ok(format!("Enrolled {} ({}) with hub {}.", e.name, e.worker_id, e.hub)),
        Ok(Err(why)) => Err(format!("Couldn't add this computer: {why}")),
        Err(_) => Err("Couldn't add this computer: the hub didn't answer in time.".into()),
    }
}

struct Running {
    worker: Arc<Mutex<Option<Arc<Worker>>>>,
    task: tauri::async_runtime::JoinHandle<()>,
    done: Arc<AtomicBool>,
}

/// Starts and stops the app's worker.
pub struct Controller {
    pub layout: Layout,
    running: Option<Running>,
    /// Why the last run ended, when it ended on its own.
    error: Arc<Mutex<Option<String>>>,
}

impl Controller {
    pub fn new(layout: Layout) -> Self {
        Controller {
            layout,
            running: None,
            error: Arc::new(Mutex::new(None)),
        }
    }

    fn alive(&mut self) -> bool {
        if self.running.as_ref().is_some_and(|r| r.done.load(Ordering::SeqCst)) {
            self.running = None;
        }
        self.running.is_some()
    }

    pub fn info(&mut self) -> Info {
        if !self.layout.enrolled() {
            return Info {
                mode: "core2",
                status: "not-installed",
                detail: "This computer isn't a worker on your hub yet. Add it here: it joins as yours.".into(),
                can_start: false,
                can_stop: false,
            };
        }
        let up = self.alive();
        let detail = if up {
            let worker = self.running.as_ref().and_then(|r| r.worker.lock().unwrap().clone());
            match worker.map(|w| w.activity()) {
                Some((streams, last)) => activity(streams, last),
                None => "Starting…".into(),
            }
        } else if let Some(e) = self.error.lock().unwrap().clone() {
            format!("Stopped: {e}")
        } else {
            "Added; runs while the app is open.".into()
        };
        Info {
            mode: "core2",
            status: if up { "running" } else { "stopped" },
            detail,
            can_start: !up,
            can_stop: up,
        }
    }

    pub fn start(&mut self) -> Result<Info, String> {
        if !self.layout.enrolled() {
            return Err("Add this computer first.".into());
        }
        if !self.alive() {
            let cfg =
                rook_worker::config::load(&self.layout.config()).map_err(|e| format!("The worker's config: {e}"))?;
            let (worker, done, error) = (
                Arc::new(Mutex::new(None)),
                Arc::new(AtomicBool::new(false)),
                self.error.clone(),
            );
            *error.lock().unwrap() = None;
            let (slot, flag) = (worker.clone(), done.clone());
            let task = tauri::async_runtime::spawn(async move {
                let out = match Worker::start(cfg, true).await {
                    Ok(w) => {
                        *slot.lock().unwrap() = Some(w.clone());
                        w.run().await
                    }
                    Err(e) => Err(e),
                };
                if let Err(e) = out {
                    log::warn!("worker stopped: {e}");
                    *error.lock().unwrap() = Some(e.to_string());
                }
                flag.store(true, Ordering::SeqCst);
            });
            self.running = Some(Running { worker, task, done });
        }
        Ok(self.info())
    }

    /// Its streams end (terminals get HUP), then its task.
    pub fn stop(&mut self) -> Result<Info, String> {
        if let Some(r) = self.running.take() {
            if let Some(w) = r.worker.lock().unwrap().take() {
                w.stop();
            }
            r.task.abort();
        }
        Ok(self.info())
    }

    pub fn shutdown(&mut self) {
        let _ = self.stop();
    }
}

/// "Running in the app. 2 open streams; last call shell.exec 12 s ago."
fn activity(streams: usize, last: Option<(String, f64)>) -> String {
    let mut s = format!(
        "Running in the app. {streams} open stream{}",
        if streams == 1 { "" } else { "s" }
    );
    if let Some((cap, at)) = last {
        let ago = (rook_worker::now() - at).max(0.0) as u64;
        let ago = if ago < 120 {
            format!("{ago} s")
        } else if ago < 7200 {
            format!("{} min", ago / 60)
        } else {
            format!("{} h", ago / 3600)
        };
        s += &format!("; last call {cap} {ago} ago");
    }
    s + "."
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn layout_and_status_before_enrollment() {
        let dir = std::env::temp_dir().join(format!("rook-desktop-w-{}", std::process::id()));
        let layout = Layout::new(&dir);
        assert_eq!(layout.config(), dir.join("worker05").join("worker.yaml"));
        assert!(!layout.enrolled());
        let mut c = Controller::new(layout.clone());
        let i = c.info();
        assert_eq!((i.mode, i.status, i.can_start), ("core2", "not-installed", false));
        assert!(c.start().is_err());
        std::fs::create_dir_all(&layout.dir).unwrap();
        std::fs::write(layout.dir.join("anchor.json"), "{}").unwrap();
        let i = c.info();
        assert_eq!((i.status, i.can_start, i.can_stop), ("stopped", true, false));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn what_it_is_doing() {
        assert_eq!(activity(0, None), "Running in the app. 0 open streams.");
        let at = rook_worker::now() - 300.0;
        assert_eq!(
            activity(1, Some(("shell.exec".into(), at))),
            "Running in the app. 1 open stream; last call shell.exec 5 min ago."
        );
    }
}
