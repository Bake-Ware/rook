//! The Rook worker: the minimal worker profile (docs/spec/worker-profile.md)
//! over one band: join, announce its caps, answer calls, heartbeat. The caps
//! are the repo's own manifests (see `manifests`), served by `caps`.

pub mod caps;
pub mod config;
pub mod link;
pub mod manifests;

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use caps::{file, shell, Args, Reply};
use config::Config;
use link::Link;
use manifests::Manifest;

pub const CORE: &str = "2.0";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
const HEARTBEAT: f64 = 30.0;
/// The hub shows a worker offline after this long without hearing it; a hub
/// heard again after as long may have restarted, so the worker announces again.
const HUB_SILENCE: Duration = Duration::from_secs(90);
const RESTART_DELAY: Duration = Duration::from_millis(500);

/// Caps of the announced manifests this worker doesn't serve, and why.
const UNAVAILABLE: [(&str, &str); 3] = [
    ("worker.log", "the hub's journal has every call this worker ran: read it there"),
    ("worker.update", "not in the Rust worker yet"),
    ("shell.open", "not in the Rust worker yet"),
];

pub struct Worker {
    cfg: Config,
    link: Link,
    manifests: Vec<Manifest>,
    entries: Value,
    started: f64,
    hub_heard: Mutex<Option<Instant>>,
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

impl Worker {
    pub async fn start(cfg: Config) -> std::io::Result<Arc<Worker>> {
        let link = Link::connect(&cfg.psk, &cfg.relay).await?;
        let manifests = manifests::served();
        let entries = Value::Array(
            manifests
                .iter()
                .map(|m| {
                    let mut e = json!({"plugin": m.plugin, "version": m.version, "hash": m.hash});
                    if m.raw["builtin"] == json!(true) {
                        e["builtin"] = json!(true);
                    }
                    e
                })
                .collect(),
        );
        Ok(Arc::new(Worker { cfg, link, manifests, entries, started: now(), hub_heard: Mutex::new(None) }))
    }

