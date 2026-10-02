//! Example worker: joins a band through the relay, announces a few caps,
//! answers calls and posts to hub chat rooms. Configured by environment
//! (the conformance contract, conformance/README.md):
//!   ROOK_RELAY=host:port  ROOK_PSK=...  ROOK_NAME=rs-worker
//!   ROOK_IDENTITY=agent:rs-worker  ROOK_ANCHOR=<root pubkey b64>  ROOK_ANNOUNCE_SECS=30

use rook_port::band::{random, Link};
use rook_port::registry::{Cap, CapError, Meta, Param};
use rook_port::worker::{hex_id, Peer, Worker};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;

fn env(k: &str) -> Option<String> {
    std::env::var(k).ok().filter(|v| !v.is_empty())
}

fn cap(doc: &str, risk: &'static str, params: Vec<Param>,
       f: impl Fn(&serde_json::Map<String, Value>) -> Result<Value, CapError> + Send + Sync + 'static) -> Cap {
    Cap { handler: Arc::new(f), params, doc: doc.into(), meta: Meta { risk: Some(risk), ..Default::default() } }
}

fn main() {
    let (Some(relay), Some(psk)) = (env("ROOK_RELAY"), env("ROOK_PSK")) else {
        eprintln!("set ROOK_RELAY=host:port and ROOK_PSK");
        std::process::exit(2);
    };
    let name = env("ROOK_NAME").unwrap_or_else(|| "rs-worker".into());
    let peer = Peer::new(&env("ROOK_IDENTITY").unwrap_or_else(|| "agent:rs-worker".into()),
                         &env("ROOK_ANCHOR").unwrap_or_default());
    let w = Arc::new(Worker::new(&env("ROOK_WORKER_ID").unwrap_or_else(hex_id), &name, peer.clone()));

    let n = name.clone();
    w.register("port.info", cap("Which implementation this worker is.", "read", vec![],
        move |_| Ok(json!({"language": "rust", "runtime": format!("rook-port {}", env!("CARGO_PKG_VERSION")), "name": n}))));
    w.register("conformance.echo", cap("Return value unchanged.", "read",
        vec![Param::opt("value", Value::Null, "any")], |a| Ok(a.get("value").cloned().unwrap_or(Value::Null))));
    w.register("conformance.add", cap("Return a + b.", "read", vec![Param::req("a", "int"), Param::req("b", "int")],
        |a| match (a["a"].as_i64(), a["b"].as_i64()) {
            (Some(x), Some(y)) => Ok(json!(x + y)),
            _ => match (a["a"].as_f64(), a["b"].as_f64()) {
                (Some(x), Some(y)) => Ok(json!(x + y)),
                _ => Err(CapError::Failed("TypeError".into(), "a and b must be numbers".into())),
            },
        }));
    let p = peer.clone();
    w.register("conformance.chat_post", cap("Post text to a hub chat room (chat.write on worker rook).", "write",
        vec![Param::req("room", "str"), Param::req("text", "str")], move |a| {
            let hub = p.call_rook("chat.write", json!({"action": "send", "room": a["room"], "text": a["text"]}))?;
            Ok(json!({"hub": hub, "rook": *p.rook.lock().unwrap()}))
        }));
    let p = peer.clone();
    w.register("conformance.chat_read", cap("Read a hub chat room (chat.read on worker rook).", "read",
        vec![Param::req("room", "str"), Param::opt("since_seq", json!(0), "int")], move |a| {
            let since = a.get("since_seq").cloned().unwrap_or(json!(0));
            let hub = p.call_rook("chat.read", json!({"action": "read", "room": a["room"], "since_seq": since}))?;
            Ok(json!({"hub": hub, "rook": *p.rook.lock().unwrap()}))
        }));

    let link = Link::connect(&psk, &relay).expect("cannot open the band link");
    *peer.band.lock().unwrap() = link.band.iter().map(|b| format!("{:02x}", b)).collect();
    let l = link.clone();
    peer.set_send(Arc::new(move |m: &Value| {
        if let Err(e) = l.send(m.to_string().as_bytes()) {
            eprintln!("send failed: {e}");
        }
    }));

    // Keepalive (relay registration) and announces, each on its own thread.
    let l = link.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(20));
        let _ = l.send(rook_port::band::KEEPALIVE);
    });
    let facts = json!({"os": std::env::consts::OS, "arch": std::env::consts::ARCH,
                       "cpus": std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)});
    let secs: f64 = env("ROOK_ANNOUNCE_SECS").and_then(|s| s.parse().ok()).unwrap_or(30.0);
    let (wa, pa) = (w.clone(), peer.clone());
    std::thread::spawn(move || loop {
        pa.send(&wa.announce(&facts));
        // ±20% jitter so a fleet started together does not announce in bursts.
        let jitter = 0.8 + (random::<1>()[0] as f64 / 255.0) * 0.4;
        std::thread::sleep(Duration::from_secs_f64(secs * jitter));
    });
    println!("worker up: id={} name={} caps={}", w.worker_id, w.name, w.registry.lock().unwrap().list().join(","));

    link.recv_loop(|payload| {
        let Ok(msg) = serde_json::from_slice::<Value>(&payload) else { return };
        let (w, p) = (w.clone(), peer.clone());
        // One thread per message: a handler that calls another node must not
        // block the receive loop that will deliver that node's reply.
        std::thread::spawn(move || {
            if let Some(reply) = w.handle(&msg) {
                p.send(&reply);
            }
        });
    });
}
