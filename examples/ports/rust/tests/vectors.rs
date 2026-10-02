//! Runs every offline conformance vector (conformance/vectors/*.json) against
//! this port: `cargo test`. ROOK_VECTORS overrides the directory.

use rook_port::authz::{effective_tier, held_roles, key_id, verify_grant, verify_ticket, GrantCtx, ReplayCache,
                       TicketEnv, TierTable};
use rook_port::band::{band_id, band_key, fragment, open, pack_frame, parse_chunk, seal, unpack_frame, Reassembler};
use rook_port::canonical::{args_hash, canonical};
use rook_port::placement::{compile_placement, evaluate_placement, NodeFacts};
use rook_port::registry::{core_api_compatible, parse_resource, Cap, Meta, Param, Registry};
use rook_port::worker::{Peer, Worker};
use serde_json::{json, Map, Value};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

fn load(name: &str) -> Value {
    let dir = std::env::var("ROOK_VECTORS").map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../conformance/vectors"));
    serde_json::from_str(&std::fs::read_to_string(dir.join(format!("{name}.json"))).unwrap()).unwrap()
}
fn hx(v: &Value) -> Vec<u8> {
    let s = v.as_str().unwrap();
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
}
fn tohex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}
fn fid(v: &Value) -> [u8; 16] {
    hx(v).try_into().unwrap()
}
fn strs(v: &Value) -> Vec<String> {
    v.as_array().map(|a| a.iter().map(|x| x.as_str().unwrap().to_string()).collect()).unwrap_or_default()
}

#[test]
fn band_crypto() {
    for c in load("band_crypto")["cases"].as_array().unwrap() {
        let psk = c["psk"].as_str().unwrap();
        assert_eq!(tohex(&band_id(psk)), c["band_id"]);
        let key = band_key(psk);
        assert_eq!(tohex(&key), c["key"]);
        let seq: u64 = c["sequence"].as_str().unwrap().parse().unwrap();
        assert_eq!(tohex(&seal(&key, seq, &hx(&c["plaintext"]))), c["ciphertext"]);
        assert_eq!(tohex(&open(&key, seq, &hx(&c["ciphertext"])).unwrap()), c["plaintext"]);
    }
}

#[test]
fn fragments() {
    let v = load("fragments");
    for c in v["split"].as_array().unwrap() {
        let got: Vec<String> = fragment(&hx(&c["payload"]), fid(&c["fragment_id"]), c["chunk_size"].as_u64().unwrap() as usize)
            .iter().map(|b| tohex(b)).collect();
        assert_eq!(got, strs(&c["chunks"]));
    }
    for c in v["parse"].as_array().unwrap() {
        let raw = hx(&c["chunk"]);
        let got = parse_chunk(&raw);
        assert_eq!(got.is_some(), c["valid"].as_bool().unwrap(), "{}", c["label"]);
        if let Some(g) = got {
            assert_eq!(tohex(&g.fid), c["fragment_id"]);
            assert_eq!((g.seq as u64, g.total as u64), (c["seq"].as_u64().unwrap(), c["total"].as_u64().unwrap()));
            assert_eq!(tohex(g.data), c["data"]);
        }
    }
    for s in v["reassemble"].as_array().unwrap() {
        let mut r = Reassembler::default();
        let out: Vec<String> = s["feed"].as_array().unwrap().iter().filter_map(|c| r.feed(&hx(c))).map(|m| tohex(&m)).collect();
        assert_eq!(out, strs(&s["emits"]), "{}", s["label"]);
    }
}

#[test]
fn frames() {
    for c in load("frames")["cases"].as_array().unwrap() {
        let psk = c["psk"].as_str().unwrap();
        let (key, band) = (band_key(psk), band_id(psk));
        let mut seq = c["first_sequence"].as_u64().unwrap();
        let built: Vec<String> = fragment(&hx(&c["message"]), fid(&c["fragment_id"]), 1003).iter().map(|chunk| {
            let f = pack_frame(&band, seq, &seal(&key, seq, chunk));
            seq += 1;
            tohex(&f)
        }).collect();
        assert_eq!(built, strs(&c["frames"]), "{}", c["label"]);
        let mut r = Reassembler::default();
        let mut msg = None;
        for f in c["frames"].as_array().unwrap() {
            let raw = hx(f);
            let fr = unpack_frame(&raw).unwrap();
            assert!(fr.band == band && fr.kind == 2 && fr.channel == 0);
            msg = r.feed(&open(&key, fr.seq, fr.sealed).unwrap()).or(msg);
        }
        assert_eq!(tohex(&msg.unwrap()), c["message"]);
    }
}

