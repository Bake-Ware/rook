//! What the hub client (`hub05`) and the rest of the app share: errors, the
//! quick-ask answer, what waits on the person, the HTTP client, a few text
//! helpers, and the public update feed (`/updates/desktop.json`).

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::Duration;

#[derive(Debug, Clone, PartialEq)]
pub enum HubError {
    /// No stored sign-in, or the hub no longer accepts it.
    NotSignedIn,
    Unreachable(String),
    Http(u16, String),
    /// The home agent is off or misconfigured, or said it can't answer.
    Agent(String),
    Timeout,
}

impl std::fmt::Display for HubError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HubError::NotSignedIn => write!(f, "Sign in to your hub first (Settings)."),
            HubError::Unreachable(e) => write!(f, "Can't reach the hub ({e})."),
            HubError::Http(code, msg) if msg.is_empty() => write!(f, "The hub answered HTTP {code}."),
            HubError::Http(code, msg) => write!(f, "The hub answered HTTP {code}: {msg}"),
            HubError::Agent(msg) => write!(f, "{msg}"),
            HubError::Timeout => write!(f, "No answer yet. It may still reply in Chat."),
        }
    }
}

impl std::error::Error for HubError {}

impl From<reqwest::Error> for HubError {
    fn from(e: reqwest::Error) -> Self {
        if e.is_timeout() {
            HubError::Timeout
        } else {
            HubError::Unreachable(short_error(&e))
        }
    }
}

fn short_error(e: &reqwest::Error) -> String {
    if e.is_connect() {
        "connection refused or no route".into()
    } else {
        let s = e.to_string();
        s.chars().take(120).collect()
    }
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Answer {
    pub text: String,
    /// "agent" (the house agent answered).
    pub via: &'static str,
    pub agent: String,
    pub room: Option<String>,
}

/// One thing that needs the person, for a notification.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AttentionItem {
    pub id: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub text: String,
}

pub(crate) fn client(timeout: Duration) -> reqwest::Client {
    reqwest::Client::builder()
        // Never follow redirects: a redirect here means "sign in" (or a
        // cookie for some other host), never something to chase with the
        // session attached.
        .redirect(reqwest::redirect::Policy::none())
        .timeout(timeout)
        .user_agent(concat!("rook-desktop/", env!("CARGO_PKG_VERSION")))
        .build()
        .expect("http client")
}

pub(crate) fn text(v: &Value, key: &str) -> String {
    v.get(key).and_then(Value::as_str).unwrap_or("").trim().to_string()
}

pub(crate) fn first_line(s: &str, max: usize) -> String {
    let line = s.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or("");
    if line.chars().count() <= max {
        line.to_string()
    } else {
        let mut out: String = line.chars().take(max - 1).collect();
        out.push('\u{2026}');
        out
    }
}

/// One thing waiting on the person, as the Rook panel shows it.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Waiting {
    pub id: String,
    pub kind: String,
    /// The speakable name ("brave-otter-lamp").
    pub name: String,
    pub title: String,
    pub text: String,
    pub who: String,
    pub age: String,
    /// Where it is decided on the dashboard.
    pub link: String,
}

/// The hub's answer to "is there a newer desktop app?"
/// (`GET /updates/desktop.json?version=&platform=`, public).
pub async fn desktop_downloads(origin: &str, version: &str, platform: &str) -> Result<Vec<String>, HubError> {
    let url = format!(
        "{origin}/updates/desktop.json?version={}&platform={}",
        urlencode(version),
        urlencode(platform)
    );
    let resp = client(Duration::from_secs(15)).get(url).send().await?;
    let status = resp.status().as_u16();
    if status != 200 {
        return Err(HubError::Http(status, String::new()));
    }
    let v: Value = resp
        .json()
        .await
        .map_err(|_| HubError::Http(status, "not JSON".into()))?;
    Ok(v.get("downloads")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|d| d.get("url").and_then(Value::as_str).map(String::from))
        .collect())
}

pub fn urlencode(s: &str) -> String {
    url::form_urlencoded::byte_serialize(s.as_bytes()).collect()
}

/// `name=value` pairs from `Set-Cookie` header values (attributes dropped).
pub fn set_cookie_pairs<'a>(values: impl IntoIterator<Item = &'a str>) -> Vec<(String, String)> {
    values
        .into_iter()
        .filter_map(|v| {
            let first = v.split(';').next()?.trim();
            let (name, value) = first.split_once('=')?;
            let (name, value) = (name.trim(), value.trim().trim_matches('"'));
            (!name.is_empty() && !value.is_empty()).then(|| (name.to_string(), value.to_string()))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cookies_are_parsed() {
        let pairs = set_cookie_pairs([
            "rook_web=abc; Path=/; HttpOnly; SameSite=Lax",
            "other=\"xyz\"; Max-Age=2592000; Secure",
            "junk",
            "empty=; Path=/",
        ]);
        assert_eq!(
            pairs,
            vec![("rook_web".into(), "abc".into()), ("other".into(), "xyz".into())]
        );
    }

    #[test]
    fn errors_and_lines_read_plainly() {
        assert!(HubError::NotSignedIn.to_string().contains("Sign in"));
        assert_eq!(
            HubError::Http(500, String::new()).to_string(),
            "The hub answered HTTP 500."
        );
        assert_eq!(first_line("", 5), "");
        assert_eq!(first_line("abcdefgh", 5), "abcd\u{2026}");
        assert_eq!(first_line("\n  two \nthree", 9), "two");
    }
}
