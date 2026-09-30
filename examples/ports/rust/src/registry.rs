//! Capability registry (spec "Capabilities"): dot-namespaced names, declared
//! params, and the core-enforced limit/fields output contract. Also the
//! plugin manifest helpers (CORE_API ranges, resource strings).

use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::Arc;

#[derive(Debug)]
pub enum CapError {
    BadArgs(String),
    Failed(String, String), // (kind, message) -> "Kind: message"
}

impl CapError {
    pub fn failed(msg: impl Into<String>) -> Self {
        CapError::Failed("Error".into(), msg.into())
    }
    pub fn reply_text(&self) -> String {
        match self {
            CapError::BadArgs(m) => format!("bad args: {}", m),
            CapError::Failed(k, m) => format!("{}: {}", k, m),
        }
    }
}

pub type Handler = Arc<dyn Fn(&Map<String, Value>) -> Result<Value, CapError> + Send + Sync>;

#[derive(Clone, Default)]
pub struct Param {
    pub name: String,
    pub required: bool,
    pub default: Value,
    pub ty: Option<String>,
}

impl Param {
    pub fn req(name: &str, ty: &str) -> Param {
        Param { name: name.into(), required: true, default: Value::Null, ty: Some(ty.into()) }
    }
    pub fn opt(name: &str, default: Value, ty: &str) -> Param {
        Param { name: name.into(), required: false, default, ty: Some(ty.into()) }
    }
}

#[derive(Clone, Default)]
pub struct Meta {
    pub risk: Option<&'static str>,
    pub limit: Option<u64>,
    pub fields: Option<Value>, // "*" or ["a", "b"]
}

#[derive(Clone)]
pub struct Cap {
    pub handler: Handler,
    pub params: Vec<Param>,
    pub doc: String,
    pub meta: Meta,
}

fn parse_fields(v: &Value) -> Result<Option<Vec<String>>, CapError> {
    let names: Vec<String> = match v {
        Value::Null => return Ok(None),
        Value::String(s) => s.split(',').map(|x| x.trim().to_string()).collect(),
        Value::Array(a) => a.iter().map(|x| x.as_str().map(str::to_string).unwrap_or_else(|| x.to_string())).collect(),
        _ => return Err(CapError::BadArgs("fields must be a list of names or a comma-separated string".into())),
    };
    let names: Vec<String> = names.into_iter().filter(|s| !s.trim().is_empty()).collect();
    Ok(if names.is_empty() || names.iter().any(|n| n == "*") { None } else { Some(names) })
}

fn project(result: Value, fields: &[String]) -> Value {
    let one = |d: Value| match d {
        Value::Object(m) => Value::Object(fields.iter().filter_map(|k| m.get(k).map(|v| (k.clone(), v.clone()))).collect()),
        other => other,
    };
    match result {
        Value::Array(a) => Value::Array(a.into_iter().map(one).collect()),
        Value::Object(mut m) => {
            if let Some(Value::Array(items)) = m.get("items").cloned() {
                m.insert("items".into(), Value::Array(items.into_iter().map(one).collect()));
                Value::Object(m)
            } else {
                one(Value::Object(m))
            }
        }
        other => other,
    }
}

fn trim(result: Value, limit: usize) -> Value {
    match result {
        Value::Array(mut a) => {
            a.truncate(limit);
            Value::Array(a)
        }
        Value::Object(mut m) => {
            if let Some(Value::Array(items)) = m.get("items").cloned() {
                if items.len() > limit {
                    let total = m.get("total").cloned().unwrap_or(json!(items.len()));
                    m.insert("items".into(), Value::Array(items[..limit].to_vec()));
                    m.insert("truncated".into(), Value::Bool(true));
                    m.insert("total".into(), total);
                }
            }
            Value::Object(m)
        }
        other => other,
    }
}

#[derive(Default, Clone)]
pub struct Registry {
    caps: BTreeMap<String, Cap>,
}

impl Registry {
    pub fn register(&mut self, name: &str, cap: Cap) {
        assert!(!name.is_empty() && !self.caps.contains_key(name), "bad or duplicate capability {name}");
        self.caps.insert(name.to_string(), cap);
    }
    pub fn has(&self, name: &str) -> bool {
        self.caps.contains_key(name)
    }
    pub fn list(&self) -> Vec<String> {
        self.caps.keys().cloned().collect()
    }

    /// {cap: r|w|x|a} for caps that declare a risk (the announce "tiers").
    pub fn tiers(&self) -> Map<String, Value> {
        self.caps
            .iter()
            .filter_map(|(n, c)| c.meta.risk.map(|r| {
                let letter = match r { "read" => "r", "write" => "w", "exec" => "x", _ => "a" };
                (n.clone(), json!(letter))
            }))
            .collect()
    }

