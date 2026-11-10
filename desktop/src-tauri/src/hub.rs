//! Talking to the hub: the panel, quick ask and notifications, over its
//! doors.
//!
//! * sign-in: the person's own door token, the one the hub site's sign-in
//!   page takes. The app posts it to the site's `/login` once, which checks
//!   it is a person's token (an agent's is refused) and gives a session
//!   cookie: `/web/api/me` names the person, and the dashboard window opens
//!   signed in with that cookie;
//! * everything else is the HTTP door's JSON API with `Authorization:
//!   Bearer <token>`: `POST /call` (every call carries a why, journaled),
//!   `GET /holds` (what waits on the person) and `POST /enroll/code` (this
//!   computer's worker, see `worker`). Calls run as the person, with their
//!   rights; nothing here widens them.
//!
//! Quick ask needs no MCP path: the door runs `agent.chat.send` as the
//! person, and the reply lands in the same session the dashboard's dock
//! shows.
//!
//! Also here: errors, the quick-ask answer, what waits on the person, the
//! HTTP client, a few text helpers, and the public update feed
//! (`/updates/desktop.json`).

use crate::sections::{Page, Section};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
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

/// The hub's own node on the band: workbench, agents and pages caps live there.
pub const HUB_WORKER: &str = "rook";
/// The ready-made house agent (rook/plugins/agents), the dashboard dock's agent.
pub const HOUSE: &str = "house";
/// The hub site's session cookie (rook/core2/hub/doors/web/site.py COOKIE).
pub const WEB_COOKIE: &str = "rook_web";
/// Dashboard pages (`#/<plugin>/<page>`, rook/core2/hub/doors/web/static/shell.js).
pub const JOURNAL_PAGE: &str = "/#/journal/journal";

#[derive(Clone)]
pub struct Hub {
    pub origin: String,
    token: String,
    http: reqwest::Client,
}

impl Hub {
    pub fn new(origin: &str, token: &str) -> Self {
        Hub {
            origin: origin.to_string(),
            token: token.to_string(),
            http: client(Duration::from_secs(20)),
        }
    }

    fn req(&self, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        self.http
            .request(method, format!("{}{}", self.origin, path))
            .header("Accept", "application/json")
            .bearer_auth(&self.token)
    }

    async fn json(&self, r: reqwest::RequestBuilder) -> Result<Value, HubError> {
        if self.token.is_empty() {
            return Err(HubError::NotSignedIn);
        }
        door_json(r.send().await?).await
    }

    /// One cap through the door as the person; the reply's `result`.
    pub async fn call(
        &self,
        worker: &str,
        cap: &str,
        args: Value,
        why: &str,
        wait: Duration,
    ) -> Result<Value, HubError> {
        let body = json!({"worker": worker, "cap": cap, "args": args, "why": why});
        let v = self
            .json(self.req(reqwest::Method::POST, "/call").timeout(wait).json(&body))
            .await?;
        call_result(v)
    }

    async fn hub_call(&self, cap: &str, args: Value, why: &str) -> Result<Value, HubError> {
        self.call(HUB_WORKER, cap, args, why, Duration::from_secs(20)).await
    }

    /// Calls waiting for the person (`GET /holds`: theirs, or all for an owner).
    pub async fn waiting(&self) -> Result<Vec<Waiting>, HubError> {
        let v = self.json(self.req(reqwest::Method::GET, "/holds")).await?;
        Ok(hold_items(&v))
    }

    pub async fn attention(&self) -> Result<Option<Vec<AttentionItem>>, HubError> {
        Ok(Some(
            self.waiting()
                .await?
                .into_iter()
                .map(|w| AttentionItem {
                    id: w.id,
                    kind: w.kind,
                    title: w.title,
                    text: w.text,
                })
                .collect(),
        ))
    }

    /// The dashboard's pages by section (`page.list`), for the tray and the panel.
    pub async fn pages(&self) -> Result<Vec<Section>, HubError> {
        let v = self
            .hub_call(
                "page.list",
                json!({}),
                "Desktop app: the tray's Dashboard menu lists the hub's pages.",
            )
            .await?;
        Ok(page_sections(&v))
    }

    /// Ask the house agent in the person's own session with it (the
    /// dashboard dock's conversation) and wait for the reply.
    pub async fn ask(&self, question: &str, wait: Duration) -> Result<Answer, HubError> {
        let v = self
            .call(
                HUB_WORKER,
                "agent.chat.send",
                json!({"agent": HOUSE, "text": question, "wait": true}),
                "Desktop app: the person asks the house agent.",
                wait,
            )
            .await?;
        Ok(Answer {
            text: text(&v, "text"),
            via: "agent",
            agent: format!("agent:{HOUSE}"),
            room: None,
        })
    }

