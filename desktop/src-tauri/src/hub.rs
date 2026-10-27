//! Talking to the hub: sign-in, the home agent, chat and "needs you" items.
//!
//! The app uses the dashboard's own HTTP API with the same session cookie a
//! browser gets from the normal sign-in, so the hub needs nothing new:
//!
//! * sign-in: the account login form (`/account/login`, giving `rook_account`)
//!   or the dashboard password (`/login`, giving `rook_session`), or a cookie
//!   read from the dashboard window after the person signs in there;
//! * the home agent: `home.ask` is the API `rook_home_ask` uses, but the hub
//!   deliberately refuses write-risk caps over the band, so the dashboard's
//!   `/api/band/call` cannot reach it. With an agent token (Tokens page) the
//!   app calls it exactly as `rook_home_ask` does, over MCP. Without one it
//!   asks in a private two-person chat room with the agent, which the home
//!   agent answers the same way (and the thread shows up in Chat);
//! * notifications: `/api/chat/rooms` (read-only: unread counts and the last
//!   message, never moving the read mark) and, when a hub serves it,
//!   `/api/attention` (huddle questions, tasks that need you).

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::time::Duration;

/// The hub's own node on the band; home.* lives there.
pub const HUB_WORKER: &str = "rook";
/// Who the dashboard API acts as in chat.
pub const OPERATOR: &str = "user:operator";
pub const QUICK_ROOM_TITLE: &str = "Quick ask (desktop)";
/// What the home agent posts when it cannot answer, and while it is busy
/// (rook/hub/plugins/home: UNAVAILABLE, BUSY).
const AGENT_UNAVAILABLE: &str = "The home agent is unavailable right now.";
const AGENT_BUSY_PREFIX: &str = "(Busy:";

