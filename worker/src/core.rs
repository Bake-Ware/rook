//! Core updates (docs/castling-updates.md, "A one-file worker"). The
//! installed binary is the base and is never overwritten; a release a hub
//! sends lives in `<data>/core/<version>/<artifact>` beside `state.json`,
//! the same file the Python worker keeps:
//!
//! ```text
//! {"current": "0.5.1" | null, "previous": ..., "trial": {"version", "boots", "from", "at"} | null,
//!  "failed": {"version", "reason", "at"} | null}          (null: the base)
//! ```
//!
//! Every start goes through the base, which picks the file to run (`select`)
//! and counts a trial's boots: after three starts without confirming, the
//! trial is dropped and `current` runs again. A trial confirms on its first
//! call served for the hub; after `TRIAL_SECS` without one it gives up.

use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const MAX_BOOTS: u64 = 3;
const HOP: &str = "ROOK_CORE_HOP"; // set across our own exec: the version (or "base") it went to
const BASE: &str = "ROOK_CORE_BASE"; // the installed binary, so a release restarts through it
pub const INSTALLED: &str = "installed";

/// The file this worker runs, by name: what a release must hold for it.
pub fn artifact() -> String {
    format!(
        "rook-worker-{}-{}{}",
        if cfg!(windows) { "windows" } else { "linux" },
        std::env::consts::ARCH,
        if cfg!(windows) { ".exe" } else { "" }
    )
}

