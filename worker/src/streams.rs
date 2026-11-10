//! Streams on this worker (rook/streams, the bounded part): terminals,
//! processes and sockets, each a ring of events kept in memory, read by
//! cursor with a long-poll, written to with input the producer takes. The
//! hub's stream index hears every change through `report` (stream.changed,
//! stream.deleted), activity at most every 5 s.

use std::collections::{HashMap, VecDeque};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use serde_json::{json, Map, Value};
use tokio::sync::Notify;

use crate::caps::{Args, Reply};

const BUDGET: usize = 256 * 1024;
const KEEP_CLOSED: usize = 32;
const MAX_EVENT: usize = 1 << 20;
const READ_LIMIT: u64 = 1000;
const SIGNALS: [&str; 5] = ["INT", "TERM", "HUP", "QUIT", "KILL"];
pub const ACTIVITY_EVERY: Duration = Duration::from_secs(5);
const CLOSE_GRACE: Duration = Duration::from_secs(3);

pub enum Body {
    Bytes(Vec<u8>),
    Json(Value),
}

struct Event {
    seq: u64,
    at: f64,
    kind: String,
    by: String,
    body: Body,
}

impl Event {
    fn size(&self) -> usize {
        64 + match &self.body {
            Body::Bytes(b) => b.len(),
            Body::Json(v) => v.to_string().len(),
        }
    }
    fn wire(&self) -> Value {
        let mut out = json!({"seq": self.seq, "at": self.at, "type": self.kind, "by": self.by});
        match &self.body {
            Body::Bytes(b) => out["b64"] = json!(B64.encode(b)),
            Body::Json(v) => out["data"] = v.clone(),
        }
        out
    }
}

struct Stream {
    id: String,
    kind: String,
    title: String,
    producer: String,
    participants: Vec<String>,
    attrs: Map<String, Value>,
    types: Vec<String>,
    input: bool,
    created: f64,
    closed: Option<f64>,
    last_seq: u64,
    events: VecDeque<Event>,
    held: usize,
    dirty: bool,
    changed: Arc<Notify>,
    /// The producer's input, while one is attached.
    feed: Option<mpsc::Sender<Value>>,
}

impl Stream {
    fn wire(&self) -> Value {
        json!({"id": self.id, "kind": self.kind, "title": self.title, "producer": self.producer,
               "types": self.types, "input": self.input, "durable": false, "attrs": self.attrs,
               "created": self.created, "closed": self.closed, "last_seq": self.last_seq})
    }

    /// What the hub's index keeps: plain attrs only, strings cut short.
    fn summary(&self) -> Value {
        let mut attrs: Map<String, Value> = self
            .attrs
            .iter()
            .filter(|(k, v)| k.len() <= 64 && (v.is_string() || v.is_number() || v.is_boolean()))
            .take(32)
            .map(|(k, v)| (k.clone(), v.as_str().map_or(v.clone(), |s| json!(s.chars().take(200).collect::<String>()))))
            .collect();
        if let Some(band) = self.attrs.get("band") {
            attrs.insert("band".into(), band.clone());
        }
        let mut out = json!({"id": self.id, "kind": self.kind, "title": self.title, "producer": self.producer,
                             "participants": self.participants.iter().take(100).collect::<Vec<_>>(), "attrs": attrs,
                             "types": self.types, "input": self.input, "durable": false, "created": self.created,
                             "last_seq": self.last_seq, "last_at": self.events.back().map_or(self.created, |e| e.at),
                             "size": self.held});
        if let Some(closed) = self.closed {
            out["closed"] = json!(closed);
        }
        out
    }
}

type Report = Box<dyn Fn(&str, Value) + Send + Sync>;

pub struct Streams {
    all: Mutex<HashMap<String, Stream>>,
    report: Report,
}

pub struct Opened {
    pub id: String,
    pub input: mpsc::Receiver<Value>,
}

impl Streams {
    /// `report(change, row)`: "changed" with the index row, "deleted" with `{id}`.
    pub fn new(report: Report) -> Arc<Streams> {
        Arc::new(Streams { all: Mutex::new(HashMap::new()), report })
    }