#[derive(Debug, Clone, PartialEq)]
pub enum HubError {
    /// No stored sign-in, or the hub no longer accepts it.
    NotSignedIn,
    /// Signed in, but not as the hub's operator (the dashboard API is theirs).
    NotOperator,
    BadLogin,
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
            HubError::NotOperator => write!(
                f,
                "This login can't use the dashboard. Sign in with the hub owner's account or the dashboard password."
            ),
            HubError::BadLogin => write!(f, "That user name or password didn't work."),
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

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Whoami {
    #[serde(default)]
    pub principal: Option<String>,
    #[serde(default)]
    pub role: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct HomeStatus {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub identity: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub missing: Vec<String>,
    #[serde(default)]
    pub chat: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Answer {
    pub text: String,
    /// "agent" (home.ask over MCP) or "chat".
    pub via: &'static str,
    pub agent: String,
    pub room: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Room {
    pub room: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub participants: Vec<String>,
    #[serde(default)]
    pub member: bool,
    #[serde(default)]
    pub unread: u64,
    #[serde(default)]
    pub last_sender: Option<String>,
    #[serde(default)]
    pub last_text: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Message {
    pub seq: u64,
    #[serde(default)]
    pub sender: String,
    #[serde(default)]
    pub text: String,
}

/// One thing that needs the person: a huddle question or a task waiting on
/// them. The `/api/attention` contract (docs/desktop-app.md).
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

#[derive(Clone)]
pub struct Hub {
    pub origin: String,
    cookie: String,
    http: reqwest::Client,
}

fn client(timeout: Duration) -> reqwest::Client {
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

impl Hub {
    pub fn new(origin: &str, cookie: &str) -> Self {
        Hub {
            origin: origin.to_string(),
            cookie: cookie.to_string(),
            http: client(Duration::from_secs(20)),
        }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.origin, path)
    }

    fn req(&self, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        let mut r = self
            .http
            .request(method, self.url(path))
            .header("Accept", "application/json");
        if !self.cookie.is_empty() {
            r = r.header("Cookie", &self.cookie);
        }
        r
    }

    async fn json(&self, r: reqwest::RequestBuilder) -> Result<Value, HubError> {
        if self.cookie.is_empty() {
            return Err(HubError::NotSignedIn);
        }
        let resp = r.send().await?;
        let status = resp.status().as_u16();
        // The dashboard answers an unknown login with 401, and sends an
        // account that isn't the operator to /account.
        if status == 401 {
            return Err(HubError::NotSignedIn);
        }
        if (300..400).contains(&status) {
            let to = resp
                .headers()
                .get("location")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            return Err(if to.starts_with("/account") {
                HubError::NotOperator
            } else {
                HubError::NotSignedIn
            });
        }
        let body = resp.text().await?;
        if !(200..300).contains(&status) {
            let msg = serde_json::from_str::<Value>(&body)
                .ok()
                .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
                .unwrap_or_default();
            return Err(HubError::Http(status, msg));
        }
        serde_json::from_str(&body).map_err(|_| HubError::Http(status, "not JSON".into()))
    }

    /// End this session at the hub (`/logout` drops the account session and
    /// the dashboard-password session, whichever the cookie is). The hub
    /// answers with a redirect to its login page.
    pub async fn logout(&self) -> Result<(), HubError> {
        if self.cookie.is_empty() {
            return Ok(());
        }
        let resp = self.req(reqwest::Method::GET, "/logout").send().await?;
        let status = resp.status().as_u16();
        if (200..400).contains(&status) || status == 401 {
            Ok(())
        } else {
            Err(HubError::Http(status, String::new()))
        }
    }

    pub async fn whoami(&self) -> Result<Whoami, HubError> {
        let v = self.json(self.req(reqwest::Method::GET, "/api/band/whoami")).await?;
        Ok(serde_json::from_value(v).unwrap_or_default())
    }

    /// A read-risk band call on the hub's node (allowed over the band).
    async fn hub_call(&self, cap: &str, args: Value, timeout_s: f64) -> Result<Value, HubError> {
        let body = json!({"cap": cap, "worker_id": HUB_WORKER, "args": args, "timeout": timeout_s});
        let v = self
            .json(self.req(reqwest::Method::POST, "/api/band/call").json(&body))
            .await?;
        band_result(v)
    }

    pub async fn home_status(&self) -> Result<HomeStatus, HubError> {
        let v = self.hub_call("home.status", json!({}), 10.0).await?;
        Ok(serde_json::from_value(v).unwrap_or_default())
    }

    pub async fn chat_rooms(&self) -> Result<Vec<Room>, HubError> {
        let v = self.json(self.req(reqwest::Method::GET, "/api/chat/rooms")).await?;
        Ok(v.get("rooms")
            .cloned()
            .and_then(|r| serde_json::from_value(r).ok())
            .unwrap_or_default())
    }

    /// `None` when this hub has no `/api/attention` yet.
    pub async fn attention(&self) -> Result<Option<Vec<AttentionItem>>, HubError> {
        match self.json(self.req(reqwest::Method::GET, "/api/attention")).await {
            Ok(v) => Ok(Some(
                v.get("items")
                    .cloned()
                    .and_then(|i| serde_json::from_value(i).ok())
                    .unwrap_or_default(),
            )),
            Err(HubError::Http(404, _)) | Err(HubError::Http(405, _)) => Ok(None),
            Err(e) => Err(e),
        }
    }

    async fn chat_read(&self, room: &str, since: u64) -> Result<(Vec<Message>, u64), HubError> {
        let path = format!("/api/chat/read?room={}&since={since}", urlencode(room));
        let v = self.json(self.req(reqwest::Method::GET, &path)).await?;
        if v.get("ok").and_then(Value::as_bool) == Some(false) {
            return Err(HubError::Http(
                404,
                v.get("error").and_then(Value::as_str).unwrap_or("").into(),
            ));
        }
        let msgs: Vec<Message> = v
            .get("messages")
            .cloned()
            .and_then(|m| serde_json::from_value(m).ok())
            .unwrap_or_default();
        let last = v.get("last_seq").and_then(Value::as_u64).unwrap_or(since);
        Ok((msgs, last))
    }

    /// The room's last message seq. The read API pages oldest-first (200 at
    /// a time), so walk it; the quick-ask room is small.
    async fn chat_tail(&self, room: &str) -> Result<u64, HubError> {
        let mut since = 0;
        for _ in 0..50 {
            let (msgs, last) = self.chat_read(room, since).await?;
            if msgs.len() < 200 || last <= since {
                return Ok(last.max(since));
            }
            since = last;
        }
        Ok(since)
    }

    /// Ask the home agent in a private room with it, and wait for its reply.
    /// `room_hint` is the room used last time (kept by the caller).
    pub async fn ask_via_chat(
        &self,
        question: &str,
        room_hint: Option<&str>,
        wait: Duration,
    ) -> Result<Answer, HubError> {
        let status = self.home_status().await?;
        if !status.enabled {
            return Err(HubError::Agent(agent_off_message(&status)));
        }
        if !status.chat {
            return Err(HubError::Agent(
                "Chat is off on this hub, so the home agent can't be reached from here.".into(),
            ));
        }
        let agent = if status.identity.is_empty() {
            "agent:home".to_string()
        } else {
            status.identity.clone()
        };
        let rooms = self.chat_rooms().await?;
        let room = match room_hint.filter(|r| rooms.iter().any(|x| x.room == *r && is_quick_room(x, &agent))) {
            Some(r) => r.to_string(),
            None => match find_quick_room(&rooms, &agent) {
                Some(r) => r,
                None => {
                    let body = json!({"title": QUICK_ROOM_TITLE, "invite": [agent]});
                    let v = self
                        .json(self.req(reqwest::Method::POST, "/api/chat/start").json(&body))
                        .await?;
                    v.get("room").and_then(Value::as_str).map(String::from).ok_or_else(|| {
                        HubError::Http(
                            500,
                            v.get("error")
                                .and_then(Value::as_str)
                                .unwrap_or("could not open a chat room")
                                .into(),
                        )
                    })?
                }
            },
        };
        let before = self.chat_tail(&room).await?;
        let sent = self
            .json(
                self.req(reqwest::Method::POST, "/api/chat/send")
                    .json(&json!({"room": room, "text": question})),
            )
            .await?;
        if sent.get("ok").and_then(Value::as_bool) == Some(false) {
            return Err(HubError::Http(
                500,
                sent.get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("send failed")
                    .into(),
            ));
        }
        let deadline = tokio::time::Instant::now() + wait;
        let mut since = before;
        loop {
            tokio::time::sleep(Duration::from_millis(1200)).await;
            let (msgs, last) = self.chat_read(&room, since).await?;
            since = last.max(since);
            match agent_reply(&msgs, &agent) {
                ReplyState::Answer(text) => {
                    return Ok(Answer {
                        text,
                        via: "chat",
                        agent,
                        room: Some(room),
                    });
                }
                ReplyState::Unavailable => {
                    return Err(HubError::Agent(
                        "The home agent couldn't answer (its model is unavailable). Check Intelligence sources > Home agent.".into(),
                    ))
                }
                ReplyState::Waiting => {}
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(HubError::Timeout);
            }
        }
    }

    /// `home.ask` exactly as `rook_home_ask` calls it: over the hub's MCP
    /// endpoint with an agent token, as `rook_call(worker="rook", cap="home.ask")`.
    pub async fn ask_via_mcp(&self, token: &str, question: &str, wait: Duration) -> Result<Answer, HubError> {
        let http = client(wait + Duration::from_secs(10));
        let mcp = Mcp {
            http: &http,
            url: self.url("/mcp"),
            token,
        };
        let session = mcp.initialize().await?;
        let args = json!({"name": "rook_call", "arguments": {
            "worker": HUB_WORKER, "cap": "home.ask", "args": {"question": question},
            "why": "Desktop app: the owner asks the house agent",
            "timeout": wait.as_secs_f64()}});
        let result = mcp.rpc(session.as_deref(), 2, "tools/call", args).await?;
        let text = tool_text(&result).ok_or_else(|| HubError::Http(502, "empty tool result".into()))?;
        let reply: Value = serde_json::from_str(&text).unwrap_or(Value::String(text.clone()));
        let inner = band_result(reply)?;
        let answer = inner
            .get("answer")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let agent = inner
            .get("agent")
            .and_then(Value::as_str)
            .unwrap_or("agent:home")
            .to_string();
        Ok(Answer {
            text: answer,
            via: "agent",
            agent,
            room: None,
        })
    }
}

struct Mcp<'a> {
    http: &'a reqwest::Client,
    url: String,
    token: &'a str,
}

impl Mcp<'_> {
    async fn post(&self, session: Option<&str>, body: Value) -> Result<(Option<String>, String), HubError> {
        let mut r = self
            .http
            .post(&self.url)
            .header("Accept", "application/json, text/event-stream")
            .header("Authorization", format!("Bearer {}", self.token))
            .json(&body);
        if let Some(s) = session {
            r = r.header("Mcp-Session-Id", s);
        }
        let resp = r.send().await?;
        let status = resp.status().as_u16();
        if status == 401 || status == 403 {
            return Err(HubError::Agent(
                "The hub refused the agent token. Make a new one on the Tokens page.".into(),
            ));
        }
        let sid = resp
            .headers()
            .get("mcp-session-id")
            .and_then(|v| v.to_str().ok())
            .map(String::from);
        let text = resp.text().await?;
        if !(200..300).contains(&status) {
            return Err(HubError::Http(status, text.chars().take(160).collect()));
        }
        Ok((sid, text))
    }

    async fn initialize(&self) -> Result<Option<String>, HubError> {
        let (sid, body) = self
            .post(
                None,
                json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "rook-desktop", "version": env!("CARGO_PKG_VERSION")}}}),
            )
            .await?;
        rpc_result(&body, 1)?;
        self.post(
            sid.as_deref(),
            json!({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        )
        .await?;
        Ok(sid)
    }

    async fn rpc(&self, session: Option<&str>, id: u64, method: &str, params: Value) -> Result<Value, HubError> {
        let (_, body) = self
            .post(
                session,
                json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}),
            )
            .await?;
        rpc_result(&body, id)
    }
}