pub fn trial_secs() -> Duration {
    let secs = std::env::var("ROOK_UPDATE_TRIAL_SECS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(120.0);
    Duration::from_secs_f64(secs)
}

pub struct Core {
    pub root: PathBuf,
    /// The release this process runs (None: the base).
    pub running: Option<String>,
}

fn now() -> f64 {
    (crate::now() * 1000.0).round() / 1000.0
}

impl Core {
    pub fn new(data: &Path) -> Core {
        let root = data.join("core");
        let me = std::env::current_exe().ok().and_then(|p| p.canonicalize().ok());
        let running = me.and_then(|exe| {
            let dir = exe.parent()?;
            (dir.parent()? == root.canonicalize().ok()?).then(|| dir.file_name()?.to_str().map(String::from))?
        });
        Core { root, running }
    }

    pub fn state(&self) -> Map<String, Value> {
        let got: Value = fs::read_to_string(self.root.join("state.json"))
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or(Value::Null);
        ["current", "previous", "trial", "failed"]
            .iter()
            .map(|k| (k.to_string(), got[*k].clone()))
            .collect()
    }

    fn save(&self, state: &Map<String, Value>) {
        let _ = fs::create_dir_all(&self.root);
        let tmp = self.root.join("state.json.tmp");
        if fs::write(&tmp, serde_json::to_string_pretty(state).unwrap()).is_ok() {
            let _ = fs::rename(&tmp, self.root.join("state.json"));
        }
    }

    fn file(&self, version: &str) -> PathBuf {
        self.root.join(version).join(artifact())
    }

    fn ok_name(v: &str) -> bool {
        !v.is_empty() && !v.starts_with('.') && !v.contains(['/', '\\'])
    }

    /// The version this start runs (None: the base), counting a trial's
    /// boots; `hop` is the version our own exec just went to (counted already).
    pub fn choose(&self, hop: Option<&str>) -> Option<String> {
        let mut state = self.state();
        if let Some(v) = state["trial"]["version"]
            .as_str()
            .filter(|v| Self::ok_name(v))
            .map(String::from)
        {
            if hop == Some(v.as_str()) {
                return Some(v);
            }
            let boots = state["trial"]["boots"].as_u64().unwrap_or(MAX_BOOTS);
            let why = if !self.file(&v).is_file() {
                Some("the release's file is missing".to_string())
            } else if boots >= MAX_BOOTS {
                Some(format!(
                    "did not come up: {MAX_BOOTS} starts without serving a call from the home hub"
                ))
            } else {
                None
            };
            match why {
                None => {
                    state["trial"]["boots"] = json!(boots + 1);
                    self.save(&state);
                    return Some(v);
                }
                Some(why) => {
                    eprintln!("rook-worker: core release {v} dropped: {why}");
                    state["trial"] = Value::Null;
                    state["failed"] = json!({"version": v, "reason": why, "at": now()});
                    self.save(&state);
                }
            }
        }
        for key in ["current", "previous"] {
            match state[key].as_str() {
                None => return None, // the base, known good
                Some(v) if Self::ok_name(v) && self.file(v).is_file() => return Some(v.to_string()),
                Some(v) => eprintln!("rook-worker: core release {v} can't run: its file is missing"),
            }
        }
        None
    }

    pub fn in_trial(&self) -> bool {
        self.running.is_some() && self.state()["trial"]["version"].as_str() == self.running.as_deref()
    }

    pub fn status(&self) -> Value {
        let mut kept: Vec<String> = fs::read_dir(&self.root)
            .map(|d| {
                d.flatten()
                    .filter(|e| e.path().is_dir())
                    .filter_map(|e| e.file_name().to_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        kept.retain(|n| !n.starts_with('.'));
        kept.sort();
        let mut out = self.state();
        out.insert("running".into(), json!(self.running));
        out.insert("kept".into(), json!(kept));
        Value::Object(out)
    }

    /// Take a verified release: its file under `<root>/<version>/` (with its
    /// release.sig), then make it the trial. The caller restarts.
    pub fn receive(&self, sig: &Value, file: Vec<u8>) -> Result<Value, String> {
        let mut state = self.state();
        if let Some(t) = state["trial"]["version"].as_str() {
            return Err(format!("an update to {t} is already waiting for its restart"));
        }
        let version = sig["version"]
            .as_str()
            .filter(|v| Self::ok_name(v))
            .ok_or("malformed: version")?
            .to_string();
        if Some(&version) == self.running.as_ref() {
            return Err(format!("already running {version}"));
        }
        let folder = self.root.join(&version);
        if let Ok(old) = fs::read_to_string(folder.join("release.sig")) {
            if serde_json::from_str::<Value>(&old)
                .map(|o| o["files"] != sig["files"])
                .unwrap_or(true)
            {
                return Err(format!(
                    "{version} is already here with different files; bump the version"
                ));
            }
        }
        fs::create_dir_all(&folder).map_err(|e| e.to_string())?;
        let path = self.file(&version);
        let part = path.with_extension("part");
        fs::write(&part, &file).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&part, fs::Permissions::from_mode(0o755)).map_err(|e| e.to_string())?;
        }
        fs::rename(&part, &path).map_err(|e| e.to_string())?;
        fs::write(folder.join("release.sig"), sig.to_string()).map_err(|e| e.to_string())?;
        state["trial"] = json!({"version": version, "boots": 0, "from": self.running, "at": now()});
        state["failed"] = Value::Null;
        self.save(&state);
        self.prune(&state);
        eprintln!("rook-worker: core update {version} ready");
        Ok(json!({"version": version, "from": self.running, "hub": sig["hub"], "restarting": true}))
    }

    /// The trial served a call for the hub: it becomes current.
    pub fn confirm(&self) {
        let mut state = self.state();
        if !self.in_trial() {
            return;
        }
        if state["current"].as_str() != self.running.as_deref() {
            state["previous"] = state["current"].clone();
        }
        state["current"] = json!(self.running);
        state["trial"] = Value::Null;
        if state["failed"]["version"].as_str() == self.running.as_deref() {
            state["failed"] = Value::Null;
        }
        self.save(&state);
        self.prune(&state);
        eprintln!(
            "rook-worker: core update {} confirmed",
            self.running.as_deref().unwrap_or("base")
        );
    }

    /// Drop the trial; the caller restarts into `current`.
    pub fn abandon(&self, reason: &str) {
        let mut state = self.state();
        let Some(v) = state["trial"]["version"].as_str().map(String::from) else {
            return;
        };
        state["trial"] = Value::Null;
        state["failed"] = json!({"version": v, "reason": reason, "at": now()});
        self.save(&state);
        eprintln!("rook-worker: core update {v} rolled back: {reason}");
    }

    /// Make a kept core current (`installed`: the base); the caller restarts.
    pub fn switch(&self, version: &str) -> Result<Value, String> {
        let mut state = self.state();
        if !state["trial"].is_null() {
            return Err("an update is waiting for its restart; restart or let it finish first".into());
        }
        let to = (version != INSTALLED).then(|| version.to_string());
        let cur = state["current"].as_str().map(String::from);
        if to == cur {
            return Err(format!("already on {version}"));
        }
        if let Some(v) = &to {
            if Some(v.as_str()) != state["previous"].as_str() {
                let kept: Vec<&str> = [cur.as_deref(), state["previous"].as_str(), Some(INSTALLED)]
                    .into_iter()
                    .flatten()
                    .collect();
                return Err(format!(
                    "{version} isn't kept here (kept: {}); update.rollout sends it",
                    kept.join(", ")
                ));
            }
            if !self.file(v).is_file() {
                return Err(format!("the kept core {v} can't run: its file is missing"));
            }
        }
        state["current"] = json!(to);
        state["previous"] = json!(cur);
        self.save(&state);
        Ok(json!({"version": to, "from": cur, "restarting": true}))
    }

    fn prune(&self, state: &Map<String, Value>) {
        let keep = [
            state["current"].as_str(),
            state["previous"].as_str(),
            state["trial"]["version"].as_str(),
            self.running.as_deref(),
        ];
        for e in fs::read_dir(&self.root).into_iter().flatten().flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            if e.path().is_dir() && !keep.contains(&Some(name.as_str())) {
                let _ = fs::remove_dir_all(e.path());
            }
        }
    }
}

/// `release.sig` and this worker's file from a release tarball, unverified.
pub fn unpack(blob: &[u8]) -> Result<(Value, Vec<u8>), String> {
    let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(blob));
    let (mut sig, mut file) = (None, None);
    let want = artifact();
    for entry in tar.entries().map_err(|e| format!("not a release: {e}"))? {
        let mut entry = entry.map_err(|e| format!("not a release: {e}"))?;
        let name = entry.path().map_err(|e| e.to_string())?.to_string_lossy().into_owned();
        let mut data = vec![];
        if name == "release.sig" || name == want {
            entry
                .read_to_end(&mut data)
                .map_err(|e| format!("not a release: {e}"))?;
        }
        if name == "release.sig" {
            sig = Some(serde_json::from_slice(&data).map_err(|e| format!("not a release: {e}"))?);
        } else if name == want {
            file = Some(data);
        }
    }
    let sig: Value = sig.ok_or("not a release: no release.sig")?;
    Ok((sig, file.ok_or(format!("this release has no {want}"))?))
}