    /// A new stream with its producer attached: the caller writes output
    /// with `append` and takes input from `Opened::input`.
    pub fn open(&self, kind: &str, title: &str, producer: &str, participants: Vec<String>, attrs: Map<String, Value>) -> Opened {
        let id = format!("s_{}", crate::link::hex(&crate::link::random::<8>()));
        let (tx, rx) = mpsc::channel();
        let mut all = self.all.lock().unwrap();
        let mut closed: Vec<(f64, String)> = all.values().filter_map(|s| s.closed.map(|c| (c, s.id.clone()))).collect();
        closed.sort_by(|a, b| a.0.total_cmp(&b.0));
        for (_, old) in closed.iter().take(closed.len().saturating_sub(KEEP_CLOSED - 1)) {
            all.remove(old);
            (self.report)("deleted", json!({"id": old}));
        }
        let s = Stream {
            id: id.clone(),
            kind: kind.into(),
            title: title.chars().take(200).collect(),
            producer: producer.into(),
            participants,
            attrs,
            types: vec!["bytes".into()],
            input: true,
            created: crate::now(),
            closed: None,
            last_seq: 0,
            events: VecDeque::new(),
            held: 0,
            dirty: false,
            changed: Arc::new(Notify::new()),
            feed: Some(tx),
        };
        (self.report)("changed", s.summary());
        all.insert(id.clone(), s);
        Opened { id, input: rx }
    }

    pub fn update(&self, sid: &str, attrs: Map<String, Value>) {
        let mut all = self.all.lock().unwrap();
        if let Some(s) = all.get_mut(sid) {
            s.attrs.extend(attrs);
            (self.report)("changed", s.summary());
        }
    }

    pub fn append(&self, sid: &str, kind: &str, body: Body, by: &str) -> Result<u64, String> {
        let mut all = self.all.lock().unwrap();
        let s = all.get_mut(sid).ok_or(format!("no stream {sid}"))?;
        if s.closed.is_some() {
            return Err(format!("stream {sid} is closed"));
        }
        if !(s.types.iter().any(|t| t == kind) || kind == "status" || (kind == "input" && s.input)) {
            return Err(format!("stream {sid} ({}) does not take {kind} events", s.kind));
        }
        if let (Body::Json(v), "input") = (&body, kind) {
            check_input(v)?;
        }
        let ev = Event { seq: s.last_seq + 1, at: crate::now(), kind: kind.into(), by: by.into(), body };
        if ev.size() - 64 > MAX_EVENT {
            return Err(format!("event larger than {MAX_EVENT} bytes; split it"));
        }
        if kind == "input" {
            if let (Some(feed), Body::Json(v)) = (&s.feed, &ev.body) {
                let _ = feed.send(v.clone());
            }
        }
        s.last_seq = ev.seq;
        s.held += ev.size();
        s.events.push_back(ev);
        while s.held > BUDGET && s.events.len() > 1 {
            let gone = s.events.pop_front().unwrap();
            s.held -= gone.size();
        }
        s.dirty = true;
        s.changed.notify_waiters();
        Ok(s.last_seq)
    }

    /// Finish a stream: a last `status` event `{state: closed, ..status}`;
    /// it stays readable. The producer's input ends.
    pub fn close(&self, sid: &str, by: &str, status: Map<String, Value>) {
        let mut all = self.all.lock().unwrap();
        let Some(s) = all.get_mut(sid) else { return };
        if s.closed.is_some() {
            return;
        }
        let mut body = status;
        body.insert("state".into(), json!("closed"));
        let now = crate::now();
        s.last_seq += 1;
        let ev = Event { seq: s.last_seq, at: now, kind: "status".into(), by: by.into(), body: Body::Json(Value::Object(body)) };
        s.held += ev.size();
        s.events.push_back(ev);
        s.closed = Some(now);
        s.feed = None;
        s.dirty = false;
        s.changed.notify_waiters();
        (self.report)("changed", s.summary());
    }

    pub fn open_count(&self) -> usize {
        self.all.lock().unwrap().values().filter(|s| s.closed.is_none()).count()
    }

