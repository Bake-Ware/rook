//! Canonical JSON (spec "Canonical JSON"): sorted keys (code point order),
//! no whitespace, non-ASCII escaped as \uXXXX, like Python's
//! `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.

use base64::Engine;
use serde_json::Value;
use sha2::{Digest, Sha256};

fn string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => string(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            // Rust string order is byte order, which for UTF-8 is code point order.
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                string(k, out);
                out.push(':');
                write(&m[*k], out);
            }
            out.push('}');
        }
    }
}

pub fn canonical(v: &Value) -> String {
    let mut out = String::new();
    write(v, &mut out);
    out
}

/// base64url(sha256(canonical(args))) without padding.
pub fn args_hash(args: &Value) -> String {
    let empty = Value::Object(Default::default());
    let v = if args.is_null() { &empty } else { args };
    let digest = Sha256::digest(canonical(v).as_bytes());
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(digest)
}