/// Whether `file` is the release's `artifact()` with the hash `sig` signed.
pub fn matches(sig: &Value, file: &[u8]) -> bool {
    sig["files"][artifact()].as_str() == Some(crate::link::hex(&Sha256::digest(file)).as_str())
}

/// At every start, before anything else: run the core `state.json` picks.
/// Execs into it (Linux) or runs it as a child and exits with its code
/// (Windows); returns when this process is the one to run.
pub fn select(data: &Path) {
    let hop = std::env::var(HOP).ok();
    std::env::remove_var(HOP);
    let me = std::env::current_exe().unwrap_or_default();
    let base = std::env::var(BASE).map(PathBuf::from).unwrap_or(me);
    std::env::set_var(BASE, &base);
    let core = Core::new(data);
    let want = core.choose(hop.as_deref().filter(|h| *h != "base"));
    if want == core.running {
        return;
    }
    if hop.is_some() {
        eprintln!(
            "rook-worker: core switch did not take (wanted {want:?}, running {:?})",
            core.running
        );
        return;
    }
    let target = want.as_ref().map_or(base, |v| core.file(v));
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut cmd = std::process::Command::new(&target);
    cmd.args(&args).env(HOP, want.as_deref().unwrap_or("base"));
    #[cfg(unix)]
    let e = {
        use std::os::unix::process::CommandExt;
        cmd.exec()
    };
    #[cfg(windows)]
    let e = match cmd.status() {
        Ok(status) => std::process::exit(status.code().unwrap_or(1)),
        Err(e) => e,
    };
    // The release can't even be started (not a program for this machine).
    eprintln!("rook-worker: can't start {}: {e}", target.display());
    if want.is_some() && core.state()["trial"]["version"].as_str() == want.as_deref() {
        core.abandon(&format!("can't start: {e}"));
    }
}

/// Start again through the base, which picks the core.
pub fn restart() {
    let base = std::env::var(BASE)
        .map(PathBuf::from)
        .or_else(|_| std::env::current_exe())
        .expect("own path");
    let args: Vec<String> = std::env::args().skip(1).collect();
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let e = std::process::Command::new(&base).args(args).exec();
        eprintln!("rook-worker: restart failed: {e}");
    }
    #[cfg(windows)]
    {
        if let Err(e) = std::process::Command::new(&base).args(args).spawn() {
            eprintln!("rook-worker: restart failed: {e}");
            return;
        }
        std::process::exit(0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn core(dir: &Path) -> Core {
        Core {
            root: dir.join("core"),
            running: None,
        }
    }

    #[test]
    fn a_trial_gets_three_boots_then_current_runs_again() {
        let dir = std::env::temp_dir().join(format!("rook-core-{}", crate::link::hex(&crate::link::random::<4>())));
        let c = core(&dir);
        let sig = json!({"version": "0.5.1", "hub": "h", "files": {}});
        c.receive(&sig, b"bin".to_vec()).unwrap();
        assert!(c
            .receive(&sig, b"bin".to_vec())
            .unwrap_err()
            .contains("already waiting"));
        for _ in 0..3 {
            assert_eq!(c.choose(None).as_deref(), Some("0.5.1"));
        }
        assert_eq!(c.choose(None), None);
        assert_eq!(c.state()["failed"]["version"], "0.5.1");
        let on = Core {
            root: c.root.clone(),
            running: Some("0.5.1".into()),
        };
        c.receive(&sig, b"bin".to_vec()).unwrap();
        on.confirm();
        assert_eq!(
            (on.state()["current"].clone(), on.state()["trial"].clone()),
            (json!("0.5.1"), Value::Null)
        );
        assert!(c.switch("0.5.1").unwrap_err().contains("already on"));
        assert_eq!(c.switch(INSTALLED).unwrap()["from"], "0.5.1");
        assert_eq!(c.choose(None), None);
        assert_eq!(c.switch("0.5.1").unwrap()["version"], "0.5.1");
        fs::remove_dir_all(dir).unwrap();
    }
}
