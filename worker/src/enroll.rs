//! Joining a hub (spec 12), as `python -m rook.core2.worker enroll` does
//! (rook/core2/enroll/device.py): claim a one-time code, or ask to join and
//! wait until someone lets this machine in on the Workers page; then write
//! the same files: anchor.json, `<data>/bands.json`, `<data>/services.json`
//! (when the hub sends any), `<data>/enrollment.json` and the config's
//! `data`, `anchor`, `name` and `worker_id` (everything else in it is kept).
//! No worker key is sent or kept: the hub takes one only for the Python
//! worker's signed requests.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use yaml_rust2::{Yaml, YamlEmitter, YamlLoader};

const CODE_SECS: u64 = 600;
const USER_AGENT: &str = "rook-worker-enroll/0.5";

pub struct Enrolled {
    pub name: String,
    pub worker_id: String,
    pub hub: String,
    pub owner: Value,
    pub config: PathBuf,
}

/// A typed address or the text of the hub's QR: (url, one-time code, pinned hub id).
pub fn parse_target(text: &str) -> Result<(String, Option<String>, Option<String>), String> {
    if let Some(query) = text.strip_prefix("rook://enroll").map(|q| q.trim_start_matches('?')) {
        let get = |k: &str| {
            query.split('&').find_map(|kv| kv.split_once('=').filter(|(n, _)| *n == k).map(|(_, v)| decode(v)))
        };
        let url = get("url").filter(|u| !u.is_empty()).ok_or("the QR has no hub address")?;
        return Ok((url.trim_end_matches('/').into(), get("code"), get("hub")));
    }
    let url = if text.contains("://") { text.to_string() } else { format!("https://{text}") };
    Ok((url.trim_end_matches('/').into(), None, None))
}

fn decode(v: &str) -> String {
    let b = v.as_bytes();
    let mut out = vec![];
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'+' => out.push(b' '),
            b'%' if i + 2 < b.len() => {
                out.push(u8::from_str_radix(&v[i + 1..i + 3], 16).unwrap_or(b'?'));
                i += 2;
            }
            c => out.push(c),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Plain http only to this machine, unless `insecure`: the band key travels in the answer.
pub fn check_url(url: &str, insecure: bool) -> Result<(), String> {
    if url.starts_with("https://") || insecure {
        return Ok(());
    }
    let Some(rest) = url.strip_prefix("http://") else { return Err(format!("not a hub address: {url}")) };
    let host = rest.split(['/', '?']).next().unwrap_or("");
    let host = host.rsplit_once(':').map_or(host, |(h, p)| if p.chars().all(|c| c.is_ascii_digit()) { h } else { host });
    let host = host.trim_matches(['[', ']']);
    let loopback = host == "localhost" || host.parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback());
    if loopback {
        Ok(())
    } else {
        Err(format!("{url} is plain http: the band key would cross the network in the clear. \
                     Use the hub's https address, or --insecure if you mean it"))
    }
}

async fn post(client: &reqwest::Client, url: &str, path: &str, body: Value) -> Result<Value, String> {
    let r = client
        .post(format!("{url}{path}"))
        .header("Content-Type", "application/json")
        .body(body.to_string())
        .send()
        .await
        .map_err(|e| format!("can't reach the hub at {url}: {e}"))?;
    let status = r.status();
    let text = r.text().await.map_err(|e| format!("can't reach the hub at {url}: {e}"))?;
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(mut m)) => {
            if !status.is_success() {
                m.insert("ok".into(), json!(false));
            }
            Ok(Value::Object(m))
        }
        _ if !status.is_success() => Ok(json!({"ok": false, "error": format!("HTTP {}", status.as_u16())})),
        _ => Err(format!("the hub at {url} answered something that is not JSON")),
    }
}

/// Claim `code`, or ask and wait to be let in; the checked bundle.
pub async fn sign_in(url: &str, name: &str, code: Option<&str>, pinned: Option<&str>, say: &(dyn Fn(&str) + Sync)) -> Result<Value, String> {
    let client = reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let mut pinned = pinned.map(String::from);
    let got = if let Some(code) = code {
        post(&client, url, "/enroll/claim", json!({"code": code, "name": name})).await?
    } else {
        let start = post(&client, url, "/enroll/ask", json!({"name": name})).await?;
        if start["ok"] != true {
            return Err(format!("the hub refused: {}", start["error"].as_str().unwrap_or("?")));
        }
        let ticket = start["ticket"].as_str().ok_or("the hub's answer has no ticket")?.to_string();
        let hub = start["hub"].as_str().unwrap_or("?");
        pinned = pinned.or(Some(hub.to_string()));
        say(&format!("{name} is waiting for someone to let this machine in on the Workers page of hub {hub}."));
        let deadline = Instant::now() + Duration::from_secs(CODE_SECS);
        loop {
            let got = post(&client, url, "/enroll/poll", json!({"ticket": ticket})).await?;
            if got["pending"] != true {
                break got;
            }
            if Instant::now() > deadline {
                return Err("nobody let this machine in in time".into());
            }
            let every = got["interval"].as_f64().unwrap_or(2.0).clamp(1.0, 10.0);
            tokio::time::sleep(Duration::from_secs_f64(every)).await;
        }
    };
    if got["ok"] != true {
        return Err(format!("the hub refused: {}", got["error"].as_str().unwrap_or("?")));
    }
    check_bundle(&got["bundle"], pinned.as_deref())
}

