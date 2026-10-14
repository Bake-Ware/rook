//! Settings and the stored sign-in, as two JSON files in the app's config
//! directory. Settings are plain preferences; the credential file holds the
//! hub session cookie and the voice key and is written owner-only (0600).

use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

pub const DEFAULT_ASK_SHORTCUT: &str = "Ctrl+Alt+Space";
pub const DEFAULT_PTT_SHORTCUT: &str = "Ctrl+Alt+V";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Settings {
    /// The hub's dashboard address, e.g. `https://rook.example.com`.
    pub hub_url: String,
    /// Global shortcut that opens the quick-ask box.
    pub ask_shortcut: String,
    pub notifications: bool,
    /// Seconds between checks for new messages and things that need you.
    pub poll_seconds: u64,
    /// Voice server base (`https://voice.example.com` or `wss://.../ws`).
    pub voice_url: String,
    /// Hold-to-talk shortcut; empty turns push-to-talk off.
    pub ptt_shortcut: String,
    /// Hands-free wake word (only in builds with the `wake-word` feature).
    pub wake_word: bool,
    /// The user's openWakeWord classifier (`.onnx`). Not bundled: the public
    /// pre-trained ones are licensed non-commercial.
    pub wake_model: String,
    pub wake_threshold: f32,
    /// Start the local worker when the app starts (child-process mode only).
    pub worker_autostart: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            hub_url: String::new(),
            ask_shortcut: DEFAULT_ASK_SHORTCUT.into(),
            notifications: true,
            poll_seconds: 30,
            voice_url: String::new(),
            ptt_shortcut: DEFAULT_PTT_SHORTCUT.into(),
            wake_word: false,
            wake_model: String::new(),
            wake_threshold: 0.5,
            worker_autostart: false,
        }
    }
}

impl Settings {
    /// Clamp values a hand-edited file could break.
    pub fn sanitized(mut self) -> Self {
        self.poll_seconds = self.poll_seconds.clamp(10, 3600);
        if !(0.05..=0.99).contains(&self.wake_threshold) {
            self.wake_threshold = 0.5;
        }
        self.hub_url = self.hub_url.trim().to_string();
        self.voice_url = self.voice_url.trim().to_string();
        self.ask_shortcut = self.ask_shortcut.trim().to_string();
        self.ptt_shortcut = self.ptt_shortcut.trim().to_string();
        self
    }
}

/// The sign-in the app keeps for its own calls to the hub (quick ask,
/// notifications). It is the same session cookie a browser holds after the
/// normal sign-in: `rook_account=...` (account login) or `rook_session=...`
/// (the dashboard password).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Credentials {
    /// Origin the cookie belongs to; a different hub never gets it.
    pub hub: String,
    pub cookie: String,
    /// Who the hub says this is (for display only).
    pub principal: String,
    /// Bearer key for the voice server's /api/transcribe.
    pub voice_key: String,
    /// Optional agent token (the hub's Tokens page). With one, quick ask
    /// calls `home.ask` over MCP exactly as `rook_home_ask` does.
    pub agent_token: String,
}

impl Credentials {
    pub fn signed_in_to(&self, hub_origin: &str) -> bool {
        !self.cookie.is_empty() && !hub_origin.is_empty() && self.hub == hub_origin
    }
}

pub struct Store {
    dir: PathBuf,
}

impl Store {
    pub fn new(dir: PathBuf) -> Self {
        Store { dir }
    }

    fn settings_path(&self) -> PathBuf {
        self.dir.join("settings.json")
    }

    fn credentials_path(&self) -> PathBuf {
        self.dir.join("credentials.json")
    }

    pub fn load_settings(&self) -> Settings {
        read_json(&self.settings_path()).map(Settings::sanitized).unwrap_or_default()
    }

    pub fn save_settings(&self, s: &Settings) -> std::io::Result<()> {
        write_json(&self.settings_path(), s, false)
    }

    pub fn load_credentials(&self) -> Credentials {
        read_json(&self.credentials_path()).unwrap_or_default()
    }

