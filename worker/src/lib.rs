//! The Rook worker: the minimal worker profile (docs/spec/worker-profile.md)
//! over one band: join, announce its caps, answer calls, heartbeat. The caps
//! are the repo's own manifests (see `manifests`), served by `caps`; the
//! stream index hears about this worker's streams through `core.event`.

pub mod caps;
pub mod config;
pub mod drop;
pub mod link;
pub mod manifests;
pub mod streams;

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use tokio::sync::{oneshot, Notify};

use caps::{file, shell, Args, Reply};
use config::Config;
use link::Link;
use manifests::Manifest;
use streams::Streams;

pub const CORE: &str = "2.0";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
const HEARTBEAT: f64 = 30.0;
/// The hub shows a worker offline after this long without hearing it; a hub
/// heard again after as long may have restarted, so the worker announces again.
const HUB_SILENCE: Duration = Duration::from_secs(90);
const RESTART_DELAY: Duration = Duration::from_millis(500);
/// core.event: events per request, how long the hub gets to answer, events
/// kept while it is away (the oldest go first), stream ids one sync lists.
const BATCH: usize = 50;
const EVENT_TIMEOUT: Duration = Duration::from_secs(15);
const MAX_QUEUED: usize = 1000;
const SYNC_IDS: usize = 500;

/// Caps of the announced manifests this worker doesn't serve, and why.
const UNAVAILABLE: [(&str, &str); 2] = [
    ("worker.log", "the hub's journal has every call this worker ran: read it there"),
    ("worker.update", "not in the Rust worker yet"),
];

pub struct Worker {
    cfg: Config,
    link: Link,
    band: String,
    manifests: Vec<Manifest>,
    entries: Value,
    started: f64,
    streams: Arc<Streams>,
    outbox: Arc<Outbox>,
    /// The hub's node id and when its announce was last heard.
    hub: Mutex<Option<(String, Instant)>>,
    waiting: Mutex<HashMap<String, oneshot::Sender<Value>>>,
    replies: drop::Replies,
}

/// Events for the hub, sent in order as `core.event` batches.
#[derive(Default)]
struct Outbox {
    queue: Mutex<VecDeque<Value>>,
    dropped: Mutex<u64>,
    wake: Notify,
}

impl Outbox {
    fn put(&self, event: &str, plugin: &str, payload: Value) {
        let mut q = self.queue.lock().unwrap();
        if q.len() >= MAX_QUEUED {
            q.pop_front();
            *self.dropped.lock().unwrap() += 1;
        }
        q.push_back(json!({"id": id32(), "event": event, "plugin": plugin, "payload": payload}));
        self.wake.notify_one();
    }
}

pub fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

fn id32() -> String {
    link::hex(&link::random::<16>())
}

impl Worker {
    pub async fn start(cfg: Config) -> std::io::Result<Arc<Worker>> {
        let link = Link::connect(&cfg.psk, &cfg.relay).await?;
        let band = link::band_id_hex(&cfg.psk);
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
        let outbox = Arc::new(Outbox::default());
        let streams = {
            let outbox = outbox.clone();
            Streams::new(Box::new(move |change, row| outbox.put(&format!("stream.{change}"), "stream", row)))
        };
        Ok(Arc::new(Worker {
            cfg, link, band, manifests, entries, started: now(), streams, outbox,
            hub: Mutex::new(None), waiting: Mutex::new(HashMap::new()), replies: drop::Replies::default(),
        }))
    }