#[test]
fn canonical_json() {
    for c in load("canonical")["cases"].as_array().unwrap() {
        assert_eq!(canonical(&c["value"]), c["canonical"].as_str().unwrap());
        assert_eq!(args_hash(&c["value"]), c["args_hash"].as_str().unwrap());
    }
}

#[test]
fn signatures() {
    let v = load("signatures");
    for k in v["keys"].as_array().unwrap() {
        assert_eq!(key_id(k["public"].as_str().unwrap()), k["kid"]);
    }
    for c in v["grants"].as_array().unwrap() {
        let x = &c["context"];
        let revoked = strs(&x["revoked"]);
        let ctx = GrantCtx { band: x["band"].as_str(), now: x["now"].as_f64().unwrap(), revoked: &revoked,
                             worker_id: x["worker_id"].as_str() };
        assert_eq!(verify_grant(&c["grant"], &strs(&x["anchors"]), &ctx).0, c["ok"].as_bool().unwrap(), "{}", c["label"]);
    }
    for c in v["announces"].as_array().unwrap() {
        let x = &c["context"];
        let got = held_roles(&c["announce"], &strs(&x["anchors"]), x["band"].as_str(), x["now"].as_f64().unwrap());
        assert_eq!(got, strs(&c["held_roles"]), "{}", c["label"]);
    }
    let env = |c: &Value| (c["envelope"].clone(), c["grants_by_kid"].as_object().unwrap().clone(), c["now"].as_f64().unwrap());
    for c in v["tickets"].as_array().unwrap() {
        let (e, keys, now) = env(c);
        let te = TicketEnv { cap: e["cap"].as_str().unwrap(), target: e["target"].as_str().unwrap(),
                             msg_id: e["msg_id"].as_str().unwrap(), args: &e["args"], keys: &keys, now };
        assert_eq!(verify_ticket(&c["ticket"], &te, None).0, c["ok"].as_bool().unwrap(), "{}", c["label"]);
    }
    let r = &v["ticket_replay"];
    let (e, keys, now) = env(r);
    let mut replay = ReplayCache::default();
    for want in r["results"].as_array().unwrap() {
        let te = TicketEnv { cap: e["cap"].as_str().unwrap(), target: e["target"].as_str().unwrap(),
                             msg_id: e["msg_id"].as_str().unwrap(), args: &e["args"], keys: &keys, now };
        assert_eq!(verify_ticket(&r["ticket"], &te, Some(&mut replay)).0, want["ok"].as_bool().unwrap());
    }
}

#[test]
fn placement() {
    let v = load("placement");
    for c in v["cases"].as_array().unwrap() {
        let expr = c["expr"].as_str().unwrap();
        assert_eq!(compile_placement(expr).is_ok(), c["valid"].as_bool().unwrap(), "valid: {expr}");
        for (node, want) in c["results"].as_object().unwrap() {
            let n = &v["nodes"][node];
            let facts = NodeFacts { roles: strs(&n["roles"]), hw: n["hw"].as_object().unwrap().clone() };
            assert_eq!(evaluate_placement(Some(expr), &facts), want.as_bool().unwrap(), "{expr} on {node}");
        }
    }
}

fn leak(s: &str) -> &'static str {
    match s { "read" => "read", "write" => "write", "exec" => "exec", _ => "admin" }
}

