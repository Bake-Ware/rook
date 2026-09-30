//! A Rook worker (spec "Messages" and "Workers"): the server side answers
//! requests for its caps; the `Peer` side calls other nodes (the hub worker
//! "rook" for chat rooms) and matches their replies.

use crate::authz::held_roles;
use crate::band::random;
use crate::registry::{Cap, CapError, Meta, Param, Registry};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub type SendFn = Arc<dyn Fn(&Value) + Send + Sync>;

pub fn hex_id() -> String {
    random::<16>().iter().map(|b| format!("{:02x}", b)).collect()
}

pub fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// The calling side of a band member.
pub struct Peer {
    pub identity: String,
    pub anchor: String,
    pub band: Mutex<String>,
    pub rook: Mutex<Option<Value>>, // {"worker_id", "verified"}
    pending: Mutex<HashMap<String, Sender<Value>>>,
    send: Mutex<Option<SendFn>>,
}

impl Peer {
    pub fn new(identity: &str, anchor: &str) -> Arc<Peer> {
        Arc::new(Peer { identity: identity.into(), anchor: anchor.into(), band: Mutex::new(String::new()),
                        rook: Mutex::new(None), pending: Mutex::new(HashMap::new()), send: Mutex::new(None) })
    }

    pub fn set_send(&self, f: SendFn) {
        *self.send.lock().unwrap() = Some(f);
    }

    pub fn send(&self, msg: &Value) {
        if let Some(f) = self.send.lock().unwrap().clone() {
            f(msg);
        }
    }

    /// A reply to one of our calls; true if it was ours.
    pub fn on_reply(&self, m: &Value) -> bool {
        let id = m.get("id").map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string()));
        match id.and_then(|id| self.pending.lock().unwrap().remove(&id)) {
            Some(tx) => tx.send(m.clone()).is_ok(),
            None => false,
        }
    }

    /// Track the hub worker "rook": with an anchor, only the holder of a
    /// root-signed is_hub grant for this band (proved by a signed announce).
    pub fn on_announce(&self, m: &Value) {
        if m.get("name").and_then(Value::as_str) != Some("rook") {
            return;
        }
        let Some(wid) = m.get("worker_id").and_then(Value::as_str) else { return };
        if self.anchor.is_empty() {
            *self.rook.lock().unwrap() = Some(json!({"worker_id": wid, "verified": false}));
            return;
        }
        let band = self.band.lock().unwrap().clone();
        if held_roles(m, &[self.anchor.clone()], Some(&band), now()).iter().any(|r| r == "is_hub") {
            *self.rook.lock().unwrap() = Some(json!({"worker_id": wid, "verified": true}));
        }
    }

    /// Call a cap on another node and wait for its reply.
    pub fn call(&self, cap: &str, args: Value, target: &str, timeout: Duration) -> Result<Value, CapError> {
        let id = hex_id();
        let (tx, rx) = channel();
        self.pending.lock().unwrap().insert(id.clone(), tx);
        let mut msg = json!({"id": id, "cap": cap, "args": args, "target": target});
        if !self.identity.is_empty() {
            msg["identity"] = json!(self.identity);
        }
        self.send(&msg);
        let got = rx.recv_timeout(timeout);
        self.pending.lock().unwrap().remove(&id);
        got.map_err(|_| CapError::Failed("TimeoutError".into(), format!("{cap}: no reply")))
    }

    /// Call the hub worker "rook" (its announce comes every ~30 s).
    pub fn call_rook(&self, cap: &str, args: Value) -> Result<Value, CapError> {
        let end = Instant::now() + Duration::from_secs(40);
        let target = loop {
            if let Some(r) = self.rook.lock().unwrap().clone() {
                break r["worker_id"].as_str().unwrap_or("").to_string();
            }
            if Instant::now() > end {
                return Err(CapError::failed("hub worker 'rook' not seen on the band"));
            }
            std::thread::sleep(Duration::from_millis(250));
        };
        let r = self.call(cap, args, &target, Duration::from_secs(15))?;
        if r.get("ok") != Some(&Value::Bool(true)) {
            let e = r.get("error").and_then(Value::as_str).unwrap_or("failed");
            return Err(CapError::failed(format!("{cap}: {e}")));
        }
        Ok(r.get("result").cloned().unwrap_or(Value::Null))
    }
}

