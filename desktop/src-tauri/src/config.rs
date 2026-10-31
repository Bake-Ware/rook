//! Settings and the stored sign-in, as two JSON files in the app's config
//! directory. Settings are plain preferences; the credential file holds the
//! hub session cookie and the voice key and is written owner-only (0600).

use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

pub const DEFAULT_ASK_SHORTCUT: &str = "Ctrl+Alt+Space";
pub const DEFAULT_PTT_SHORTCUT: &str = "Ctrl+Alt+V";
pub const SPEAK_VOICE: &str = "voice";
pub const SPEAK_ALWAYS: &str = "always";
pub const SPEAK_NEVER: &str = "never";
/// The hub's Rook version: 0.4 (today's hubs) or 0.5 (castling). The switch
/// stays until every hub is on 0.5.
pub const HUB_04: &str = "0.4";
pub const HUB_05: &str = "0.5";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Settings {
    /// The hub's dashboard address, e.g. `https://rook.example.com`.
    pub hub_url: String,
    /// "0.4" or "0.5": which hub API the app speaks.
    pub hub_version: String,
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
    /// Read answers aloud: "voice" (after a spoken question), "always" or
    /// "never". Needs the voice server.
    pub speak_answers: String,
    /// Start the app when the person signs in to the computer.
    pub start_at_login: bool,
    /// The newest app version already announced, so each is announced once.
    pub update_notified: String,
    /// 0.5: the Python that runs this computer's worker (Rook installed in
    /// it); empty is `python3` (`python` on Windows).
    pub worker_python: String,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            hub_url: String::new(),
            hub_version: HUB_04.into(),
            ask_shortcut: DEFAULT_ASK_SHORTCUT.into(),
            notifications: true,
            poll_seconds: 30,
            voice_url: String::new(),
            ptt_shortcut: DEFAULT_PTT_SHORTCUT.into(),
            wake_word: false,
            wake_model: String::new(),
            wake_threshold: 0.5,
            worker_autostart: false,
            speak_answers: SPEAK_VOICE.into(),
            start_at_login: false,
            update_notified: String::new(),
            worker_python: String::new(),
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
        if ![SPEAK_VOICE, SPEAK_ALWAYS, SPEAK_NEVER].contains(&self.speak_answers.as_str()) {
            self.speak_answers = SPEAK_VOICE.into();
        }
        if self.hub_version != HUB_05 {
            self.hub_version = HUB_04.into();
        }
        self.worker_python = self.worker_python.trim().to_string();
        self
    }

    pub fn v05(&self) -> bool {
        self.hub_version == HUB_05
    }
}

/// The sign-in the app keeps for its own calls to the hub (quick ask,
/// notifications). On 0.4 it is the same session cookie a browser holds
/// after the normal sign-in: `rook_account=...` (account login) or
/// `rook_session=...` (the dashboard password). On 0.5 it is the person's
/// door token, plus the hub site's `rook_web=...` cookie for the dashboard
/// window.
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
    /// 0.5: the person's door token (`Authorization: Bearer`).
    pub token: String,
}

impl Credentials {
    /// On 0.4 the cookie is the sign-in; on 0.5 the token is.
    pub fn signed_in_to(&self, hub_origin: &str, v05: bool) -> bool {
        let have = if v05 { &self.token } else { &self.cookie };
        !have.is_empty() && !hub_origin.is_empty() && self.hub == hub_origin
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
        read_json(&self.settings_path())
            .map(Settings::sanitized)
            .unwrap_or_default()
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
        // A leftover temp file (a crash mid-write) keeps whatever mode it was
        // created with, and `mode` only applies to new files: start fresh,
        // and set the mode explicitly before any secret is written.
        match fs::remove_file(&tmp) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e),
        }
        let mut opts = fs::OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        if private {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut f = opts.open(&tmp)?;
        #[cfg(unix)]
        if private {
            use std::os::unix::fs::PermissionsExt;
            f.set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        #[cfg(not(unix))]
        let _ = private; // per-user profile directory ACLs on Windows
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
    let with_scheme = if raw.contains("://") {
        raw.to_string()
    } else {
        format!("https://{raw}")
    };
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
        assert_eq!(
            normalize_hub_url("rook.example.com").unwrap(),
            "https://rook.example.com"
        );
        assert_eq!(
            normalize_hub_url(" https://rook.example.com/account?x=1 ").unwrap(),
            "https://rook.example.com"
        );
        assert_eq!(
            normalize_hub_url("http://192.0.2.10:7005/").unwrap(),
            "http://192.0.2.10:7005"
        );
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
        let s = Settings {
            poll_seconds: 1,
            wake_threshold: 7.0,
            hub_url: " https://a.example ".into(),
            ..Default::default()
        };
        store.save_settings(&s).unwrap();
        let back = store.load_settings();
        assert_eq!(back.poll_seconds, 10);
        assert_eq!(back.wake_threshold, 0.5);
        assert_eq!(back.hub_url, "https://a.example");
        let c = Credentials {
            hub: "https://a.example".into(),
            cookie: "rook_session=x".into(),
            ..Default::default()
        };
        store.save_credentials(&c).unwrap();
        assert_eq!(store.load_credentials(), c);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(dir.join("credentials.json")).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600);
        }
        #[cfg(unix)]
        {
            // A leftover world-readable temp file doesn't leak the next write.
            use std::os::unix::fs::PermissionsExt;
            let tmp = dir.join("credentials.json.tmp");
            fs::write(&tmp, b"old").unwrap();
            fs::set_permissions(&tmp, fs::Permissions::from_mode(0o644)).unwrap();
            store.save_credentials(&c).unwrap();
            let mode = fs::metadata(dir.join("credentials.json")).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600);
            assert!(!tmp.exists());
        }
        assert!(c.signed_in_to("https://a.example", false));
        assert!(!c.signed_in_to("https://b.example", false));
        // A 0.4 cookie isn't a 0.5 sign-in, and the other way round.
        assert!(!c.signed_in_to("https://a.example", true));
        let t = Credentials {
            hub: "https://a.example".into(),
            token: "t".into(),
            ..Default::default()
        };
        assert!(t.signed_in_to("https://a.example", true));
        assert!(!t.signed_in_to("https://a.example", false));
        fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn unknown_fields_and_partial_files_load() {
        let s: Settings = serde_json::from_str(r#"{"hub_url":"https://a.example","future":1}"#).unwrap();
        assert_eq!(s.ask_shortcut, DEFAULT_ASK_SHORTCUT);
        assert_eq!(s.speak_answers, SPEAK_VOICE);
        assert!(!s.start_at_login);
        let odd: Settings = serde_json::from_str(r#"{"speak_answers":"loud"}"#).unwrap();
        assert_eq!(odd.sanitized().speak_answers, SPEAK_VOICE);
        assert!(s.notifications);
        assert_eq!(s.hub_version, HUB_04);
        assert!(!s.v05());
        let v: Settings = serde_json::from_str(r#"{"hub_version":"0.5"}"#).unwrap();
        assert!(v.sanitized().v05());
        let odd: Settings = serde_json::from_str(r#"{"hub_version":"9"}"#).unwrap();
        assert_eq!(odd.sanitized().hub_version, HUB_04);
    }
}