    pub fn describe(&self, prefix: &str) -> Value {
        let mut out = Map::new();
        for (n, c) in self.caps.iter().filter(|(n, _)| n.starts_with(prefix)) {
            let params: Vec<Value> = c.params.iter().map(|p| json!({"name": p.name, "required": p.required,
                "default": if p.required { Value::Null } else { p.default.clone() }, "type": p.ty})).collect();
            let doc = c.doc.split("\n\n").next().unwrap_or("").replace('\n', " ").trim().to_string();
            let mut e = json!({"doc": doc, "params": params});
            if let Some(r) = c.meta.risk {
                e["risk"] = json!(r);
            }
            if let Some(l) = c.meta.limit {
                e["limit"] = json!(l);
            }
            if let Some(f) = &c.meta.fields {
                e["fields"] = f.clone();
            }
            out.insert(n.clone(), e);
        }
        Value::Object(out)
    }

    /// Check args against params, apply limit/fields, run the handler.
    pub fn call(&self, name: &str, input: &Map<String, Value>) -> Result<Value, CapError> {
        let c = self.caps.get(name).ok_or_else(|| CapError::failed(format!("no such capability: {name}")))?;
        let mut args = input.clone();
        let accepts = |p: &str| c.params.iter().any(|x| x.name == p);
        let mut trim_to: Option<usize> = None;
        let mut fields: Option<Vec<String>> = None;
        if let Some(limit) = c.meta.limit {
            if accepts("limit") {
                if args.get("limit").is_none_or(Value::is_null) {
                    args.insert("limit".into(), json!(limit));
                }
            } else {
                let asked = args.remove("limit").and_then(|v| v.as_u64()).filter(|n| *n > 0);
                trim_to = Some(asked.unwrap_or(limit) as usize);
            }
        }
        if let Some(default) = &c.meta.fields {
            if !accepts("fields") {
                fields = match args.remove("fields") {
                    Some(v) => parse_fields(&v)?,
                    None if default == "*" => None,
                    None => parse_fields(default)?,
                };
            }
        }
        if let Some(k) = args.keys().find(|k| !accepts(k)) {
            return Err(CapError::BadArgs(format!("{name}() got an unexpected keyword argument '{k}'")));
        }
        if let Some(p) = c.params.iter().find(|p| p.required && !args.contains_key(&p.name)) {
            return Err(CapError::BadArgs(format!("{name}() missing required argument: '{}'", p.name)));
        }
        let mut result = (c.handler)(&args)?;
        if let Some(n) = trim_to {
            result = trim(result, n);
        }
        if let Some(f) = fields {
            result = project(result, &f);
        }
        Ok(result)
    }
}

fn vtuple(v: &str) -> Option<Vec<u64>> {
    let mut parts: Vec<u64> = v.trim().split('.').map_while(|p| p.parse().ok().filter(|_| p.bytes().all(|b| b.is_ascii_digit()))).collect();
    if parts.is_empty() {
        return None;
    }
    while parts.len() < 2 {
        parts.push(0);
    }
    Some(parts)
}

/// Whether core API version `have` satisfies a plugin's CORE_API range.
pub fn core_api_compatible(spec: &str, have: &str) -> bool {
    let Some(h) = vtuple(have) else { return false };
    let s = spec.trim();
    if s.is_empty() {
        return true;
    }
    if s.starts_with(|c: char| c.is_ascii_digit()) {
        let Some(lo) = vtuple(s) else { return false };
        return lo <= h && h < vec![lo[0] + 1, 0];
    }
    for clause in s.split(',') {
        let clause = clause.trim();
        let op_len = clause.bytes().take_while(|b| b"<>=!".contains(b)).count();
        let (op, ver) = clause.split_at(op_len);
        let ver = ver.trim();
        if ver.is_empty() || !ver.bytes().all(|b| b.is_ascii_digit() || b == b'.') {
            return false;
        }
        let Some(v) = vtuple(ver) else { return false };
        let ok = match op {
            ">=" => h >= v,
            "<=" => h <= v,
            "==" => h == v,
            "!=" => h != v,
            ">" => h > v,
            "<" => h < v,
            _ => return false,
        };
        if !ok {
            return false;
        }
    }
    true
}

#[derive(Debug, PartialEq)]
pub struct Resource {
    pub scheme: String,
    pub target: String,
    pub path: String,
}

/// Parse a resource connection string: cap://<worker|any>/<cap>, http(s)://, sqlite://, file://.
pub fn parse_resource(url: &str) -> Option<Resource> {
    let (scheme, rest) = url.split_once("://")?;
    let scheme = scheme.to_lowercase();
    if !["cap", "http", "https", "sqlite", "file"].contains(&scheme.as_str()) {
        return None;
    }
    let rest = rest.split(['?', '#']).next().unwrap_or("");
    let (netloc, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, ""),
    };
    if scheme == "cap" {
        let cap = path.trim_start_matches('/');
        if netloc.is_empty() || cap.is_empty() {
            return None;
        }
        return Some(Resource { scheme, target: netloc.into(), path: cap.into() });
    }
    Some(Resource { scheme, target: netloc.into(), path: path.into() })
}