    /// Every stream as index rows (a resync after hub contact).
    pub fn rows(&self) -> Vec<Value> {
        let all = self.all.lock().unwrap();
        let mut rows: Vec<&Stream> = all.values().collect();
        rows.sort_by(|a, b| a.created.total_cmp(&b.created));
        rows.into_iter().map(Stream::summary).collect()
    }

    /// Activity since the last flush, reported once (every ACTIVITY_EVERY).
    pub fn flush(&self) {
        let mut all = self.all.lock().unwrap();
        for s in all.values_mut().filter(|s| s.dirty) {
            s.dirty = false;
            (self.report)("changed", s.summary());
        }
    }

    fn read_now(&self, sid: &str, after: u64, limit: u64, types: &Option<Vec<String>>) -> Result<(Value, bool), String> {
        let all = self.all.lock().unwrap();
        let s = all.get(sid).ok_or(format!("no stream {sid}"))?;
        let first = s.events.front().map_or(s.last_seq + 1, |e| e.seq);
        let mut after = after.min(s.last_seq);
        let mut dropped = 0;
        if after + 1 < first {
            dropped = first - 1 - after;
            after = first - 1;
        }
        let mut out = vec![];
        let mut next = s.last_seq;
        for ev in s.events.iter().filter(|e| e.seq > after && types.as_ref().is_none_or(|t| t.contains(&e.kind))) {
            out.push(ev.wire());
            if out.len() as u64 >= limit {
                next = ev.seq;
                break;
            }
        }
        let empty = out.is_empty();
        Ok((json!({"events": out, "next": next, "dropped": dropped, "closed": s.closed.is_some()}), empty && s.closed.is_none()))
    }

    // -- the stream caps (rook/core2/builtin/stream.yaml) ----------------------------

    pub async fn read(&self, a: Args<'_>) -> Reply {
        let sid = a.need("stream")?;
        let after = a.int("after", 0)?;
        let limit = a.int("limit", 200)?.clamp(1, READ_LIMIT);
        let wait = a.float("wait", 0.0)?;
        let types = a.strings("types")?.filter(|t| !t.is_empty());
        let changed = self.all.lock().unwrap().get(&sid).map(|s| s.changed.clone()).ok_or(format!("no stream {sid} of yours on this worker"))?;
        let woke = changed.notified();
        tokio::pin!(woke);
        woke.as_mut().enable(); // before the read: an append in between is never missed
        let (first, idle) = self.read_now(&sid, after, limit, &types)?;
        if !idle || wait <= 0.0 || tokio::time::timeout(Duration::from_secs_f64(wait), woke).await.is_err() {
            return Ok(first);
        }
        let (mut out, _) = self.read_now(&sid, first["next"].as_u64().unwrap(), limit, &types)?;
        out["dropped"] = json!(out["dropped"].as_u64().unwrap() + first["dropped"].as_u64().unwrap());
        Ok(out)
    }

    pub fn write(&self, a: Args<'_>, by: &str) -> Reply {
        let sid = a.need("stream")?;
        let kind = a.str("type")?.unwrap_or_else(|| "input".into());
        if kind != "input" {
            return Err("type must be input".into());
        }
        let body = a.0.get("body").cloned().ok_or("body is required")?;
        let seq = self.append(&sid, "input", Body::Json(body), by)?;
        Ok(json!({"seq": seq}))
    }

    pub fn list(&self, a: Args<'_>) -> Reply {
        let kind = a.str("kind")?;
        let open = a.bool("open", false)?;
        let all = self.all.lock().unwrap();
        let mut rows: Vec<&Stream> = all
            .values()
            .filter(|s| kind.as_ref().is_none_or(|k| *k == s.kind) && !(open && s.closed.is_some()))
            .collect();
        rows.sort_by(|a, b| a.created.total_cmp(&b.created));
        Ok(json!({"streams": rows.into_iter().map(Stream::wire).collect::<Vec<_>>()}))
    }