    /// A one-time code for this computer's worker to join with (`POST /enroll/code`), as the signed-in person.
    pub async fn join_code(&self) -> Result<String, HubError> {
        let v = self
            .json(self.req(reqwest::Method::POST, "/enroll/code").json(&json!({})))
            .await?;
        let code = text(&v, "code");
        call_result(v).map(|_| code)
    }
}

/// A door reply: 401 is "sign in again"; other errors carry the hub's words.
async fn door_json(resp: reqwest::Response) -> Result<Value, HubError> {
    let status = resp.status().as_u16();
    if status == 401 {
        return Err(HubError::NotSignedIn);
    }
    let body = resp.text().await?;
    if !(200..300).contains(&status) {
        let msg = serde_json::from_str::<Value>(&body)
            .ok()
            .map(|v| text(&v, "error"))
            .unwrap_or_default();
        return Err(HubError::Http(status, msg));
    }
    serde_json::from_str(&body).map_err(|_| HubError::Http(status, "not JSON".into()))
}

/// `{ok, result | error}`, or a held call `{held, hold}` (spec 6.4).
pub fn call_result(v: Value) -> Result<Value, HubError> {
    if v.get("held").and_then(Value::as_bool) == Some(true) {
        return Err(HubError::Agent(format!(
            "This waits for your approval ({}).",
            text(&v, "hold")
        )));
    }
    match v.get("ok").and_then(Value::as_bool) {
        Some(true) => Ok(v.get("result").cloned().unwrap_or(Value::Null)),
        _ => {
            let err = text(&v, "error");
            let mut s: String = if err.is_empty() { "The call failed.".into() } else { err };
            if let Some(first) = s.get(0..1) {
                s = first.to_uppercase() + &s[1..];
            }
            Err(HubError::Agent(s.chars().take(200).collect()))
        }
    }
}

// ---- the site's session (the dashboard window) -------------------------------------

/// Sign the token in at the hub site: its session cookie (`rook_web=...`)
/// and the person it names. An agent's token, or one the hub doesn't know,
/// is refused.
pub async fn web_sign_in(origin: &str, token: &str) -> Result<(String, String), HubError> {
    let http = client(Duration::from_secs(20));
    let health = http.get(format!("{origin}/health")).send().await?;
    if !health.status().is_success() {
        return Err(HubError::Agent(
            "That address isn't a Rook 0.5 hub (no /health). Switch to 0.4 in Settings for an older hub.".into(),
        ));
    }
    let resp = http
        .post(format!("{origin}/login"))
        .json(&json!({"token": token}))
        .send()
        .await?;
    if resp.status().as_u16() == 401 {
        return Err(HubError::Agent(
            "The hub didn't take that token. Use your own sign-in token (an agent's token won't do).".into(),
        ));
    }
    let pairs = set_cookie_pairs(
        resp.headers()
            .get_all("set-cookie")
            .iter()
            .filter_map(|v| v.to_str().ok()),
    );
    door_json(resp).await?;
    let cookie = pairs
        .iter()
        .find(|(n, _)| n == WEB_COOKIE)
        .map(|(n, v)| format!("{n}={v}"))
        .ok_or_else(|| HubError::Http(502, "no session cookie".into()))?;
    let (person, _) = web_me(origin, &cookie).await?;
    Ok((cookie, person))
}

/// Who the site session is for, and its CSRF token.
pub async fn web_me(origin: &str, cookie: &str) -> Result<(String, String), HubError> {
    let resp = client(Duration::from_secs(20))
        .get(format!("{origin}/web/api/me"))
        .header("Cookie", cookie)
        .send()
        .await?;
    let v = door_json(resp).await?;
    Ok((text(&v, "person"), text(&v, "csrf")))
}

/// End the site session (the token itself stays valid at the hub; only an
/// owner revokes tokens).
pub async fn web_sign_out(origin: &str, cookie: &str) -> Result<(), HubError> {
    let (_, csrf) = web_me(origin, cookie).await?;
    let resp = client(Duration::from_secs(20))
        .post(format!("{origin}/logout"))
        .header("Cookie", cookie)
        .header("X-Rook-CSRF", csrf)
        .send()
        .await?;
    door_json(resp).await.map(|_| ())
}

// ---- replies into the panel's items (pure) -------------------------------------------

fn short_id(s: &str) -> String {
    s.split_once(':').map(|(_, n)| n).unwrap_or(s).to_string()
}

