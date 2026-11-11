//! What joining needs, from the files enrollment writes for the Python
//! worker (rook/core2/enroll/device.py `install`): the config's `name`,
//! `worker_id` and `data`, its inline `bands`, and `<data>/bands.json`.
//! The first band with a PSK and a relay is joined; a band from bands.json
//! without a `ws_url` gets the hub's `/band` web socket at the address kept in
//! `<data>/enrollment.json`, the fallback when UDP hears nothing. `anchor`
//! names the home hub's root key (core updates).

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;
use yaml_rust2::{Yaml, YamlLoader};

pub struct Config {
    pub name: String,
    pub worker_id: String,
    pub psk: String,
    pub relay: String,
    pub ws_url: Option<String>,
    pub data: PathBuf,
    /// anchor.json: the home hub's root key, which a core release's signature chains to.
    pub anchor: PathBuf,
}

pub fn load(path: &Path) -> Result<Config, String> {
    let text = fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let docs = YamlLoader::load_from_str(&text).map_err(|e| format!("{}: {e}", path.display()))?;
    let raw = docs.into_iter().next().unwrap_or(Yaml::Null);
    let base = path.parent().unwrap_or(Path::new("."));
    let data = base.join(
        raw["data"]
            .as_str()
            .ok_or(format!("{}: data is required", path.display()))?,
    );
    let anchor = base.join(raw["anchor"].as_str().unwrap_or("anchor.json"));
    let name = match raw["name"].as_str() {
        Some(n) => n.to_string(),
        None => hostname(),
    };
    let worker_id = match raw["worker_id"].as_str() {
        Some(w) => w.to_string(),
        None => held_id(&data)?,
    };
    let text = |v: &Value, k: &str| v[k].as_str().map(String::from);
    let mut bands: Vec<(Option<String>, Option<String>, Option<String>)> = raw["bands"]
        .as_vec()
        .map(|v| {
            v.iter()
                .map(|b| {
                    let psk = b["psk"]
                        .as_str()
                        .map(String::from)
                        .or_else(|| b["psk_env"].as_str().and_then(|e| std::env::var(e).ok()));
                    (
                        psk,
                        b["relay"].as_str().map(String::from),
                        b["ws_url"].as_str().map(String::from),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    let read = |name: &str| {
        fs::read_to_string(data.join(name))
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .unwrap_or(Value::Null)
    };
    let door = text(&read("enrollment.json"), "url").and_then(|site| band_door(&site));
    if let Value::Object(joined) = read("bands.json") {
        for spec in joined.values() {
            bands.push((
                text(spec, "psk"),
                text(spec, "relay"),
                text(spec, "ws_url").or_else(|| door.clone()),
            ));
        }
    }
    let (psk, relay, ws_url) = bands
        .into_iter()
        .find_map(|(p, r, w)| Some((p?, r?, w)))
        .ok_or("no band with a psk and a relay in the config or <data>/bands.json")?;
    Ok(Config {
        name,
        worker_id,
        psk,
        relay,
        ws_url,
        data,
        anchor,
    })
}

/// The hub's `/band` web socket at its site address: `https://hub.example` -> `wss://hub.example/band`.
pub fn band_door(site: &str) -> Option<String> {
    let (scheme, rest) = site.split_once("://")?;
    let host = rest.split('/').next().filter(|h| !h.is_empty())?;
    Some(format!(
        "{}://{host}/band",
        if scheme == "https" { "wss" } else { "ws" }
    ))
}

/// `<data>/worker_id`, made once when the config names none (as the Python worker does).
fn held_id(data: &Path) -> Result<String, String> {
    let file = data.join("worker_id");
    if let Ok(held) = fs::read_to_string(&file) {
        let held = held.trim();
        if held.len() == 32 && held.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()) {
            return Ok(held.to_string());
        }
    }
    let id = crate::link::hex(&crate::link::random::<16>());
    fs::create_dir_all(data)
        .and_then(|_| fs::write(&file, format!("{id}\n")))
        .map_err(|e| format!("{}: {e}", file.display()))?;
    Ok(id)
}

pub fn hostname() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| fs::read_to_string("/proc/sys/kernel/hostname").map(|s| s.trim().to_string()))
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "worker".into())
}

#[cfg(test)]
mod tests {
    #[test]
    fn the_band_door_is_the_hubs_own_address() {
        assert_eq!(
            super::band_door("https://hub.example").as_deref(),
            Some("wss://hub.example/band")
        );
        assert_eq!(
            super::band_door("http://192.168.1.5:8750/").as_deref(),
            Some("ws://192.168.1.5:8750/band")
        );
        assert_eq!(super::band_door("hub.example"), None);
    }
}