    /// Ask the producer to end (HUP), give it CLOSE_GRACE, then close.
    pub async fn close_cap(&self, a: Args<'_>, by: &str) -> Reply {
        let sid = a.need("stream")?;
        let attached = {
            let all = self.all.lock().unwrap();
            let s = all.get(&sid).ok_or(format!("no stream {sid} of yours on this worker"))?;
            s.closed.is_none() && s.feed.is_some()
        };
        if attached {
            self.append(&sid, "input", Body::Json(json!({"signal": "HUP"})), by)?;
            let deadline = tokio::time::Instant::now() + CLOSE_GRACE;
            while self.closed(&sid).is_none() && tokio::time::Instant::now() < deadline {
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        }
        if self.closed(&sid).is_none() {
            self.close(&sid, by, Map::new());
        }
        Ok(json!({"stream": sid, "closed": self.closed(&sid)}))
    }

    fn closed(&self, sid: &str) -> Option<f64> {
        self.all.lock().unwrap().get(sid).and_then(|s| s.closed)
    }
}

fn check_input(body: &Value) -> Result<(), String> {
    let Value::Object(m) = body else { return Err("an input event needs an object body".into()) };
    let keys: Vec<&str> = ["text", "signal", "resize"].into_iter().filter(|k| m.contains_key(*k)).collect();
    match keys.as_slice() {
        ["text"] if m["text"].is_string() => Ok(()),
        ["text"] => Err("input text must be a string".into()),
        ["signal"] if m["signal"].as_str().is_some_and(|s| SIGNALS.contains(&s)) => Ok(()),
        ["signal"] => Err(format!("signal must be one of {}", SIGNALS.join(", "))),
        ["resize"] => match m["resize"].as_array() {
            Some(r) if r.len() == 2 && r.iter().all(|x| x.as_u64().is_some_and(|n| n > 0)) => Ok(()),
            _ => Err("resize is [cols, rows]".into()),
        },
        _ => Err("an input event has exactly one of text, signal, resize".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn quiet() -> Arc<Streams> {
        Streams::new(Box::new(|_, _| {}))
    }

    fn args(v: Value) -> Map<String, Value> {
        v.as_object().unwrap().clone()
    }

    #[tokio::test]
    async fn a_reader_waits_for_output_and_sees_what_it_missed() {
        let s = quiet();
        let o = s.open("process", "t", "shell.open", vec![], Map::new());
        let reader = {
            let (s, id) = (s.clone(), o.id.clone());
            tokio::spawn(async move { s.read(Args(&args(json!({"stream": id, "wait": 5})))).await })
        };
        tokio::time::sleep(Duration::from_millis(50)).await;
        s.append(&o.id, "bytes", Body::Bytes(b"hi".to_vec()), "p").unwrap();
        let got = reader.await.unwrap().unwrap();
        assert_eq!(got["events"][0]["b64"], "aGk=");
        assert_eq!(got["next"], 1);
        for _ in 0..400 {
            s.append(&o.id, "bytes", Body::Bytes(vec![b'x'; 1000]), "p").unwrap();
        }
        let got = s.read(Args(&args(json!({"stream": o.id, "after": 1, "limit": 1})))).await.unwrap();
        assert!(got["dropped"].as_u64().unwrap() > 0);
    }

    #[tokio::test]
    async fn input_reaches_the_producer_and_close_ends_it() {
        let s = quiet();
        let o = s.open("terminal", "t", "shell.open", vec![], Map::new());
        s.write(Args(&args(json!({"stream": o.id, "body": {"text": "ls\r"}}))), "me").unwrap();
        assert_eq!(o.input.recv().unwrap(), json!({"text": "ls\r"}));
        assert!(s.write(Args(&args(json!({"stream": o.id, "body": {"signal": "NOPE"}}))), "me").is_err());
        s.close(&o.id, "p", args(json!({"exit": 0})));
        assert!(o.input.recv().is_err());
        let got = s.read(Args(&args(json!({"stream": o.id})))).await.unwrap();
        assert_eq!(got["closed"], true);
        assert_eq!(got["events"].as_array().unwrap().last().unwrap()["data"], json!({"exit": 0, "state": "closed"}));
    }
}