/// The JSON-RPC result with `id`, from a plain JSON body or an SSE stream
/// (`data: {...}` lines), which the MCP endpoint may answer with.
pub fn rpc_result(body: &str, id: u64) -> Result<Value, HubError> {
    let mut candidates: Vec<Value> = Vec::new();
    if let Ok(v) = serde_json::from_str::<Value>(body.trim()) {
        candidates.push(v);
    } else {
        for line in body.lines() {
            if let Some(data) = line.strip_prefix("data:") {
                if let Ok(v) = serde_json::from_str::<Value>(data.trim()) {
                    candidates.push(v);
                }
            }
        }
    }
    for v in candidates {
        if v.get("id").and_then(Value::as_u64) != Some(id) {
            continue;
        }
        if let Some(err) = v.get("error") {
            let msg = err.get("message").and_then(Value::as_str).unwrap_or("MCP error");
            return Err(HubError::Agent(msg.to_string()));
        }
        return Ok(v.get("result").cloned().unwrap_or(Value::Null));
    }
    Err(HubError::Http(502, "no reply from the hub's MCP endpoint".into()))
}

/// The text of an MCP tool result (`content[0].text`), flagging tool errors.
pub fn tool_text(result: &Value) -> Option<String> {
    let text = result
        .get("content")?
        .as_array()?
        .iter()
        .find_map(|c| c.get("text").and_then(Value::as_str))?;
    Some(text.to_string())
}