/// The bundle's parts hang together, and it is from the pinned hub.
fn check_bundle(b: &Value, pinned: Option<&str>) -> Result<Value, String> {
    let malformed = |what: &str| format!("the hub sent a malformed bundle ({what})");
    let hub_id = b["hub"]["hub_id"].as_str().ok_or_else(|| malformed("hub_id"))?;
    let root = b["hub"]["root"].as_str().ok_or_else(|| malformed("root"))?;
    if crate::sig::key_id(root).as_deref() != Some(hub_id) {
        return Err("the bundle's hub id is not its root key's".into());
    }
    let psk = b["band"]["psk"].as_str().ok_or_else(|| malformed("psk"))?;
    if b["band"]["id"].as_str() != Some(crate::link::band_id_hex(psk).as_str()) {
        return Err("the bundle's band key is for another band".into());
    }
    if b["band"]["relay"].as_str().is_none() && b["band"]["ws_url"].as_str().is_none() {
        return Err("the bundle says no way to reach the band".into());
    }
    if b["worker_id"].as_str().is_none_or(|w| w.len() != 32) {
        return Err("the bundle has no worker id".into());
    }
    if pinned.is_some_and(|p| p != hub_id) {
        return Err(format!("the bundle is from hub {hub_id}, not {}: refused", pinned.unwrap()));
    }
    Ok(b.clone())
}

fn load(config: &Path) -> Result<Yaml, String> {
    match fs::read_to_string(config) {
        Ok(text) => Ok(YamlLoader::load_from_str(&text).map_err(|e| format!("{}: {e}", config.display()))?.into_iter().next().unwrap_or(Yaml::Null)),
        Err(_) => Ok(Yaml::Null),
    }
}

fn base(config: &Path) -> PathBuf {
    let abs = std::path::absolute(config).unwrap_or_else(|_| config.to_path_buf());
    abs.parent().map(Path::to_path_buf).unwrap_or_default()
}

/// The hub this config is enrolled with already, if any.
pub fn home_of(config: &Path) -> Result<Option<String>, String> {
    let raw = load(config)?;
    let anchor = base(config).join(raw["anchor"].as_str().unwrap_or("anchor.json"));
    Ok(fs::read_to_string(anchor).ok().map(|t| {
        serde_json::from_str::<Value>(&t).ok().and_then(|v| v["hub"].as_str().map(String::from)).unwrap_or_else(|| "?".into())
    }))
}

fn name_ok(n: &str) -> bool {
    let b = n.as_bytes();
    !b.is_empty() && b.len() <= 63 && b[0].is_ascii_alphanumeric()
        && b.iter().all(|c| c.is_ascii_alphanumeric() || b"._-".contains(c)) && n != "rook"
}

pub fn default_name(config: &Path) -> String {
    let raw = load(config).unwrap_or(Yaml::Null);
    let name = raw["name"].as_str().map(String::from).unwrap_or_else(|| crate::config::hostname().split('.').next().unwrap_or("").to_string());
    if name_ok(&name) { name } else { "worker".into() }
}

fn write(path: &Path, text: &str, mode: u32) -> Result<(), String> {
    let fail = |e: std::io::Error| format!("{}: {e}", path.display());
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(fail)?;
    }
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, text).map_err(fail)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(mode)).map_err(fail)?;
    }
    #[cfg(not(unix))]
    let _ = mode;
    fs::rename(&tmp, path).map_err(fail)
}