    /// Announce, then heartbeat, keep the relay's path open, send events and
    /// answer calls until the link fails.
    pub async fn run(self: Arc<Self>) -> std::io::Result<()> {
        self.announce().await;
        let me = self.clone();
        tokio::spawn(async move {
            loop {
                let jitter = 0.8 + 0.4 * (link::random::<1>()[0] as f64 / 255.0);
                tokio::time::sleep(Duration::from_secs_f64(HEARTBEAT * jitter)).await;
                me.send(&json!({"kind": "heartbeat", "worker_id": me.cfg.worker_id, "core": CORE,
                                "manifests_hash": manifests::sha256(&me.entries)})).await;
            }
        });
        let me = self.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(link::KEEPALIVE_EVERY).await;
                let _ = me.link.send(link::KEEPALIVE).await;
            }
        });
        let me = self.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(streams::ACTIVITY_EVERY).await;
                me.streams.flush();
            }
        });
        tokio::spawn(self.clone().send_events());
        let mut reassembler = link::reassembler();
        loop {
            let payload = self.link.recv(&mut reassembler).await?;
            if payload.first() != Some(&b'{') {
                let me = self.clone();
                tokio::spawn(async move { me.replies.feed(&me.link, &payload).await });
                continue;
            }
            let Ok(Value::Object(msg)) = serde_json::from_slice::<Value>(&payload) else { continue };
            let text = |k: &str| msg.get(k).and_then(Value::as_str);
            if text("kind") == Some("announce") && text("name") == Some("rook") {
                self.hub_announced(text("worker_id").unwrap_or_default()).await;
                continue;
            }
            if msg.get("cap").is_none() {
                if let Some(id) = text("id") {
                    if let Some(tx) = self.waiting.lock().unwrap().remove(id) {
                        let _ = tx.send(Value::Object(msg.clone()));
                    }
                }
                continue;
            }
            let (Some(cap), Some(id)) = (text("cap"), text("id")) else { continue };
            if text("target") != Some(self.cfg.worker_id.as_str()) {
                continue;
            }
            let (cap, id) = (cap.to_string(), id.to_string());
            let args = match msg.get("args") {
                Some(Value::Object(a)) => a.clone(),
                _ => Map::new(),
            };
            // Who the call is for, as the ticket says (unchecked): a stream's participants.
            let who: Vec<String> = ["person", "agent"]
                .iter()
                .filter_map(|k| msg.get("ticket").and_then(|t| t[*k].as_str()).map(String::from))
                .collect();
            let drop = msg.get("accept").and_then(Value::as_array).is_some_and(|a| a.contains(&json!("drop")));
            let me = self.clone();
            tokio::spawn(async move {
                let reply = match me.call(&cap, Args(&args), who).await {
                    Ok(result) => json!({"id": id, "from": me.cfg.worker_id, "ok": true, "result": result}),
                    Err(error) => json!({"id": id, "from": me.cfg.worker_id, "ok": false, "error": error}),
                };
                let data = reply.to_string();
                if drop && data.len() > drop::THRESHOLD {
                    let desc = me.replies.serve(&id, &me.cfg.worker_id, data.into_bytes());
                    me.send(&json!({"id": id, "from": me.cfg.worker_id, "ok": reply["ok"], "drop": desc})).await;
                } else if let Err(e) = me.link.send(data.as_bytes()).await {
                    eprintln!("rook-worker: send failed: {e}");
                }
            });
        }
    }

    /// The hub's announce: the first one, or one after a silence, means the
    /// hub may have restarted: announce again and resend the stream index.
    async fn hub_announced(&self, node: &str) {
        let fresh = {
            let mut hub = self.hub.lock().unwrap();
            let fresh = hub.as_ref().is_none_or(|(n, t)| n != node || t.elapsed() > HUB_SILENCE);
            *hub = Some((node.to_string(), Instant::now()));
            fresh
        };
        if fresh {
            self.announce().await;
            let rows = self.streams.rows();
            let ids: Vec<Value> = rows.iter().take(SYNC_IDS).map(|r| r["id"].clone()).collect();
            let complete = rows.len() <= SYNC_IDS;
            for row in rows {
                self.outbox.put("stream.changed", "stream", row);
            }
            self.outbox.put("stream.synced", "stream", json!({"ids": ids, "complete": complete}));
        }
    }

    /// One batch in flight at a time; a batch the hub didn't answer goes
    /// again with the same event ids (the hub drops repeats).
    async fn send_events(self: Arc<Self>) {
        let mut backoff = 1.0;
        loop {
            let node = self.hub.lock().unwrap().as_ref().map(|(n, _)| n.clone());
            let batch: Vec<Value> = self.outbox.queue.lock().unwrap().iter().take(BATCH).cloned().collect();
            let Some(node) = node.filter(|_| !batch.is_empty()) else {
                let _ = tokio::time::timeout(Duration::from_secs(1), self.outbox.wake.notified()).await;
                continue;
            };
            let dropped = std::mem::take(&mut *self.outbox.dropped.lock().unwrap());
            let mut args = json!({"worker_id": self.cfg.worker_id, "events": batch});
            if dropped > 0 {
                args["dropped"] = json!(dropped);
            }
            let id = id32();
            let (tx, rx) = oneshot::channel();
            self.waiting.lock().unwrap().insert(id.clone(), tx);
            self.send(&json!({"id": id, "cap": "core.event", "target": node, "args": args})).await;
            let reply = tokio::time::timeout(EVENT_TIMEOUT, rx).await.ok().and_then(Result::ok);
            self.waiting.lock().unwrap().remove(&id);
            let again = match &reply {
                Some(r) if r["ok"] == json!(true) => false,
                Some(r) if r["retry"] == json!(true) => true,
                Some(r) => {
                    eprintln!("rook-worker: events refused by the hub: {}", r["error"]);
                    false
                }
                None => true,
            };
            if again {
                tokio::time::sleep(Duration::from_secs_f64(backoff)).await;
                backoff = (backoff * 2.0).min(30.0);
                continue;
            }
            backoff = 1.0;
            let mut q = self.outbox.queue.lock().unwrap();
            let sent: Vec<&Value> = batch.iter().map(|e| &e["id"]).collect();
            q.retain(|e| !sent.contains(&&e["id"]));
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

    async fn call(&self, cap: &str, a: Args<'_>, who: Vec<String>) -> Reply {
        if let Some((_, why)) = UNAVAILABLE.iter().find(|(c, _)| *c == cap) {
            return Err(format!("{cap} is unavailable: {why}"));
        }
        let by = who.last().cloned().unwrap_or_default(); // the agent if one acts, else the person
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
            "shell.open" => {
                let (args, streams, band) = (a.0.clone(), self.streams.clone(), self.band.clone());
                blocking(move || {
                    let mut attrs = Map::new();
                    attrs.insert("band".into(), json!(band));
                    shell::open(Args(&args), &streams, who, attrs)
                })
                .await
            }
            "stream.read" => self.streams.read(a).await,
            "stream.write" => self.streams.write(a, &by),
            "stream.list" => self.streams.list(a),
            "stream.close" => self.streams.close_cap(a, &by).await,
            "file.read" | "file.write" | "file.list" | "file.search" | "file.stat" => {
                let (cap, args) = (cap.to_string(), a.0.clone());
                blocking(move || {
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
            }
            _ => Err(format!("unknown capability: {cap}")),
        }
    }

    fn status(&self) -> Value {
        let hub = self.hub.lock().unwrap().as_ref().is_some_and(|(_, t)| t.elapsed() <= HUB_SILENCE);
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
               "plugins": plugins, "unavailable": unavailable, "streams_open": self.streams.open_count(),
               "release": null})
    }
}

async fn blocking(f: impl FnOnce() -> Reply + Send + 'static) -> Reply {
    tokio::task::spawn_blocking(f).await.map_err(|e| e.to_string())?
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
