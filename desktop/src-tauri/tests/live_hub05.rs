//! Against a running Rook 0.5 hub (docs/castling-e2e.md, "A 0.5 hub and
//! worker by hand"). Ignored by default; run with
//!
//!   ROOK_TEST_HUB05=http://127.0.0.1:8740 ROOK_TEST_TOKEN05=<a person's token> \
//!   cargo test --test live_hub05 -- --ignored --test-threads 1
//!
//! With `ROOK_TEST_PYTHON05` (a Python that has Rook 0.5 and telesthete) it
//! also adds a worker the way "Add this computer" does: it enrolls one in a
//! temp folder (approving its device code with the token), starts it, waits
//! for it on the band and stops it. Give the token of an owner or adult.
//! `ROOK_TEST_ASK05=1` also asks the house agent (an agent turn: it needs an
//! `agent.backend` worker on the band).

use rook_desktop_lib::hub05::{self, Hub05};
use rook_desktop_lib::worker05;
use std::path::PathBuf;
use std::time::{Duration, Instant};

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("set {name}"))
}

#[tokio::test]
#[ignore]
async fn live_hub05_round_trip() {
    let origin = env("ROOK_TEST_HUB05");
    let token = env("ROOK_TEST_TOKEN05");

    assert!(hub05::web_sign_in(&origin, "not-a-token").await.is_err());
    let (cookie, person) = hub05::web_sign_in(&origin, &token).await.unwrap();
    assert!(cookie.starts_with("rook_web="), "{cookie}");
    assert!(person.starts_with("person:"), "{person}");
    assert_eq!(hub05::web_me(&origin, &cookie).await.unwrap().0, person);

    let h = Hub05::new(&origin, &token);
    let bad = Hub05::new(&origin, "stale");
    assert_eq!(bad.waiting().await, Err(rook_desktop_lib::hub::HubError::NotSignedIn));
    h.waiting().await.unwrap();
    assert!(h.attention().await.unwrap().is_some());
    let pages = h.pages().await.unwrap();
    let hub = pages.iter().find(|s| s.title == "Hub").expect("the Hub section");
    assert!(hub.pages.iter().any(|p| p.hash == "/approval/approvals"), "{pages:?}");

    if std::env::var("ROOK_TEST_ASK05").is_ok() {
        let a = h
            .ask("Say hello in three words.", Duration::from_secs(150))
            .await
            .unwrap();
        assert!(!a.text.is_empty());
    }

    if let Ok(py) = std::env::var("ROOK_TEST_PYTHON05") {
        add_this_computer(&h, &origin, PathBuf::from(py)).await;
    }

    hub05::web_sign_out(&origin, &cookie).await.unwrap();
    assert!(hub05::web_me(&origin, &cookie).await.is_err());
}

async fn add_this_computer(h: &Hub05, origin: &str, python: PathBuf) {
    let dir = std::env::temp_dir().join(format!("rook-desktop-live05-{}", std::process::id()));
    let layout = worker05::Layout::new(&dir);
    let name = format!("desktop-test-{}", std::process::id());
    let (l, py, o, n, approver) = (
        layout.clone(),
        python.clone(),
        origin.to_string(),
        name.clone(),
        h.clone(),
    );
    let said = tokio::task::spawn_blocking(move || {
        worker05::enroll(&l, &py, &o, Some(&n), |code| {
            tokio::runtime::Handle::current()
                .block_on(approver.approve_device(&code))
                .map_err(|e| e.to_string())
        })
    })
    .await
    .unwrap()
    .unwrap();
    println!("{said}");
    assert!(layout.enrolled());
    // Enrolling again is refused: the worker keeps its home hub.
    let (l, py, o) = (layout.clone(), python.clone(), origin.to_string());
    let again = tokio::task::spawn_blocking(move || worker05::enroll(&l, &py, &o, None, |_| Ok(())))
        .await
        .unwrap();
    assert!(again.is_err());

    let mut c = worker05::Controller::new(layout.clone());
    assert_eq!(c.start(&python).unwrap().status, "running");
    let until = Instant::now() + Duration::from_secs(60);
    let online = loop {
        let v = h
            .call(
                "rook",
                "hub.workers",
                serde_json::json!({}),
                "Desktop app test: is the new worker online?",
                Duration::from_secs(20),
            )
            .await
            .unwrap();
        let me = v
            .as_array()
            .into_iter()
            .chain(v.get("workers").and_then(|w| w.as_array()))
            .flatten()
            .find(|w| w["name"] == name.as_str())
            .cloned();
        if me.as_ref().is_some_and(|w| w["online"] == true) || Instant::now() > until {
            break me;
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    };
    let stopped = tokio::task::spawn_blocking(move || c.stop().unwrap()).await.unwrap();
    assert_eq!(stopped.status, "stopped");
    let w = online.expect("the worker is listed");
    assert_eq!(w["online"], true, "{w}");
    let plugins = w["plugins"].to_string();
    assert!(plugins.contains("notification-desktop"), "{plugins}");
    std::fs::remove_dir_all(&dir).ok();
}