    pub fn save_credentials(&self, c: &Credentials) -> std::io::Result<()> {
        write_json(&self.credentials_path(), c, true)
    }
}

fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Option<T> {
    let text = fs::read_to_string(path).ok()?;
    match serde_json::from_str(&text) {
        Ok(v) => Some(v),
        Err(e) => {
            log::warn!("ignoring unreadable {}: {e}", path.display());
            None
        }
    }
}

/// Write via a temp file and rename, so a crash never leaves half a file.
fn write_json<T: Serialize>(path: &Path, value: &T, private: bool) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    let body = serde_json::to_vec_pretty(value).map_err(std::io::Error::other)?;
    {
        let mut opts = fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        if private {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        #[cfg(not(unix))]
        let _ = private; // per-user profile directory ACLs on Windows
        let mut f = opts.open(&tmp)?;
        f.write_all(&body)?;
        f.sync_all()?;
    }
    fs::rename(&tmp, path)
}

/// Turn what someone typed into a hub origin: `rook.example.com` becomes
/// `https://rook.example.com`; a path, query or trailing slash is dropped
/// (the dashboard lives at the root). Plain `http://` is kept, because a LAN
/// hub serves its dashboard over HTTP.
pub fn normalize_hub_url(input: &str) -> Result<String, String> {
    let raw = input.trim();
    if raw.is_empty() {
        return Err("Enter your hub's address.".into());
    }
    let with_scheme = if raw.contains("://") { raw.to_string() } else { format!("https://{raw}") };
    let url = url::Url::parse(&with_scheme).map_err(|_| "That doesn't look like a web address.".to_string())?;
    match url.scheme() {
        "http" | "https" => {}
        _ => return Err("Use an http:// or https:// address.".into()),
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err("Leave the user name and password out of the address.".into());
    }
    let host = url.host_str().ok_or("The address needs a host name.")?;
    let mut origin = format!("{}://{}", url.scheme(), host);
    if let Some(port) = url.port() {
        origin.push_str(&format!(":{port}"));
    }
    Ok(origin)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hub_url_gets_https_and_loses_path() {
        assert_eq!(normalize_hub_url("rook.example.com").unwrap(), "https://rook.example.com");
        assert_eq!(normalize_hub_url(" https://rook.example.com/account?x=1 ").unwrap(), "https://rook.example.com");
        assert_eq!(normalize_hub_url("http://192.0.2.10:7005/").unwrap(), "http://192.0.2.10:7005");
    }

    #[test]
    fn hub_url_rejects_junk() {
        assert!(normalize_hub_url("").is_err());
        assert!(normalize_hub_url("ftp://rook.example.com").is_err());
        assert!(normalize_hub_url("https://me:pw@rook.example.com").is_err());
    }

    #[test]
    fn settings_round_trip_and_sanitize() {
        let dir = std::env::temp_dir().join(format!("rook-desktop-test-{}", std::process::id()));
        let store = Store::new(dir.clone());
        assert_eq!(store.load_settings(), Settings::default());
        let s = Settings { poll_seconds: 1, wake_threshold: 7.0, hub_url: " https://a.example ".into(), ..Default::default() };
        store.save_settings(&s).unwrap();
        let back = store.load_settings();
        assert_eq!(back.poll_seconds, 10);
        assert_eq!(back.wake_threshold, 0.5);
        assert_eq!(back.hub_url, "https://a.example");
        let c = Credentials { hub: "https://a.example".into(), cookie: "rook_session=x".into(), ..Default::default() };
        store.save_credentials(&c).unwrap();
        assert_eq!(store.load_credentials(), c);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(dir.join("credentials.json")).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600);
        }
        assert!(c.signed_in_to("https://a.example"));
        assert!(!c.signed_in_to("https://b.example"));
        fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn unknown_fields_and_partial_files_load() {
        let s: Settings = serde_json::from_str(r#"{"hub_url":"https://a.example","future":1}"#).unwrap();
        assert_eq!(s.ask_shortcut, DEFAULT_ASK_SHORTCUT);
        assert!(s.notifications);
    }
}