/// Unwrap a band reply `{ok, result | error}`; errors come back as plain text.
pub fn band_result(v: Value) -> Result<Value, HubError> {
    match v.get("ok").and_then(Value::as_bool) {
        Some(true) => Ok(v.get("result").cloned().unwrap_or(Value::Null)),
        Some(false) => {
            let err = v.get("error").and_then(Value::as_str).unwrap_or("the call failed");
            Err(HubError::Agent(friendly_agent_error(err)))
        }
        None => Ok(v),
    }
}

fn friendly_agent_error(err: &str) -> String {
    if err.contains("home agent is off") {
        "The home agent is off. Turn it on in the dashboard: Intelligence sources > Home agent.".into()
    } else if err.contains("rate limited") {
        "Too many questions this minute. Try again shortly.".into()
    } else if err.contains("no worker") || err.contains("not on the band") {
        "The hub's home agent isn't reachable right now.".into()
    } else {
        let mut s: String = err.chars().take(200).collect();
        if let Some(first) = s.get(0..1) {
            s = first.to_uppercase() + &s[1..];
        }
        s
    }
}

pub fn agent_off_message(status: &HomeStatus) -> String {
    if !status.missing.is_empty() {
        "The home agent isn't set up yet. Pick its model in the dashboard: Intelligence sources > Home agent.".into()
    } else {
        "The home agent is off. Turn it on in the dashboard: Intelligence sources > Home agent.".into()
    }
}

