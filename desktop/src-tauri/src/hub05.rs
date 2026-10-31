//! Talking to a Rook 0.5 hub (castling): the same panel, quick ask and
//! notifications as `hub` (0.4), over the 0.5 doors.
//!
//! * sign-in: the person's own door token, the one the hub site's sign-in
//!   page takes. The app posts it to the site's `/login` once, which checks
//!   it is a person's token (an agent's is refused) and gives a session
//!   cookie: `/web/api/me` names the person, and the dashboard window opens
//!   signed in with that cookie;
//! * everything else is the HTTP door's JSON API with `Authorization:
//!   Bearer <token>`: `POST /call` (every call carries a why, journaled),
//!   `GET /holds` (what waits on the person) and `POST /enroll/approve` (this
//!   computer's worker, see `worker05`). Calls run as the person, with their
//!   rights; nothing here widens them.
//!
//! The 0.4 MCP path for quick ask isn't needed: the door runs
//! `agents.chat.send` as the person, and the reply lands in the same session
//! the dashboard's dock shows.

use crate::hub::{
    client, first_line, set_cookie_pairs, text, Answer, AttentionItem, Found, HubError, Session, Waiting, WorkItem,
};
use crate::sections::{Page, Section};
use serde_json::{json, Value};
use std::time::Duration;

/// The hub's own node on the band: workbench, agents and pages caps live there.
pub const HUB_WORKER: &str = "rook";
/// The ready-made house agent (rook/plugins/agents), the dashboard dock's agent.
pub const HOUSE: &str = "house";
/// The hub site's session cookie (rook/core2/hub/doors/web/site.py COOKIE).
pub const WEB_COOKIE: &str = "rook_web";
/// Dashboard pages (`#/<plugin>/<page>`, rook/core2/hub/doors/web/static/shell.js).
pub const APPROVALS_PAGE: &str = "/#/approval/approvals";
pub const TASKS_PAGE: &str = "/#/task/tasks";
pub const KNOWLEDGE_PAGE: &str = "/#/knowledge/knowledge";

#[derive(Clone)]
pub struct Hub05 {
    pub origin: String,
    token: String,
    http: reqwest::Client,
}

