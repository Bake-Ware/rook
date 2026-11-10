//! Against a running Rook 0.5 hub (docs/castling-e2e.md, "A 0.5 hub and
//! worker by hand"). Ignored by default; run with
//!
//!   ROOK_TEST_HUB05=http://127.0.0.1:8740 ROOK_TEST_TOKEN05=<a person's token> \
//!   cargo test --test live_hub05 -- --ignored --test-threads 1
//!
//! With `ROOK_TEST_ADD=1` it also adds a worker the way "Add this computer"
//! does: it enrolls one in a temp folder with a one-time code the token
//! gets, starts it in this process, waits for it on the band and stops it.
//! Give the token of an owner or adult.
//! `ROOK_TEST_ASK05=1` also asks the house agent (an agent turn: it needs an
//! `agent.backend` worker on the band).

use rook_desktop_lib::hub::{self, Hub};
use rook_desktop_lib::worker;
use std::time::{Duration, Instant};

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("set {name}"))
}

#[tokio::test]
#[ignore]
async fn live_hub05_round_trip() {
    let origin = env("ROOK_TEST_HUB05");
    let token = env("ROOK_TEST_TOKEN05");

    assert!(hub::web_sign_in(&origin, "not-a-token").await.is_err());
    let (cookie, person) = hub::web_sign_in(&origin, &token).await.unwrap();
    assert!(cookie.starts_with("rook_web="), "{cookie}");
    assert!(person.starts_with("person:"), "{person}");
    assert_eq!(hub::web_me(&origin, &cookie).await.unwrap().0, person);

    let h = Hub::new(&origin, &token);
    let bad = Hub::new(&origin, "stale");
    assert_eq!(bad.waiting().await, Err(rook_desktop_lib::hub::HubError::NotSignedIn));
    h.waiting().await.unwrap();
    assert!(h.attention().await.unwrap().is_some());
    let pages = h.pages().await.unwrap();
    let hub = pages.iter().find(|s| s.title == "Hub").expect("the Hub section");
    assert!(hub.pages.iter().any(|p| p.hash == "/hub/workers"), "{pages:?}");

    if std::env::var("ROOK_TEST_ASK05").is_ok() {
        let a = h
            .ask("Say hello in three words.", Duration::from_secs(150))
            .await
            .unwrap();
        assert!(!a.text.is_empty());
    }

    if std::env::var("ROOK_TEST_ADD").is_ok() {
        add_this_computer(&h, &origin).await;
    }

    hub::web_sign_out(&origin, &cookie).await.unwrap();
    assert!(hub::web_me(&origin, &cookie).await.is_err());
}

async fn add_this_computer(h: &Hub, origin: &str) {
    let dir = std::env::temp_dir().join(format!("rook-desktop-live05-{}", std::process::id()));
    let layout = worker::Layout::new(&dir);
    let name = format!("desktop-test-{}", std::process::id());
    let code = h.join_code().await.unwrap();
    let said = worker::enroll(&layout, origin, &code, Some(&name)).await.unwrap();
    println!("{said}");
    assert!(layout.enrolled());
    // Enrolling again is refused: the worker keeps its home hub.
    assert!(worker::enroll(&layout, origin, &code, None).await.is_err());

    let mut c = worker::Controller::new(layout.clone());
    assert_eq!(c.start().unwrap().status, "running");
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
    let busy = c.info().detail;
    assert!(busy.starts_with("Running in the app."), "{busy}");
    assert_eq!(c.stop().unwrap().status, "stopped");
    let w = online.expect("the worker is listed");
    assert_eq!(w["online"], true, "{w}");
    let plugins = w["plugins"].to_string();
    assert!(plugins.contains("shell") && plugins.contains("file"), "{plugins}");
    assert!(
        w["unavailable"]["worker.update"]
            .as_str()
            .unwrap_or("")
            .contains("desktop app"),
        "{w}"
    );
    std::fs::remove_dir_all(&dir).ok();
}