#[test]
fn tiers() {
    let v = load("tiers");
    let table = TierTable {
        table: v["table"].as_object().unwrap().iter().map(|(k, e)| (k.clone(), leak(e["tier"].as_str().unwrap()))).collect(),
        prefixes: v["prefixes"].as_array().unwrap().iter()
            .map(|p| (p["prefix"].as_str().unwrap().to_string(), leak(p["tier"].as_str().unwrap()))).collect(),
    };
    for c in v["cases"].as_array().unwrap() {
        let opt = |k: &str| if c[k].is_null() { None } else { Some(&c[k]) };
        let got = effective_tier(&table, c["cap"].as_str().unwrap(), opt("declared"), opt("override"), c["lower"].as_bool().unwrap());
        assert_eq!(got, c["tier"].as_str().unwrap(), "{c}");
    }
}

#[test]
fn projection() {
    for c in load("projection")["cases"].as_array().unwrap() {
        let received = Arc::new(Mutex::new(Value::Null));
        let (rec, result) = (received.clone(), c["handler_result"].clone());
        let meta = &c["meta"];
        let mut reg = Registry::default();
        reg.register("t.x", Cap {
            handler: Arc::new(move |a| { *rec.lock().unwrap() = Value::Object(a.clone()); Ok(result.clone()) }),
            params: strs(&c["handler_params"]).iter().map(|n| Param { name: n.clone(), ..Default::default() }).collect(),
            doc: String::new(),
            meta: Meta { risk: None, limit: meta.get("limit").and_then(Value::as_u64), fields: meta.get("fields").cloned() },
        });
        let got = reg.call("t.x", c["args"].as_object().unwrap()).unwrap();
        assert_eq!(got, c["result"], "{}", c["label"]);
        assert_eq!(*received.lock().unwrap(), c["handler_received"], "{}", c["label"]);
    }
}

#[test]
fn core_api() {
    for c in load("core_api")["cases"].as_array().unwrap() {
        assert_eq!(core_api_compatible(c["spec"].as_str().unwrap(), c["have"].as_str().unwrap()),
                   c["compatible"].as_bool().unwrap(), "{c}");
    }
}

#[test]
fn resources() {
    for c in load("resources")["cases"].as_array().unwrap() {
        let got = parse_resource(c["url"].as_str().unwrap());
        assert_eq!(got.is_some(), c["valid"].as_bool().unwrap(), "{}", c["url"]);
        if let Some(r) = got {
            assert_eq!(json!([r.scheme, r.target, r.path]), json!([c["scheme"], c["target"], c["path"]]));
        }
    }
}

#[test]
fn messages() {
    for c in load("messages")["cases"].as_array().unwrap() {
        let w = Worker::new(c["worker_id"].as_str().unwrap(), "worker-a", Peer::new("", ""));
        w.register("conformance.echo", Cap { handler: Arc::new(|a| Ok(a.get("value").cloned().unwrap_or(Value::Null))),
            params: vec![Param::opt("value", Value::Null, "any")], doc: String::new(), meta: Meta::default() });
        w.register("conformance.add", Cap { handler: Arc::new(|a| Ok(json!(a["a"].as_i64().unwrap() + a["b"].as_i64().unwrap()))),
            params: vec![Param::req("a", "int"), Param::req("b", "int")], doc: String::new(), meta: Meta::default() });
        let input = if let Some(h) = c.get("input_hex") {
            match serde_json::from_slice::<Value>(&hx(h)) { Ok(v) => v, Err(_) => Value::Null }
        } else {
            c["input"].clone()
        };
        let replies: Vec<Value> = w.handle(&input).into_iter().collect();
        let want = c["replies"].as_array().unwrap();
        assert_eq!(replies.len(), want.len(), "{}", c["label"]);
        for (got, want) in replies.iter().zip(want) {
            let (mut got, mut exact): (Map<String, Value>, Map<String, Value>) =
                (got.as_object().unwrap().clone(), want.as_object().unwrap().clone());
            if let Some(prefix) = exact.remove("error_prefix") {
                assert!(got["error"].as_str().unwrap().starts_with(prefix.as_str().unwrap()), "{}", c["label"]);
                got.remove("error");
                exact.remove("error");
            }
            assert_eq!(got, exact, "{}", c["label"]);
        }
    }
}