    /// Announce, then heartbeat, keep the relay's path open and answer calls until the link fails.
    pub async fn run(self: Arc<Self>) -> std::io::Result<()> {
        self.announce().await;
        let beat = self.clone();
        tokio::spawn(async move {
            loop {
                let jitter = 0.8 + 0.4 * (link::random::<1>()[0] as f64 / 255.0);
                tokio::time::sleep(Duration::from_secs_f64(HEARTBEAT * jitter)).await;
                beat.send(&json!({"kind": "heartbeat", "worker_id": beat.cfg.worker_id, "core": CORE,
                                  "manifests_hash": manifests::sha256(&beat.entries)})).await;
            }
        });
        let keep = self.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(link::KEEPALIVE_EVERY).await;
                let _ = keep.link.send(link::KEEPALIVE).await;
            }
        });
        let mut reassembler = link::reassembler();
        loop {
            let payload = self.link.recv(&mut reassembler).await?;
            let Ok(Value::Object(msg)) = serde_json::from_slice::<Value>(&payload) else { continue };
            if msg.get("kind").and_then(Value::as_str) == Some("announce") && msg.get("name").and_then(Value::as_str) == Some("rook") {
                let first = {
                    let mut heard = self.hub_heard.lock().unwrap();
                    let first = heard.is_none_or(|t| t.elapsed() > HUB_SILENCE);
                    *heard = Some(Instant::now());
                    first
                };
                if first {
                    self.announce().await;
                }
                continue;
            }
            let (Some(Value::String(cap)), Some(Value::String(id))) = (msg.get("cap"), msg.get("id")) else { continue };
            if msg.get("target").and_then(Value::as_str) != Some(self.cfg.worker_id.as_str()) {
                continue;
            }
            let (cap, id) = (cap.clone(), id.clone());
            let args = match msg.get("args") {
                Some(Value::Object(a)) => a.clone(),
                _ => Map::new(),
            };
            let me = self.clone();
            tokio::spawn(async move {
                let reply = match me.call(&cap, Args(&args)).await {
                    Ok(result) => json!({"id": id, "from": me.cfg.worker_id, "ok": true, "result": result}),
                    Err(error) => json!({"id": id, "from": me.cfg.worker_id, "ok": false, "error": error}),
                };
                me.send(&reply).await;
            });
        }
    }

    async fn send(&self, msg: &Value) {
        if let Err(e) = self.link.send(msg.to_string().as_bytes()).await {
            eprintln!("rook-worker: send failed: {e}");
        }
    }

    async fn announce(&self) {
        let unavailable: Map<String, Value> = UNAVAILABLE.iter().map(|(c, why)| (c.to_string(), json!(why))).collect();
        self.send(&json!({"kind": "announce", "worker_id": self.cfg.worker_id, "name": self.cfg.name, "core": CORE,
                          "version": VERSION, "facts": facts(), "manifests": self.entries,
                          "manifests_hash": manifests::sha256(&self.entries), "unavailable": unavailable}))
            .await;
    }

    async fn call(&self, cap: &str, a: Args<'_>) -> Reply {
        if let Some((_, why)) = UNAVAILABLE.iter().find(|(c, _)| *c == cap) {
            return Err(format!("{cap} is unavailable: {why}"));
        }
        match cap {
            "core.manifests" => {
                let wanted = a.strings("plugins")?;
                let raw = self.manifests.iter().filter(|m| wanted.as_ref().is_none_or(|w| w.is_empty() || w.contains(&m.plugin)));
                Ok(json!({"manifests": raw.map(|m| m.raw.clone()).collect::<Vec<_>>()}))
            }
            "worker.ping" => Ok(json!({"pong": true, "at": now()})),
            "worker.status" => Ok(self.status()),
            "worker.restart" => {
                tokio::spawn(async {
                    tokio::time::sleep(RESTART_DELAY).await;
                    restart();
                });
                Ok(json!({"restarting": true}))
            }
            "shell.exec" => shell::exec(a).await,
            "shell.which" => shell::which(a),
            "shell.env" => shell::env(a),
            "file.read" | "file.write" | "file.list" | "file.search" | "file.stat" => {
                let (cap, args) = (cap.to_string(), Map::clone(a.0));
                tokio::task::spawn_blocking(move || {
                    let a = Args(&args);
                    match cap.as_str() {
                        "file.read" => file::read(a),
                        "file.write" => file::write(a),
                        "file.list" => file::list(a),
                        "file.search" => file::search(a),
                        _ => file::stat(a),
                    }
                })
                .await
                .map_err(|e| e.to_string())?
            }
            _ => Err(format!("unknown capability: {cap}")),
        }
    }

    fn status(&self) -> Value {
        let hub = self.hub_heard.lock().unwrap().is_some_and(|t| t.elapsed() <= HUB_SILENCE);
        let plugins: Vec<String> = self
            .manifests
            .iter()
            .filter(|m| m.raw["builtin"] != json!(true))
            .map(|m| format!("{}@{}", m.plugin, m.version))
            .collect();
        let unavailable: Map<String, Value> = UNAVAILABLE.iter().map(|(c, why)| (c.to_string(), json!(why))).collect();
        json!({"name": self.cfg.name, "worker_id": self.cfg.worker_id, "version": VERSION, "core": CORE,
               "facts": facts(), "pid": std::process::id(), "started": (self.started * 1000.0).round() / 1000.0,
               "uptime_s": (now() - self.started) as u64, "band": {"connected": true, "hub_contact": hub},
               "plugins": plugins, "unavailable": unavailable, "streams_open": 0, "release": null})
    }
}

fn facts() -> Value {
    let os = if cfg!(windows) { "windows" } else { "linux" };
    let cpus = std::thread::available_parallelism().map_or(1, |n| n.get());
    json!({"os": os, "arch": std::env::consts::ARCH, "cpus": cpus})
}

/// Replace this process with a fresh one on the same command line.
fn restart() {
    let exe = std::env::current_exe().expect("own path");
    let args: Vec<String> = std::env::args().skip(1).collect();
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let e = std::process::Command::new(exe).args(args).exec();
        eprintln!("rook-worker: restart failed: {e}");
    }
    #[cfg(windows)]
    {
        if let Err(e) = std::process::Command::new(exe).args(args).spawn() {
            eprintln!("rook-worker: restart failed: {e}");
            return;
        }
        std::process::exit(0);
    }
}