fn is_quick_room(r: &Room, agent: &str) -> bool {
    r.member
        && r.participants.len() == 2
        && r.participants.iter().any(|p| p == OPERATOR)
        && r.participants.iter().any(|p| p == agent)
        && r.title == QUICK_ROOM_TITLE
}

/// The quick-ask room with this agent, if one exists (newest first, as the
/// hub lists them).
pub fn find_quick_room(rooms: &[Room], agent: &str) -> Option<String> {
    rooms.iter().find(|r| is_quick_room(r, agent)).map(|r| r.room.clone())
}

#[derive(Debug, PartialEq)]
pub enum ReplyState {
    Waiting,
    Answer(String),
    Unavailable,
}

/// What the agent has said since the question: a busy note means keep
/// waiting; its "unavailable" note means stop.
pub fn agent_reply(msgs: &[Message], agent: &str) -> ReplyState {
    for m in msgs.iter().filter(|m| m.sender == agent) {
        let t = m.text.trim();
        if t.starts_with(AGENT_BUSY_PREFIX) {
            continue;
        }
        if t == AGENT_UNAVAILABLE {
            return ReplyState::Unavailable;
        }
        return ReplyState::Answer(t.to_string());
    }
    ReplyState::Waiting
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

/// The hub session among some cookies: the account session wins over the
/// shared dashboard password's session.
pub fn session_cookie(pairs: &[(String, String)]) -> Option<String> {
    for want in ["rook_account", "rook_session"] {
        if let Some((n, v)) = pairs.iter().find(|(n, _)| n == want) {
            return Some(format!("{n}={v}"));
        }
    }
    None
}

/// The hidden `form_token` on the account sign-in page.
pub fn form_token(html: &str) -> Option<String> {
    let at = html.find("name=\"form_token\"")?;
    let rest = &html[at..];
    let v = rest.find("value=\"")? + 7;
    let end = rest[v..].find('"')?;
    Some(rest[v..v + end].to_string())
}

/// Sign in with a user name and password the way the browser pages do, and
/// return the session cookie. Tries the account login first, then the
/// dashboard password (any user name).
pub async fn password_sign_in(origin: &str, user: &str, pass: &str) -> Result<String, HubError> {
    let http = client(Duration::from_secs(20));
    // 1. Account login: GET the form for its token (also set as a cookie).
    let page = http.get(format!("{origin}/account/login")).send().await?;
    if page.status().is_success() {
        let form_cookie = set_cookie_pairs(
            page.headers()
                .get_all("set-cookie")
                .iter()
                .filter_map(|v| v.to_str().ok()),
        )
        .into_iter()
        .find(|(n, _)| n == "rook_login_form");
        let html = page.text().await.unwrap_or_default();
        if let (Some((n, v)), Some(token)) = (form_cookie, form_token(&html)) {
            let resp = http
                .post(format!("{origin}/account/login"))
                .header("Cookie", format!("{n}={v}"))
                .form(&[("form_token", token.as_str()), ("username", user), ("password", pass)])
                .send()
                .await?;
            if resp.status().is_redirection() {
                let pairs = set_cookie_pairs(
                    resp.headers()
                        .get_all("set-cookie")
                        .iter()
                        .filter_map(|v| v.to_str().ok()),
                );
                if let Some(c) = pairs.iter().find(|(n, _)| n == "rook_account") {
                    return Ok(format!("{}={}", c.0, c.1));
                }
            } else if resp.status().as_u16() == 429 {
                return Err(HubError::Http(429, "too many attempts; wait a minute".into()));
            }
        }
    }
    // 2. The dashboard password.
    let resp = http
        .post(format!("{origin}/login"))
        .form(&[("user", user), ("pass", pass)])
        .send()
        .await?;
    if resp.status().is_redirection() {
        let pairs = set_cookie_pairs(
            resp.headers()
                .get_all("set-cookie")
                .iter()
                .filter_map(|v| v.to_str().ok()),
        );
        if let Some(c) = pairs.iter().find(|(n, _)| n == "rook_session") {
            return Ok(format!("{}={}", c.0, c.1));
        }
    }
    Err(HubError::BadLogin)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cookies_are_parsed_and_account_wins() {
        let pairs = set_cookie_pairs([
            "rook_session=abc; Path=/; HttpOnly; SameSite=Lax",
            "rook_account=\"xyz\"; Max-Age=2592000; Secure",
            "junk",
            "empty=; Path=/",
        ]);
        assert_eq!(pairs.len(), 2);
        assert_eq!(session_cookie(&pairs).unwrap(), "rook_account=xyz");
        assert_eq!(session_cookie(&pairs[..1]).unwrap(), "rook_session=abc");
        assert_eq!(session_cookie(&[]), None);
    }

    #[test]
    fn form_token_is_found() {
        let html = r#"<form><input type="hidden" name="form_token" value="t0k-en_1"><input name="username"></form>"#;
        assert_eq!(form_token(html).unwrap(), "t0k-en_1");
        assert_eq!(form_token("<p>nothing</p>"), None);
    }

    #[test]
    fn rpc_result_reads_json_and_sse() {
        let plain = r#"{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"ok\":true}"}]}}"#;
        assert_eq!(tool_text(&rpc_result(plain, 2).unwrap()).unwrap(), "{\"ok\":true}");
        let sse = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"protocolVersion\":\"x\"}}\n\n";
        assert_eq!(rpc_result(sse, 1).unwrap()["protocolVersion"], "x");
        let err = r#"{"jsonrpc":"2.0","id":3,"error":{"code":-32602,"message":"bad args"}}"#;
        assert_eq!(rpc_result(err, 3), Err(HubError::Agent("bad args".into())));
        assert!(rpc_result(plain, 9).is_err());
    }

    #[test]
    fn band_results_unwrap_and_explain() {
        assert_eq!(
            band_result(json!({"ok": true, "result": {"answer": "hi"}})).unwrap()["answer"],
            "hi"
        );
        let off =
            band_result(json!({"ok": false, "error": "the home agent is off (Intelligence sources > Home agent)"}));
        assert!(matches!(off, Err(HubError::Agent(m)) if m.contains("Turn it on")));
        let other = band_result(json!({"ok": false, "error": "worker said no"}));
        assert_eq!(other, Err(HubError::Agent("Worker said no".into())));
    }

    fn room(id: &str, title: &str, parts: &[&str], member: bool) -> Room {
        Room {
            room: id.into(),
            title: title.into(),
            participants: parts.iter().map(|s| s.to_string()).collect(),
            member,
            ..Default::default()
        }
    }

    #[test]
    fn quick_room_is_the_private_one_with_the_agent() {
        let rooms = vec![
            room("a", "general", &[OPERATOR, "agent:home", "agent:x"], true),
            room("b", QUICK_ROOM_TITLE, &[OPERATOR, "agent:other"], true),
            room("c", QUICK_ROOM_TITLE, &[OPERATOR, "agent:home"], false),
            room("d", QUICK_ROOM_TITLE, &[OPERATOR, "agent:home"], true),
        ];
        assert_eq!(find_quick_room(&rooms, "agent:home").as_deref(), Some("d"));
        assert_eq!(find_quick_room(&rooms, "agent:nobody"), None);
    }

    #[test]
    fn agent_reply_skips_busy_and_others() {
        let m = |seq, s: &str, t: &str| Message {
            seq,
            sender: s.into(),
            text: t.into(),
        };
        assert_eq!(agent_reply(&[m(1, OPERATOR, "hi")], "agent:home"), ReplyState::Waiting);
        assert_eq!(
            agent_reply(&[m(2, "agent:home", "(Busy: I will answer shortly.)")], "agent:home"),
            ReplyState::Waiting
        );
        assert_eq!(
            agent_reply(
                &[
                    m(2, "agent:home", "(Busy: I will answer shortly.)"),
                    m(3, "agent:home", " Hello! ")
                ],
                "agent:home"
            ),
            ReplyState::Answer("Hello!".into())
        );
        assert_eq!(
            agent_reply(&[m(4, "agent:home", AGENT_UNAVAILABLE)], "agent:home"),
            ReplyState::Unavailable
        );
    }

    #[test]
    fn errors_read_plainly() {
        assert!(HubError::NotSignedIn.to_string().contains("Sign in"));
        assert_eq!(
            HubError::Http(500, String::new()).to_string(),
            "The hub answered HTTP 500."
        );
        let s = HomeStatus {
            missing: vec!["model".into()],
            ..Default::default()
        };
        assert!(agent_off_message(&s).contains("isn't set up"));
    }
}