// Python truthiness of an envelope value ("args": [] or false means {}).
fn falsy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => true,
        Some(Value::Bool(b)) => !b,
        Some(Value::Number(n)) => n.as_f64() == Some(0.0),
        Some(Value::String(s)) => s.is_empty(),
        Some(Value::Array(a)) => a.is_empty(),
        Some(Value::Object(o)) => o.is_empty(),
    }
}

/// The answering side: a registry plus the dispatch rules.
pub struct Worker {
    pub worker_id: String,
    pub name: String,
    pub registry: Arc<Mutex<Registry>>,
    pub peer: Arc<Peer>,
}

impl Worker {
    pub fn new(worker_id: &str, name: &str, peer: Arc<Peer>) -> Worker {
        let registry = Arc::new(Mutex::new(Registry::default()));
        let reg = registry.clone();
        registry.lock().unwrap().register("caps.describe", Cap {
            handler: Arc::new(move |a| {
                let prefix = a.get("prefix").and_then(Value::as_str).unwrap_or("").to_string();
                Ok(reg.lock().unwrap().describe(&prefix))
            }),
            params: vec![Param::opt("prefix", json!(""), "str")],
            doc: "Arg schema + docstring for every capability on this worker.".into(),
            meta: Meta { risk: Some("read"), ..Default::default() },
        });
        Worker { worker_id: worker_id.into(), name: name.into(), registry, peer }
    }

    pub fn register(&self, name: &str, cap: Cap) {
        self.registry.lock().unwrap().register(name, cap);
    }

    pub fn announce(&self, facts: &Value) -> Value {
        let reg = self.registry.lock().unwrap();
        let caps = reg.list();
        let mut plugins: Vec<String> = caps.iter().map(|c| c.split('.').next().unwrap_or("").to_string()).collect();
        plugins.dedup();
        json!({
            "kind": "announce", "worker_id": self.worker_id, "name": self.name, "description": "",
            "caps": caps, "plugins": plugins, "version": "0.1.0-rs", "build": 0, "app_release": {},
            "facts": facts, "tiers": reg.tiers(),
            // This port does not verify hub tickets (mode "off"); see the spec, "Permissions".
            "authz": {"v": 1, "mode": "off", "anchors": [], "kids": []},
        })
    }

    /// Classify one inbound message. Returns the reply to send, if any.
    /// Announces and replies are routed to the peer side.
    pub fn handle(&self, msg: &Value) -> Option<Value> {
        let m = msg.as_object()?;
        let cap = match m.get("cap") {
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            c if !falsy(c) => c.map(|v| v.to_string()).unwrap_or_default(), // non-string cap: never ours
            _ => {
                if m.get("kind").and_then(Value::as_str) == Some("announce") {
                    self.peer.on_announce(msg);
                } else if m.contains_key("id") && m.contains_key("ok") && m.contains_key("from") {
                    self.peer.on_reply(msg);
                }
                return None;
            }
        };
        let target = m.get("target").filter(|t| !falsy(Some(t)));
        let mine = target.is_some_and(|t| t.as_str() == Some(self.worker_id.as_str()));
        if target.is_some() && !mine {
            return None; // addressed to someone else
        }
        let reply = |body: Value| {
            let mut out = Map::new();
            if let Some(id) = m.get("id").filter(|v| !v.is_null()) {
                out.insert("id".into(), id.clone());
            }
            out.insert("from".into(), json!(self.worker_id));
            out.extend(body.as_object().cloned().unwrap_or_default());
            Some(Value::Object(out))
        };
        let registry = self.registry.lock().unwrap().clone(); // don't hold the lock while running
        if !registry.has(&cap) {
            return if mine { reply(json!({"ok": false, "error": format!("unknown capability: {cap}")})) } else { None };
        }
        let empty = Value::Object(Map::new());
        let args = if falsy(m.get("args")) { &empty } else { &m["args"] };
        let Some(args) = args.as_object() else {
            return reply(json!({"ok": false, "error": "args must be an object"}));
        };
        match registry.call(&cap, args) {
            Ok(result) => reply(json!({"ok": true, "result": result})),
            Err(e) => reply(json!({"ok": false, "error": e.reply_text()})),
        }
    }
}
