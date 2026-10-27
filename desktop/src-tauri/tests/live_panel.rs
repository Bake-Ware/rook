//! The Rook panel's reads against a running hub (scripts/local-hub.sh with
//! ROOK_KNOWLEDGE_ENABLED=1; no home agent needed). Ignored by default:
//!
//!   ROOK_TEST_HUB=http://127.0.0.1:7005 ROOK_TEST_PASS=<dashboard password> \
//!   cargo test --test live_panel -- --ignored
//!
//! Seeds a concept and project with a task in progress and a blocked one (as the hub's
//! operator, through the dashboard's /api/band/call), then reads them back
//! the way the panel does.

use rook_desktop_lib::hub::{self, Hub};
use serde_json::{json, Value};

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("set {name}"))
}

async fn write(origin: &str, cookie: &str, args: Value) -> Value {
    let v: Value = reqwest::Client::new()
        .post(format!("{origin}/api/band/call"))
        .header("Cookie", cookie)
        .json(&json!({"cap": "task.write", "worker_id": hub::HUB_WORKER, "args": args, "timeout": 20}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(v["ok"], json!(true), "{v}");
    v["result"].clone()
}

#[tokio::test]
#[ignore]
async fn live_panel_lists() {
    let origin = env("ROOK_TEST_HUB");
    let pass = env("ROOK_TEST_PASS");
    let cookie = hub::password_sign_in(&origin, "operator", &pass).await.unwrap();
    assert!(cookie.starts_with("rook_account="), "{cookie}");
    let h = Hub::new(&origin, &cookie);
    let tag = format!("desktop-panel-{}", std::process::id());

    let concept = write(
        &origin,
        &cookie,
        json!({"action": "create", "kind": "concept", "request_id": format!("{tag}-c"),
               "data": {"title": format!("Panel concept {tag}")}}),
    )
    .await;
    let project = write(
        &origin,
        &cookie,
        json!({"action": "create", "kind": "project", "request_id": format!("{tag}-p"),
               "data": {"title": format!("Panel test {tag}"), "parent": concept["id"],
                        "attrs": {"criteria": ["the panel lists it"]}}}),
    )
    .await;
    let pid = project["id"].as_str().unwrap().to_string();
    let mut ids = vec![];
    for (n, state) in [("a", "in_progress"), ("b", "blocked")] {
        let t = write(
            &origin,
            &cookie,
            json!({"action": "create", "request_id": format!("{tag}-{n}"),
                   "data": {"title": format!("Panel task {n} {tag}"), "parent": pid,
                            "attrs": {"criteria": ["listed"]}}}),
        )
        .await;
        let id = t["id"].as_str().unwrap().to_string();
        let mut patch = json!({"state": state});
        if state == "blocked" {
            patch["attrs"] = json!({"blocked_reason": "Waiting on a signing certificate."});
        }
        write(
            &origin,
            &cookie,
            json!({"action": "update", "id": id, "request_id": format!("{tag}-{n}-u"),
                   "data": {"revision": t["revision"], "patch": patch}}),
        )
        .await;
        ids.push(id);
    }

    let work = h.work().await.unwrap();
    let mine: Vec<_> = work.iter().filter(|w| w.title.ends_with(&tag)).collect();
    assert_eq!(mine.len(), 2, "{work:?}");
    let blocked = mine.iter().find(|w| w.state == "blocked").unwrap();
    assert_eq!(blocked.note, "Waiting on a signing certificate.");
    assert!(blocked.link.starts_with("/#work?t="), "{}", blocked.link);
    assert!(mine.iter().all(|w| w.project.ends_with(&tag)));

    let found = h.knowledge(&format!("Panel task {tag}")).await.unwrap();
    assert!(found.iter().any(|f| f.title.ends_with(&tag)), "{found:?}");

    // Nothing waits on a fresh hub, but the list answers (and notifications use it).
    h.waiting().await.unwrap();
    assert!(h.attention().await.unwrap().is_some());
    // No workers, so no sessions; the account sign-in may read the list.
    h.sessions().await.unwrap();
    // The dashboard password can't read Sessions (an account page).
    let shared = hub::password_sign_in(&origin, "anyone", &pass).await.unwrap();
    assert!(Hub::new(&origin, &shared).sessions().await.is_err());
    // The update check is public.
    hub::desktop_downloads(&origin, "0.2.0", "windows").await.unwrap();
}