impl Hub05 {
    pub fn new(origin: &str, token: &str) -> Self {
        Hub05 {
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

    /// Tasks in progress or blocked (`task.list` per state), with their projects' titles.
    pub async fn work(&self) -> Result<Vec<WorkItem>, HubError> {
        let why = "Desktop app: the Rook panel lists work in progress.";
        let list =
            |state: &'static str| self.hub_call("task.list", json!({"where": {"state": state}, "limit": 50}), why);
        let projects = self.hub_call("project.list", json!({"limit": 200}), why);
        let (active, blocked, projects) = tokio::join!(list("in_progress"), list("blocked"), projects);
        // A hub without the projects plugin still lists its tasks.
        Ok(task_items(&blocked?, &active?, &projects.unwrap_or(Value::Null)))
    }

    /// Knowledge pages, then tasks, matching the words.
    pub async fn knowledge(&self, query: &str) -> Result<Vec<Found>, HubError> {
        let why = "Desktop app: a knowledge search from the Rook panel.";
        let (pages, tasks) = tokio::join!(
            self.hub_call("knowledge.search", json!({"query": query, "limit": 10}), why),
            self.hub_call("task.search", json!({"query": query, "limit": 5}), why),
        );
        let mut out = found_items(&pages?, "knowledge", KNOWLEDGE_PAGE);
        out.extend(found_items(&tasks.unwrap_or(Value::Null), "task", TASKS_PAGE));
        Ok(out)
    }

    /// The hub's agents, awake ones first (`agents.list`).
    pub async fn sessions(&self) -> Result<Vec<Session>, HubError> {
        let v = self
            .hub_call(
                "agents.list",
                json!({}),
                "Desktop app: the Rook panel lists the agents.",
            )
            .await?;
        Ok(agent_items(&v))
    }

    /// The dashboard's pages by section (`pages.list`), for the tray and the panel.
    pub async fn pages(&self) -> Result<Vec<Section>, HubError> {
        let v = self
            .hub_call(
                "pages.list",
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
                "agents.chat.send",
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

    /// Approve a device code (`POST /enroll/approve`) as the signed-in person.
    pub async fn approve_device(&self, code: &str) -> Result<(), HubError> {
        let v = self
            .json(
                self.req(reqwest::Method::POST, "/enroll/approve")
                    .json(&json!({"code": code})),
            )
            .await?;
        call_result(v).map(|_| ())
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

/// `{ok, result | error}`, or a held call `{held, hold}` (spec 10.3).
pub fn call_result(v: Value) -> Result<Value, HubError> {
    if v.get("held").and_then(Value::as_bool) == Some(true) {
        return Err(HubError::Agent(format!(
            "This waits for your approval ({}). Approve it on the dashboard's Approvals page.",
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
                link: APPROVALS_PAGE.into(),
            })
        })
        .collect()
}

/// `task.list` rows (records): blocked first, then in progress.
pub fn task_items(blocked: &Value, active: &Value, projects: &Value) -> Vec<WorkItem> {
    let project = |id: &str| -> String {
        projects
            .as_array()
            .into_iter()
            .flatten()
            .find(|p| text(p, "id") == id)
            .map(|p| text(p, "title"))
            .unwrap_or_default()
    };
    let mut out = vec![];
    for (state, rows) in [("blocked", blocked), ("in_progress", active)] {
        for t in rows.as_array().into_iter().flatten() {
            let title = text(t, "title");
            if title.is_empty() {
                continue;
            }
            let note = if state == "blocked" {
                text(t, "assignee")
            } else {
                t.get("claims")
                    .and_then(Value::as_array)
                    .map(|cs| {
                        cs.iter()
                            .map(|c| short_id(&text(c, "actor")))
                            .filter(|a| !a.is_empty())
                            .collect::<Vec<_>>()
                            .join(", ")
                    })
                    .unwrap_or_default()
            };
            out.push(WorkItem {
                title,
                state: state.into(),
                project: project(&text(t, "project")),
                note,
                link: TASKS_PAGE.into(),
            });
        }
    }
    out
}

/// `knowledge.search` / `task.search` rows (records).
pub fn found_items(v: &Value, kind: &str, link: &str) -> Vec<Found> {
    v.as_array()
        .into_iter()
        .flatten()
        .filter_map(|r| {
            let title = text(r, "title");
            (!title.is_empty()).then(|| Found {
                excerpt: first_line(&text(r, "body").replace("**", ""), 220),
                title,
                kind: kind.into(),
                link: link.into(),
            })
        })
        .collect()
}

/// `agents.list`: live (awake) agents first; archived ones left out.
pub fn agent_items(v: &Value) -> Vec<Session> {
    let mut out: Vec<Session> = v
        .as_array()
        .into_iter()
        .flatten()
        .filter(|a| a.get("archived").map_or(true, Value::is_null))
        .filter_map(|a| {
            let name = text(a, "name");
            if name.is_empty() {
                return None;
            }
            let awake = a.get("awake").and_then(Value::as_bool) == Some(true);
            Some(Session {
                title: Some(text(a, "title"))
                    .filter(|t| !t.is_empty())
                    .unwrap_or_else(|| name.clone()),
                agent: name,
                worker: HUB_WORKER.into(),
                state: if awake { "live" } else { "idle" }.into(),
                cwd: first_line(&text(a, "summary"), 200),
                updated: 0.0,
                link: "/".into(),
            })
        })
        .collect();
    out.sort_by_key(|s| s.state != "live");
    out
}

/// A page id the menu may carry: `<plugin>/<page>` in plain characters.
fn safe_page(id: &str) -> bool {
    !id.is_empty()
        && !id.contains("..")
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '-' | '_' | '.'))
}

/// `pages.list` grouped by section, in the hub's order. A page's `hash` is
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
        assert!(matches!(held, Err(HubError::Agent(m)) if m.contains("brave-otter-lamp") && m.contains("Approvals")));
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
        assert_eq!((w[0].kind.as_str(), w[0].link.as_str()), ("hold", APPROVALS_PAGE));
        assert_eq!(w[1].who, "kid");
        assert!(hold_items(&json!({"ok": true})).is_empty());
    }

    #[test]
    fn tasks_with_project_titles() {
        let active = json!([{"id": "r1", "title": "Port the desktop app", "state": "in_progress",
            "project": "p1", "claims": [{"actor": "agent:claude-code", "person": "person:bake"}]}]);
        let blocked = json!([{"id": "r2", "title": "Sign the app", "state": "blocked", "project": "px",
            "assignee": "bake"}, {"id": "r3", "title": ""}]);
        let projects = json!([{"id": "p1", "title": "Castling"}]);
        let w = task_items(&blocked, &active, &projects);
        assert_eq!(w.len(), 2);
        assert_eq!(
            (w[0].title.as_str(), w[0].state.as_str(), w[0].note.as_str()),
            ("Sign the app", "blocked", "bake")
        );
        assert_eq!(w[0].project, "");
        assert_eq!((w[1].project.as_str(), w[1].note.as_str()), ("Castling", "claude-code"));
        assert_eq!(w[1].link, TASKS_PAGE);
        assert!(task_items(&Value::Null, &Value::Null, &Value::Null).is_empty());
    }

    #[test]
    fn search_hits_and_agents() {
        let f = found_items(
            &json!([{"title": "Desktop", "body": "**Preview:** new.\nmore"}, {"title": ""}]),
            "knowledge",
            KNOWLEDGE_PAGE,
        );
        assert_eq!(f.len(), 1);
        assert_eq!(
            (f[0].excerpt.as_str(), f[0].link.as_str()),
            ("Preview: new.", KNOWLEDGE_PAGE)
        );
        let a = agent_items(&json!([
            {"name": "pm", "title": "Project manager", "summary": "Sweeps tickets.", "awake": false, "archived": null},
            {"name": "house", "title": "House agent", "awake": true, "archived": null},
            {"name": "old", "awake": true, "archived": 1793400000.0}]));
        assert_eq!(a.iter().map(|s| s.agent.as_str()).collect::<Vec<_>>(), ["house", "pm"]);
        assert_eq!((a[0].state.as_str(), a[1].cwd.as_str()), ("live", "Sweeps tickets."));
    }

    #[test]
    fn pages_group_by_section_and_drop_odd_ids() {
        let v = json!({"pages": [
            {"page": "approval/approvals", "title": "Approvals", "section": "Hub"},
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
