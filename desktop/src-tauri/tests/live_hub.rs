//! Against a running hub (scripts/local-hub.sh) with the home agent on.
//! Ignored by default; run with
//!
//!   ROOK_TEST_HUB=http://127.0.0.1:7005 ROOK_TEST_PASS=<dashboard password> \
//!   ROOK_TEST_MCP_TOKEN=<ROOK_MCP_STATIC_TOKEN> cargo test --test live_hub -- --ignored
//!
//! The home agent needs a model endpoint; any OpenAI-compatible one works.

use rook_desktop_lib::hub::{self, Hub, HubError};
use std::time::Duration;

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("set {name}"))
}

#[tokio::test]
#[ignore]
async fn live_hub_round_trip() {
    let origin = env("ROOK_TEST_HUB");
    let pass = env("ROOK_TEST_PASS");

    assert_eq!(
        hub::password_sign_in(&origin, "nobody", "wrong-password").await,
        Err(HubError::BadLogin)
    );

    // The operator's account login (the bootstrap account is "operator" when
    // no ROOK_WEB_USER is set), then the shared dashboard password.
    let account = hub::password_sign_in(&origin, "operator", &pass).await.unwrap();
    assert!(account.starts_with("rook_account="), "{account}");
    let who = Hub::new(&origin, &account).whoami().await.unwrap();
    assert!(who.principal.is_some());

    let cookie = hub::password_sign_in(&origin, "anyone", &pass).await.unwrap();
    let h = Hub::new(&origin, &cookie);
    let who = h.whoami().await.unwrap();
    assert!(who.principal.is_some());

    // A stale cookie reads as "sign in again".
    assert_eq!(
        Hub::new(&origin, "rook_session=stale").whoami().await,
        Err(HubError::NotSignedIn)
    );

    let status = h.home_status().await.unwrap();
    assert!(status.enabled, "turn the home agent on for this test");

    let a = h
        .ask_via_chat("What's on the grocery list?", None, Duration::from_secs(60))
        .await
        .unwrap();
    assert_eq!(a.via, "chat");
    assert!(!a.text.is_empty());
    let room = a.room.clone().unwrap();
    // The second question reuses the room.
    let b = h
        .ask_via_chat("Is the printer back?", Some(&room), Duration::from_secs(60))
        .await
        .unwrap();
    assert_eq!(b.room.as_deref(), Some(room.as_str()));
    assert_ne!(a.text, b.text);

    let rooms = h.chat_rooms().await.unwrap();
    assert_eq!(hub::find_quick_room(&rooms, &a.agent).as_deref(), Some(room.as_str()));

    // No /api/attention on today's hubs: a clean "not supported".
    assert_eq!(h.attention().await.unwrap(), None);

    if let Ok(token) = std::env::var("ROOK_TEST_MCP_TOKEN") {
        let m = h
            .ask_via_mcp(&token, "Anything on the grocery list?", Duration::from_secs(60))
            .await
            .unwrap();
        assert_eq!(m.via, "agent");
        assert!(!m.text.is_empty());
        let bad = h.ask_via_mcp("not-a-token", "hi", Duration::from_secs(20)).await;
        assert!(matches!(bad, Err(HubError::Agent(_))), "{bad:?}");
    }
}