/// `GET /holds`: `{holds: [{hold, cap, worker, why, person, agent, reasons}]}`.
pub fn hold_items(v: &Value) -> Vec<Waiting> {
    let Some(holds) = v.get("holds").and_then(Value::as_array) else {
        return vec![];
    };
    holds
        .iter()
        .filter_map(|h| {
            let name = text(h, "hold");
            if name.is_empty() {
                return None;
            }
            let who = [text(h, "agent"), text(h, "person")]
                .into_iter()
                .find(|w| !w.is_empty())
                .map(|w| short_id(&w))
                .unwrap_or_default();
            let worker = text(h, "worker");
            let title = format!(
                "{}{}",
                text(h, "cap"),
                if worker.is_empty() {
                    String::new()
                } else {
                    format!(" on {worker}")
                }
            );
            Some(Waiting {
                id: name.clone(),
                kind: "hold".into(),
                name,
                title: first_line(&title, 120),
                text: first_line(&text(h, "why"), 200),
                who,
                age: String::new(),
                link: JOURNAL_PAGE.into(),
            })
        })
        .collect()
}

/// A page id the menu may carry: `<plugin>/<page>` in plain characters.
fn safe_page(id: &str) -> bool {
    !id.is_empty()
        && !id.contains("..")
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '-' | '_' | '.'))
}

/// `page.list` grouped by section, in the hub's order. A page's `hash` is
/// `/<page id>`, so the dashboard path is `/#/<page id>`.
pub fn page_sections(v: &Value) -> Vec<Section> {
    let mut out: Vec<Section> = vec![];
    for p in v.get("pages").and_then(Value::as_array).into_iter().flatten() {
        let id = text(p, "page");
        if !safe_page(&id) {
            continue;
        }
        let title = Some(text(p, "title"))
            .filter(|t| !t.is_empty())
            .unwrap_or_else(|| id.clone());
        let section = Some(text(p, "section"))
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "Pages".into());
        let page = Page {
            hash: format!("/{id}"),
            title,
        };
        match out.iter_mut().find(|s| s.title == section) {
            Some(s) => s.pages.push(page),
            None => out.push(Section {
                id: section.to_lowercase().replace(' ', "-"),
                title: section,
                pages: vec![page],
            }),
        }
    }
    out
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

    #[test]
    fn call_results_unwrap_refusals_and_holds() {
        assert_eq!(
            call_result(json!({"id": "c", "ok": true, "result": {"a": 1}})).unwrap()["a"],
            1
        );
        assert_eq!(
            call_result(json!({"ok": false, "error": "denied: person layer"})),
            Err(HubError::Agent("Denied: person layer".into()))
        );
        let held = call_result(json!({"ok": false, "held": true, "hold": "brave-otter-lamp"}));
        assert!(matches!(held, Err(HubError::Agent(m)) if m.contains("brave-otter-lamp") && m.contains("approval")));
        assert!(call_result(json!({"ok": false})).is_err());
    }

    #[test]
    fn holds_become_waiting_items() {
        let v = json!({"ok": true, "holds": [
            {"hold": "h1", "cap": "torrent.delete", "worker": "cachyrig", "why": "Free the disk.\nMore.",
             "person": "person:bake", "agent": "agent:claude-code"},
            {"hold": "h2", "cap": "shell.exec", "worker": "laptop", "why": "", "person": "person:kid", "agent": null},
            {"cap": "no name"}]});
        let w = hold_items(&v);
        assert_eq!(w.len(), 2);
        assert_eq!(w[0].title, "torrent.delete on cachyrig");
        assert_eq!(w[0].who, "claude-code");
        assert_eq!(w[0].text, "Free the disk.");
        assert_eq!((w[0].kind.as_str(), w[0].link.as_str()), ("hold", JOURNAL_PAGE));
        assert_eq!(w[1].who, "kid");
        assert!(hold_items(&json!({"ok": true})).is_empty());
    }

    #[test]
    fn pages_group_by_section_and_drop_odd_ids() {
        let v = json!({"pages": [
            {"page": "token/tokens", "title": "Tokens", "section": "Hub"},
            {"page": "task/tasks", "title": "Tasks", "section": "Work"},
            {"page": "hub/workers", "title": "Workers", "section": "Hub"},
            {"page": "made/x y", "title": "Bad"},
            {"page": "../etc", "title": "Bad"},
            {"page": "agent/notes", "title": ""}]});
        let s = page_sections(&v);
        assert_eq!(
            s.iter().map(|s| s.title.as_str()).collect::<Vec<_>>(),
            ["Hub", "Work", "Pages"]
        );
        assert_eq!(s[0].pages.len(), 2);
        assert_eq!(s[0].pages[1].hash, "/hub/workers");
        assert_eq!(s[2].pages[0].title, "agent/notes");
    }
}
