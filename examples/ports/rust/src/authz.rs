//! Permissions primitives (spec "Permissions"): tiers, domain-separated
//! ed25519 signatures, is_hub grants, signed announces, call tickets.

use crate::canonical::{args_hash, canonical};
use base64::Engine;
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, VecDeque};

pub const TIERS: [&str; 4] = ["read", "write", "exec", "admin"];

pub fn norm_tier(v: Option<&Value>) -> Option<&'static str> {
    let s = v?.as_str()?.trim().to_lowercase();
    let t = match s.as_str() {
        "read" | "r" => "read",
        "write" | "w" => "write",
        "exec" | "x" => "exec",
        "admin" | "a" => "admin",
        _ => return None,
    };
    Some(t)
}

pub fn rank(t: &str) -> usize {
    TIERS.iter().position(|x| *x == t).unwrap_or(2)
}

fn max_tier(ts: &[Option<&'static str>]) -> &'static str {
    ts.iter().flatten().copied().max_by_key(|t| rank(t)).unwrap_or("exec")
}

/// The built-in tier table (cap -> tier) and fixed-tier prefixes.
pub struct TierTable {
    pub table: HashMap<String, &'static str>,
    pub prefixes: Vec<(String, &'static str)>,
}

/// max(builtin, declared); cmd.* fixed; override raises, lowers only with lower.
pub fn effective_tier(t: &TierTable, cap: &str, declared: Option<&Value>, over: Option<&Value>,
                      lower: bool) -> &'static str {
    for (p, tier) in &t.prefixes {
        if cap.starts_with(p.as_str()) {
            return if lower { norm_tier(over).unwrap_or(tier) } else { max_tier(&[Some(tier), norm_tier(over)]) };
        }
    }
    let base = t.table.get(cap).copied();
    let dec = norm_tier(declared);
    let mut tier = if base.is_none() && dec.is_none() { "exec" } else { max_tier(&[base, dec]) };
    if let Some(ov) = norm_tier(over) {
        tier = if lower { ov } else { max_tier(&[Some(tier), Some(ov)]) };
    }
    tier
}

pub const PREFIX_GRANT: &str = "rook-grant-v1\n";
pub const PREFIX_TICKET: &str = "rook-ticket-v1\n";
pub const PREFIX_ANNOUNCE: &str = "rook-announce-v1\n";
pub const GRANT_GRACE: f64 = 3600.0;
pub const ANNOUNCE_FRESH: f64 = 90.0;
pub const TICKET_SKEW: f64 = 300.0;

fn b64(s: &str) -> Option<Vec<u8>> {
    base64::engine::general_purpose::STANDARD.decode(s).ok()
}

fn raw_key(pub_key: &str) -> Option<Vec<u8>> {
    b64(pub_key.strip_prefix("ed25519:").unwrap_or(pub_key))
}

pub fn key_id(pub_key: &str) -> String {
    let raw = raw_key(pub_key).unwrap_or_default();
    let d = Sha256::digest(raw);
    d.iter().take(8).map(|b| format!("{:02x}", b)).collect()
}

pub fn verify_sig(pub_key: &str, prefix: &str, body: &Value, sig: Option<&Value>) -> bool {
    let (Some(raw), Some(sig)) = (raw_key(pub_key), sig.and_then(Value::as_str).and_then(b64)) else {
        return false;
    };
    let (Ok(k), Ok(s)) = (<[u8; 32]>::try_from(raw.as_slice()), <[u8; 64]>::try_from(sig.as_slice())) else {
        return false;
    };
    let Ok(vk) = VerifyingKey::from_bytes(&k) else { return false };
    let msg = format!("{}{}", prefix, canonical(body));
    vk.verify_strict(msg.as_bytes(), &Signature::from_bytes(&s)).is_ok()
}

fn without(obj: &Map<String, Value>, key: &str) -> Value {
    Value::Object(obj.iter().filter(|(k, _)| k.as_str() != key).map(|(k, v)| (k.clone(), v.clone())).collect())
}

fn int(v: Option<&Value>) -> Option<f64> {
    match v? {
        Value::Number(n) => n.as_f64().map(f64::trunc),
        Value::String(s) => s.trim().parse::<i64>().ok().map(|x| x as f64),
        _ => None,
    }
}

pub struct GrantCtx<'a> {
    pub band: Option<&'a str>,
    pub now: f64,
    pub revoked: &'a [String],
    pub worker_id: Option<&'a str>,
}

pub fn verify_grant(g: &Value, anchors: &[String], c: &GrantCtx) -> (bool, &'static str) {
    let Some(o) = g.as_object() else { return (false, "not a v1 grant") };
    if o.get("typ").and_then(Value::as_str) != Some("rook-grant") || o.get("v").and_then(Value::as_i64) != Some(1) {
        return (false, "not a v1 grant");
    }
    let iss = o.get("iss").and_then(Value::as_str).unwrap_or("");
    let Some(root) = anchors.iter().find(|a| !a.is_empty() && key_id(a) == iss) else {
        return (false, "issuer is not a trusted root");
    };
    if !verify_sig(root, PREFIX_GRANT, &without(o, "sig"), o.get("sig")) {
        return (false, "bad grant signature");
    }
    let nbf = if o.contains_key("nbf") { int(o.get("nbf")) } else { Some(0.0) };
    let exp = if o.contains_key("exp") { int(o.get("exp")) } else { Some(0.0) };
    let (Some(nbf), Some(exp)) = (nbf, exp) else { return (false, "bad validity window") };
    if c.now + GRANT_GRACE < nbf || c.now > exp + GRANT_GRACE {
        return (false, "grant expired or not yet valid");
    }
    if o.get("role").and_then(Value::as_str) != Some("is_hub") {
        return (false, "unknown role");
    }
    let in_scope = o.get("scope").and_then(|s| s.get("bands")).and_then(Value::as_array)
        .map(|b| b.iter().any(|x| Some(x.as_str().unwrap_or("")) == c.band))
        .unwrap_or(false);
    if c.band.is_some() && !in_scope {
        return (false, "band not in grant scope");
    }
    if let Some(serial) = o.get("serial").and_then(Value::as_str) {
        if c.revoked.iter().any(|r| r == serial) {
            return (false, "grant revoked");
        }
    }
    let sub = o.get("sub").cloned().unwrap_or(Value::Null);
    let (Some(key), kid) = (sub.get("key").and_then(Value::as_str), sub.get("kid").and_then(Value::as_str)) else {
        return (false, "bad grant subject");
    };
    if Some(key_id(key).as_str()) != kid {
        return (false, "bad grant subject");
    }
    if let (Some(want), Some(bound)) = (c.worker_id.filter(|w| !w.is_empty()),
                                        sub.get("worker_id").and_then(Value::as_str).filter(|w| !w.is_empty())) {
        if want != bound {
            return (false, "grant bound to another worker");
        }
    }
    match o.get("name") {
        None | Some(Value::Null) => {}
        Some(n) if n.as_str() == Some("rook") => {}
        _ => return (false, "is_hub grant with a non-reserved name"),
    }
    (true, "ok")
}

fn announce_body(m: &Value) -> Value {
    let g = |k: &str| m.get(k).cloned().unwrap_or(Value::Null);
    let caps = match m.get("caps") {
        Some(Value::Array(a)) => Value::Array(a.clone()),
        _ => Value::Array(vec![]),
    };
    serde_json::json!({"worker_id": g("worker_id"), "name": g("name"), "caps": caps, "ts": g("ts"), "seq": g("seq")})
}

pub fn verify_announce(m: &Value, grant: &Value, now: f64) -> bool {
    let (Some(asig), sub) = (m.get("asig").and_then(Value::as_object), grant.get("sub")) else { return false };
    let kid = sub.and_then(|s| s.get("kid"));
    if asig.get("kid") != kid || kid.is_none() {
        return false;
    }
    let Some(ts) = int(m.get("ts")) else { return false };
    if (now - ts).abs() > ANNOUNCE_FRESH {
        return false;
    }
    let key = sub.and_then(|s| s.get("key")).and_then(Value::as_str).unwrap_or("");
    verify_sig(key, PREFIX_ANNOUNCE, &announce_body(m), asig.get("sig"))
}

/// Roles an announce proves: a verified grant AND an announce signed by its key.
pub fn held_roles(m: &Value, anchors: &[String], band: Option<&str>, now: f64) -> Vec<String> {
    let Some(grants) = m.get("grants").and_then(Value::as_array) else { return vec![] };
    let wid = m.get("worker_id").and_then(Value::as_str).filter(|w| !w.is_empty());
    let mut out: Vec<String> = vec![];
    for g in grants.iter().take(4) {
        let c = GrantCtx { band, now, revoked: &[], worker_id: wid };
        if verify_grant(g, anchors, &c).0 && verify_announce(m, g, now) {
            let role = g["role"].as_str().unwrap_or("").to_string();
            if !out.contains(&role) {
                out.push(role);
            }
        }
    }
    out.sort();
    out
}

/// Message ids seen within the ticket window (bounded, oldest first).
pub struct ReplayCache {
    seen: VecDeque<(String, f64)>,
    max: usize,
    window: f64,
}

impl Default for ReplayCache {
    fn default() -> Self {
        ReplayCache { seen: VecDeque::new(), max: 10_000, window: 30.0 + TICKET_SKEW }
    }
}

impl ReplayCache {
    pub fn check(&mut self, id: &str, now: f64) -> bool {
        while let Some((_, t)) = self.seen.front() {
            if now - t > self.window || self.seen.len() > self.max {
                self.seen.pop_front();
            } else {
                break;
            }
        }
        if self.seen.iter().any(|(k, _)| k == id) {
            return true;
        }
        self.seen.push_back((id.to_string(), now));
        if self.seen.len() > self.max {
            self.seen.pop_front();
        }
        false
    }
}

pub struct TicketEnv<'a> {
    pub cap: &'a str,
    pub target: &'a str,
    pub msg_id: &'a str,
    pub args: &'a Value,
    pub keys: &'a Map<String, Value>,
    pub now: f64,
}

