//! The manifests this worker announces: the repo's own files, embedded
//! unmodified, parsed at start and hashed as the hub hashes them (the hex
//! SHA-256 of the RFC 8785 canonical JSON of the parsed manifest), so the
//! hub sees the very plugins a Python worker announces.

use serde_json::{Map, Number, Value};
use sha2::{Digest, Sha256};
use yaml_rust2::{Yaml, YamlLoader};

pub const FILES: [(&str, &str); 4] = [
    ("worker", include_str!("../../rook/core2/builtin/worker.yaml")),
    ("stream", include_str!("../../rook/core2/builtin/stream.yaml")),
    ("shell", include_str!("../../rook/plugins/shell/plugin.yaml")),
    ("file", include_str!("../../rook/plugins/file/plugin.yaml")),
];

pub struct Manifest {
    pub plugin: String,
    pub version: String,
    pub hash: String,
    pub raw: Value,
}

pub fn load(text: &str) -> Manifest {
    let docs = YamlLoader::load_from_str(text).expect("an embedded manifest is YAML");
    let raw = to_json(&docs[0]);
    Manifest {
        plugin: raw["plugin"].as_str().expect("plugin").to_string(),
        version: raw["version"].as_str().expect("version").to_string(),
        hash: sha256(&raw),
        raw,
    }
}

/// The plugins this worker serves. `stream` waits for the stream caps (stage 2).
pub fn served() -> Vec<Manifest> {
    FILES.iter().filter(|(name, _)| *name != "stream").map(|(_, text)| load(text)).collect()
}

fn to_json(y: &Yaml) -> Value {
    match y {
        Yaml::Hash(h) => {
            let mut m = Map::new();
            for (k, v) in h {
                let key = match k {
                    Yaml::String(s) => s.clone(),
                    other => panic!("manifest key is not a string: {other:?}"),
                };
                m.insert(key, to_json(v));
            }
            Value::Object(m)
        }
        Yaml::Array(a) => Value::Array(a.iter().map(to_json).collect()),
        Yaml::String(s) => Value::String(s.clone()),
        Yaml::Integer(i) => Value::from(*i),
        Yaml::Real(r) => Number::from_f64(r.parse().expect("a YAML float")).map(Value::Number).expect("finite"),
        Yaml::Boolean(b) => Value::Bool(*b),
        Yaml::Null => Value::Null,
        other => panic!("not JSON in a manifest: {other:?}"),
    }
}

pub fn sha256(v: &Value) -> String {
    crate::link::hex(&Sha256::digest(canonical(v).as_bytes()))
}

/// RFC 8785. Object keys sort by their UTF-16 code units.
pub fn canonical(v: &Value) -> String {
    let mut out = String::new();
    write(v, &mut out);
    out
}

fn write(v: &Value, out: &mut String) {
    match v {
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
            out.push('{');
            for (i, k) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&Value::String(k.clone()).to_string());
                out.push(':');
                write(&m[k], out);
            }
            out.push('}');
        }
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
        Value::Number(n) if n.is_f64() => out.push_str(&number(n.as_f64().unwrap())),
        other => out.push_str(&other.to_string()),
    }
}

/// ECMAScript's shortest form of a double.
fn number(f: f64) -> String {
    if f == 0.0 {
        return "0".into();
    }
    if f.fract() == 0.0 && f.abs() < 1e21 {
        return format!("{f:.0}");
    }
    let s = format!("{f:e}"); // shortest digits: "1.5e-7"
    let (mant, exp) = s.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    if (-6..21).contains(&exp) {
        return format!("{f}");
    }
    format!("{mant}e{}{exp}", if exp > 0 { "+" } else { "" })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_matches_the_conformance_vectors() {
        // The two "error" cases (a lone surrogate, which serde_json won't even
        // parse, and an integer past 2^53) never occur in the embedded manifests.
        let text = include_str!("../../conformance/core-v2/vectors/canonical.json").replace(r#""\ud800""#, "null");
        let v: Value = serde_json::from_str(&text).unwrap();
        for case in v["cases"].as_array().unwrap().iter().filter(|c| c.get("error").is_none()) {
            assert_eq!(canonical(&case["value"]), case["canonical"].as_str().unwrap());
        }
    }

    #[test]
    fn numbers_print_as_ecmascript() {
        for (f, want) in [(0.1, "0.1"), (30.0, "30"), (1e21, "1e+21"), (1.5e-7, "1.5e-7"), (1e-6, "0.000001"), (1e20, "100000000000000000000"), (123.456, "123.456"), (-2.0, "-2")] {
            assert_eq!(number(f), want);
        }
    }

    #[test]
    fn every_embedded_manifest_loads() {
        for (name, text) in FILES {
            let m = load(text);
            assert_eq!(m.plugin, name);
            assert_eq!(m.raw["builtin"].as_bool(), if matches!(name, "worker" | "stream") { Some(true) } else { None });
        }
    }
}