/// Write what the worker needs to start (the files device.py `install` writes).
pub fn install(config: &Path, bundle: &Value, url: &str, force: bool) -> Result<(), String> {
    let mut raw = match load(config)? {
        Yaml::Hash(h) => h,
        Yaml::Null => Default::default(),
        _ => return Err(format!("{}: not a mapping", config.display())),
    };
    let key = |k: &str| Yaml::String(k.into());
    let data_rel = raw.get(&key("data")).and_then(Yaml::as_str).unwrap_or("data").to_string();
    let anchor_rel = raw.get(&key("anchor")).and_then(Yaml::as_str).unwrap_or("anchor.json").to_string();
    let (data, anchor) = (base(config).join(&data_rel), base(config).join(&anchor_rel));
    if anchor.exists() {
        if !force {
            return Err(already(home_of(config)?));
        }
        let memberships = raw.get(&key("memberships")).and_then(Yaml::as_str).map_or(data.join("memberships.json"), |m| base(config).join(m));
        for old in [memberships, data.join("bands.json"), data.join("services.json"), anchor.clone()] {
            let _ = fs::remove_file(old);
        }
    }
    let (hub, band) = (&bundle["hub"], &bundle["band"]);
    write(&anchor, &format!("{}\n", json!({"hub": hub["hub_id"], "root": hub["root"], "band": band["id"]})), 0o644)?;
    let link: serde_json::Map<String, Value> =
        ["psk", "relay", "ws_url"].iter().filter(|k| band[**k].is_string()).map(|k| (k.to_string(), band[*k].clone())).collect();
    let band_id = band["id"].as_str().unwrap_or_default();
    write(&data.join("bands.json"), &serde_json::to_string_pretty(&json!({band_id: link})).unwrap(), 0o600)?;
    if bundle["services"].as_object().is_some_and(|s| !s.is_empty()) {
        write(&data.join("services.json"), &serde_json::to_string_pretty(&bundle["services"]).unwrap(), 0o600)?;
    }
    let url = hub["url"].as_str().filter(|u| !u.is_empty()).unwrap_or(url);
    let enrollment = json!({"hub": hub["hub_id"], "url": url, "owner": bundle["owner"], "at": crate::now() as u64});
    write(&data.join("enrollment.json"), &serde_json::to_string_pretty(&enrollment).unwrap(), 0o600)?;
    raw.insert(key("data"), Yaml::String(data_rel));
    raw.insert(key("anchor"), Yaml::String(anchor_rel));
    raw.insert(key("name"), Yaml::String(bundle["name"].as_str().unwrap_or_default().into()));
    raw.insert(key("worker_id"), Yaml::String(bundle["worker_id"].as_str().unwrap_or_default().into()));
    let mut out = String::new();
    YamlEmitter::new(&mut out).dump(&Yaml::Hash(raw)).map_err(|e| e.to_string())?;
    write(config, &format!("{out}\n"), 0o600)
}

fn already(hub: Option<String>) -> String {
    format!("this worker's home is already hub {}. Enrolling again (--force) makes the new hub \
             its home and drops every other band", hub.unwrap_or_else(|| "?".into()))
}

/// The whole enrollment: find the hub, sign in, write the files.
pub async fn enroll(config: &Path, target: &str, code: Option<&str>, name: Option<&str>, insecure: bool,
                    force: bool, say: &(dyn Fn(&str) + Sync)) -> Result<Enrolled, String> {
    let (url, qr_code, pinned) = parse_target(target)?;
    check_url(&url, insecure)?;
    if !force {
        if let Some(hub) = home_of(config)? {
            return Err(already(Some(hub)));
        }
    }
    let name = name.map(String::from).unwrap_or_else(|| default_name(config));
    let bundle = sign_in(&url, &name, code.or(qr_code.as_deref()), pinned.as_deref(), say).await?;
    install(config, &bundle, &url, force)?;
    Ok(Enrolled {
        name: bundle["name"].as_str().unwrap_or_default().into(),
        worker_id: bundle["worker_id"].as_str().unwrap_or_default().into(),
        hub: bundle["hub"]["hub_id"].as_str().unwrap_or_default().into(),
        owner: bundle["owner"].clone(),
        config: config.to_path_buf(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn targets_and_addresses() {
        assert_eq!(parse_target("rook.example").unwrap(), ("https://rook.example".into(), None, None));
        let (url, code, hub) = parse_target("rook://enroll?url=https%3A%2F%2Frook.example%2F&code=ab-c_1&hub=19c7").unwrap();
        assert_eq!((url.as_str(), code.as_deref(), hub.as_deref()), ("https://rook.example", Some("ab-c_1"), Some("19c7")));
        assert!(check_url("http://127.0.0.1:8750", false).is_ok());
        assert!(check_url("http://[::1]:8750/x", false).is_ok());
        assert!(check_url("http://192.168.1.4:8750", false).unwrap_err().contains("plain http"));
        assert!(check_url("http://192.168.1.4:8750", true).is_ok());
    }
}