pub fn verify_ticket(t: &Value, e: &TicketEnv, replay: Option<&mut ReplayCache>) -> (bool, &'static str) {
    let Some(o) = t.as_object().filter(|o| o.get("v").and_then(Value::as_i64) == Some(1)) else {
        return (false, "no ticket");
    };
    let kid = o.get("kid").and_then(Value::as_str).unwrap_or("");
    let Some(grant) = e.keys.get(kid) else { return (false, "unknown ticket key") };
    let body = without(o, "grant");
    let pub_key = grant.get("sub").and_then(|s| s.get("key")).and_then(Value::as_str).unwrap_or("");
    if !verify_sig(pub_key, PREFIX_TICKET, &without(body.as_object().unwrap(), "sig"), body.get("sig")) {
        return (false, "bad ticket signature");
    }
    let s = |k: &str| body.get(k).and_then(Value::as_str);
    if s("t") != Some(e.target) {
        return (false, "ticket for another worker");
    }
    if s("id") != Some(e.msg_id) {
        return (false, "ticket for another message");
    }
    if s("cap") != Some(e.cap) {
        return (false, "ticket for another cap");
    }
    if s("ah") != Some(args_hash(e.args).as_str()) {
        return (false, "ticket args mismatch");
    }
    let (Some(iat), Some(exp)) = (int(body.get("iat")), int(body.get("exp"))) else {
        return (false, "bad ticket window");
    };
    if !(iat - TICKET_SKEW <= e.now && e.now <= exp + TICKET_SKEW) {
        return (false, "ticket expired");
    }
    let max_t = norm_tier(grant.get("constraints").and_then(|c| c.get("max_tier"))).unwrap_or("admin");
    if rank(norm_tier(body.get("tier")).unwrap_or("exec")) > rank(max_t) {
        return (false, "ticket tier above grant constraint");
    }
    if let Some(r) = replay {
        if r.check(e.msg_id, e.now) {
            return (false, "ticket replayed");
        }
    }
    (true, "ok")
}
