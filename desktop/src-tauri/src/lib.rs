//! Rook desktop: a tray app for a Rook hub.
//!
//! * the hub's dashboard in a window (the hub's own pages; no app access);
//! * a global hotkey (default Ctrl+Alt+Space) for a quick-ask box that talks
//!   to the home agent;
//! * desktop notifications for new chat messages and, on hubs that list
//!   them, huddle questions and tasks that need you;
//! * push-to-talk and an optional wake word (feature `wake-word`);
//! * start/stop for this computer's worker, and a hub installer wizard.

pub mod config;
pub mod hub;
pub mod installer;
pub mod notify;
pub mod voice;
pub mod worker;

use config::{Credentials, Settings, Store};
use serde::Serialize;
use serde_json::json;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};
use tauri_plugin_notification::NotificationExt;

/// How long a quick ask waits for the home agent (its own timeout is 90 s).
const ASK_WAIT: Duration = Duration::from_secs(150);

pub struct App {
    store: Store,
    settings: Mutex<Settings>,
    creds: Mutex<Credentials>,
    worker: Mutex<worker::Controller>,
    quick_room: Mutex<Option<String>>,
    shortcuts: Mutex<(Option<Shortcut>, Option<Shortcut>)>,
    /// Bumped on every sign-in attempt, so an older cookie watcher stops.
    signin_gen: AtomicU64,
    installing: AtomicBool,
    worker_item: Mutex<Option<MenuItem<tauri::Wry>>>,
    #[cfg(feature = "voice")]
    recording: Mutex<Option<voice::capture::Recording>>,
    #[cfg(feature = "wake-word")]
    listener: Mutex<Option<voice::wake::Listener>>,
}

fn state(app: &AppHandle) -> tauri::State<'_, App> {
    app.state::<App>()
}

fn settings(app: &AppHandle) -> Settings {
    state(app).settings.lock().unwrap().clone()
}

fn creds(app: &AppHandle) -> Credentials {
    state(app).creds.lock().unwrap().clone()
}

/// The hub client for the configured hub, if signed in to it.
fn hub_client(app: &AppHandle) -> Result<hub::Hub, String> {
    let s = settings(app);
    let c = creds(app);
    let origin = config::normalize_hub_url(&s.hub_url)?;
    if !c.signed_in_to(&origin) {
        return Err(hub::HubError::NotSignedIn.to_string());
    }
    Ok(hub::Hub::new(&origin, &c.cookie))
}

fn notify(app: &AppHandle, title: &str, body: &str) {
    if let Err(e) = app.notification().builder().title(title).body(body).show() {
        log::warn!("notification failed: {e}");
    }
}

// ---- windows ------------------------------------------------------------------

fn show_settings(app: &AppHandle, section: Option<&str>) {
    if let Some(w) = app.get_webview_window("settings") {
        let _ = w.show();
        let _ = w.set_focus();
        if let Some(s) = section {
            let _ = w.emit("goto", s);
        }
        return;
    }
    let page = match section {
        Some(s) => format!("index.html#{s}"),
        None => "index.html".into(),
    };
    if let Err(e) = WebviewWindowBuilder::new(app, "settings", WebviewUrl::App(page.into()))
        .title("Rook settings")
        .inner_size(720.0, 760.0)
        .min_inner_size(380.0, 480.0)
        .build()
    {
        log::error!("settings window: {e}");
    }
}

fn show_install(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("install") {
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }
    if let Err(e) = WebviewWindowBuilder::new(app, "install", WebviewUrl::App("install.html".into()))
        .title("Install a Rook hub")
        .inner_size(720.0, 640.0)
        .min_inner_size(380.0, 420.0)
        .build()
    {
        log::error!("install window: {e}");
    }
}

fn build_overlay(app: &AppHandle) -> tauri::Result<()> {
    let w = WebviewWindowBuilder::new(app, "overlay", WebviewUrl::App("overlay.html".into()))
        .title("Ask Rook")
        .inner_size(640.0, 300.0)
        .resizable(false)
        .decorations(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .visible(false)
        .center()
        .build()?;
    let handle = w.clone();
    w.on_window_event(move |e| {
        if let WindowEvent::CloseRequested { api, .. } = e {
            api.prevent_close();
            let _ = handle.hide();
        }
    });
    Ok(())
}

fn show_overlay(app: &AppHandle) {
    if app.get_webview_window("overlay").is_none() {
        if let Err(e) = build_overlay(app) {
            log::error!("overlay window: {e}");
            return;
        }
    }
    if let Some(w) = app.get_webview_window("overlay") {
        let _ = w.center();
        let _ = w.show();
        let _ = w.set_focus();
        let _ = w.emit("overlay-show", ());
    }
}

fn toggle_overlay(app: &AppHandle) {
    match app.get_webview_window("overlay") {
        Some(w) if w.is_visible().unwrap_or(false) => {
            let _ = w.hide();
        }
        _ => show_overlay(app),
    }
}

/// Phases of a quick ask, for the overlay: idle, recording, wake,
/// transcribing, thinking, answer, error.
fn overlay_state(app: &AppHandle, phase: &str, text: &str, question: &str) {
    let _ = app.emit_to("overlay", "overlay-state", json!({"phase": phase, "text": text, "question": question}));
}

fn session_cookie_for_webview(origin: &str, cookie: &str) -> Option<tauri::webview::Cookie<'static>> {
    let (name, value) = cookie.split_once('=')?;
    let url = url::Url::parse(origin).ok()?;
    Some(
        tauri::webview::Cookie::build((name.to_string(), value.to_string()))
            .domain(url.host_str()?.to_string())
            .path("/")
            .http_only(true)
            .secure(url.scheme() == "https")
            .build(),
    )
}

fn open_dashboard(app: &AppHandle, path: &str) -> Result<(), String> {
    let s = settings(app);
    let origin = config::normalize_hub_url(&s.hub_url)?;
    let target: url::Url = format!("{origin}{path}").parse().map_err(|_| "bad address".to_string())?;
    if let Some(w) = app.get_webview_window("dashboard") {
        let _ = w.navigate(target);
        let _ = w.show();
        let _ = w.set_focus();
        return Ok(());
    }
    let w = WebviewWindowBuilder::new(app, "dashboard", WebviewUrl::External(target.clone()))
        .title("Rook")
        .inner_size(1280.0, 860.0)
        .min_inner_size(380.0, 480.0)
        .build()
        .map_err(|e| e.to_string())?;
    // Signed in here already (password sign-in): hand the window the same
    // session so the dashboard opens signed in.
    let c = creds(app);
    if c.signed_in_to(&origin) {
        if let Some(cookie) = session_cookie_for_webview(&origin, &c.cookie) {
            if w.set_cookie(cookie).is_ok() {
                let _ = w.navigate(target);
            }
        }
    }
    Ok(())
}

// ---- state for the settings page -----------------------------------------------

#[derive(Serialize)]
struct Features {
    voice: bool,
    wake_word: bool,
}

#[derive(Serialize)]
struct UiState {
    settings: Settings,
    hub: String,
    signed_in: bool,
    principal: String,
    voice_key_set: bool,
    agent_token_set: bool,
    features: Features,
    microphone: Option<String>,
    version: &'static str,
    os: &'static str,
    worker: worker::Info,
}

fn ui_state(app: &AppHandle) -> UiState {
    let s = settings(app);
    let c = creds(app);
    let origin = config::normalize_hub_url(&s.hub_url).unwrap_or_default();
    let worker = state(app).worker.lock().unwrap().info();
    update_worker_item(app, &worker);
    UiState {
        signed_in: c.signed_in_to(&origin),
        principal: c.principal.clone(),
        voice_key_set: !c.voice_key.is_empty(),
        agent_token_set: !c.agent_token.is_empty(),
        hub: origin,
        settings: s,
        features: Features { voice: cfg!(feature = "voice"), wake_word: cfg!(feature = "wake-word") },
        microphone: microphone_name(),
        version: env!("CARGO_PKG_VERSION"),
        os: installer::current_os(),
        worker,
    }
}

#[cfg(feature = "voice")]
fn microphone_name() -> Option<String> {
    voice::capture::input_name()
}
#[cfg(not(feature = "voice"))]
fn microphone_name() -> Option<String> {
    None
}

fn changed(app: &AppHandle) {
    let _ = app.emit("state-changed", ());
}

// ---- shortcuts --------------------------------------------------------------------

fn parse_shortcut(s: &str) -> Result<Option<Shortcut>, String> {
    if s.trim().is_empty() {
        return Ok(None);
    }
    s.trim().parse::<Shortcut>().map(Some).map_err(|_| format!("\"{s}\" isn't a shortcut the app understands (try Ctrl+Alt+Space)."))
}

fn apply_shortcuts(app: &AppHandle) -> Result<(), String> {
    let s = settings(app);
    let ask = parse_shortcut(&s.ask_shortcut)?;
    let ptt = if cfg!(feature = "voice") { parse_shortcut(&s.ptt_shortcut)? } else { None };
    if ask.is_some() && ask == ptt {
        return Err("Quick ask and push-to-talk need different shortcuts.".into());
    }
    let gs = app.global_shortcut();
    let _ = gs.unregister_all();
    let mut problems = vec![];
    for sc in [ask, ptt].into_iter().flatten() {
        if let Err(e) = gs.register(sc) {
            problems.push(format!("{sc}: {e}"));
        }
    }
    *state(app).shortcuts.lock().unwrap() = (ask, ptt);
    if problems.is_empty() {
        Ok(())
    } else {
        Err(format!("Couldn't claim the shortcut ({}). Another app may be using it.", problems.join("; ")))
    }
}

fn on_shortcut(app: &AppHandle, sc: &Shortcut, pressed: bool) {
    let (ask, ptt) = *state(app).shortcuts.lock().unwrap();
    if Some(*sc) == ask && pressed {
        toggle_overlay(app);
    } else if Some(*sc) == ptt {
        if pressed {
            ptt_begin(app);
        } else {
            ptt_end(app);
        }
    }
}

// ---- asking -------------------------------------------------------------------------

async fn ask_impl(app: &AppHandle, question: &str) -> Result<hub::Answer, String> {
    let q = question.trim();
    if q.is_empty() {
        return Err("Type a question first.".into());
    }
    let h = hub_client(app)?;
    let token = creds(app).agent_token;
    let answer = if !token.is_empty() {
        h.ask_via_mcp(&token, q, ASK_WAIT).await
    } else {
        let hint = state(app).quick_room.lock().unwrap().clone();
        h.ask_via_chat(q, hint.as_deref(), ASK_WAIT).await
    }
    .map_err(|e| e.to_string())?;
    if let Some(room) = &answer.room {
        *state(app).quick_room.lock().unwrap() = Some(room.clone());
    }
    // Answered while the box was closed: say so.
    let visible = app.get_webview_window("overlay").and_then(|w| w.is_visible().ok()).unwrap_or(false);
    if !visible {
        notify(app, "The home agent answered", &answer.text.chars().take(200).collect::<String>());
    }
    Ok(answer)
}

/// After speech: transcribe, then ask, reporting each step to the overlay.
#[cfg_attr(not(feature = "voice"), allow(dead_code))]
async fn voice_question(app: AppHandle, pcm: Vec<i16>) {
    if pcm.len() < (voice::RATE as usize) / 4 {
        overlay_state(&app, "error", "That was too short. Hold the shortcut while you speak.", "");
        return;
    }
    overlay_state(&app, "transcribing", "", "");
    let s = settings(&app);
    let key = creds(&app).voice_key;
    let text = match voice::transcribe(&s.voice_url, &key, &pcm).await {
        Ok(t) if t.is_empty() => {
            overlay_state(&app, "error", "I didn't catch that.", "");
            return;
        }
        Ok(t) => t,
        Err(e) => {
            overlay_state(&app, "error", &e, "");
            return;
        }
    };
    overlay_state(&app, "thinking", "", &text);
    match ask_impl(&app, &text).await {
        Ok(a) => overlay_state(&app, "answer", &a.text, &text),
        Err(e) => overlay_state(&app, "error", &e, &text),
    }
}

#[cfg(feature = "voice")]
fn ptt_begin(app: &AppHandle) {
    let st = state(app);
    let mut rec = st.recording.lock().unwrap();
    if rec.is_some() {
        return; // key auto-repeat
    }
    show_overlay(app);
    *rec = Some(voice::capture::Recording::start());
    overlay_state(app, "recording", "", "");
}

#[cfg(feature = "voice")]
fn ptt_end(app: &AppHandle) {
    let Some(rec) = state(app).recording.lock().unwrap().take() else { return };
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        match tauri::async_runtime::spawn_blocking(move || rec.finish()).await {
            Ok(Ok(pcm)) => voice_question(app, pcm).await,
            Ok(Err(e)) => overlay_state(&app, "error", &e, ""),
            Err(_) => overlay_state(&app, "error", "The recording failed.", ""),
        }
    });
}

#[cfg(not(feature = "voice"))]
fn ptt_begin(_: &AppHandle) {}
#[cfg(not(feature = "voice"))]
fn ptt_end(_: &AppHandle) {}

#[cfg(feature = "wake-word")]
fn wake_features_dir(app: &AppHandle) -> PathBuf {
    let bundled = app.path().resource_dir().ok().map(|d| d.join("wakeword"));
    let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../android/app/src/main/assets/wakeword");
    bundled.filter(|d| d.join("melspectrogram.onnx").is_file()).unwrap_or(dev)
}

/// Start or stop the wake word listener to match the settings. Returns a
/// problem to show (a missing model, no microphone).
#[cfg(feature = "wake-word")]
fn apply_wake(app: &AppHandle) -> Option<String> {
    let st = state(app);
    let s = settings(app);
    let mut slot = st.listener.lock().unwrap();
    if let Some(mut l) = slot.take() {
        l.stop();
    }
    if !s.wake_word {
        return None;
    }
    let models = voice::wake::Models { features_dir: wake_features_dir(app), classifier: PathBuf::from(&s.wake_model) };
    let handle = app.clone();
    match voice::wake::Listener::start(models, s.wake_threshold, move |ev| match ev {
        voice::wake::Event::Woke => {
            show_overlay(&handle);
            overlay_state(&handle, "wake", "", "");
        }
        voice::wake::Event::Utterance(pcm) if pcm.is_empty() => overlay_state(&handle, "error", "I didn't hear a question.", ""),
        voice::wake::Event::Utterance(pcm) => {
            tauri::async_runtime::spawn(voice_question(handle.clone(), pcm));
        }
        voice::wake::Event::Error(e) => {
            log::warn!("wake word stopped: {e}");
            notify(&handle, "Wake word stopped", &e);
        }
    }) {
        Ok(l) => {
            *slot = Some(l);
            None
        }
        Err(e) => Some(e),
    }
}

#[cfg(not(feature = "wake-word"))]
fn apply_wake(_: &AppHandle) -> Option<String> {
    None
}

// ---- background: notifications and the sign-in watcher -----------------------------

fn spawn_poller(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let mut watcher = notify::Watcher::new();
        let mut watched_hub = String::new();
        let mut attention_supported = true;
        let mut polls: u64 = 0;
        let mut warned = false;
        loop {
            let s = settings(&app);
            tokio::time::sleep(Duration::from_secs(s.poll_seconds)).await;
            polls += 1;
            if polls % 4 == 0 {
                let info = state(&app).worker.lock().unwrap().info();
                update_worker_item(&app, &info);
            }
            if !s.notifications {
                continue;
            }
            let Ok(h) = hub_client(&app) else { continue };
            if h.origin != watched_hub {
                watcher = notify::Watcher::new();
                watched_hub = h.origin.clone();
                attention_supported = true;
                warned = false;
            }
            let quick = state(&app).quick_room.lock().unwrap().clone();
            match h.chat_rooms().await {
                Ok(rooms) => {
                    warned = false;
                    for n in watcher.rooms(&rooms, quick.as_deref()) {
                        notify(&app, &n.title, &n.body);
                    }
                }
                Err(hub::HubError::NotSignedIn) | Err(hub::HubError::NotOperator) => {
                    if !warned {
                        notify(&app, "Rook", "Your hub sign-in expired. Sign in again in Settings.");
                        warned = true;
                    }
                    continue;
                }
                Err(e) => log::debug!("poll: {e}"),
            }
            // Hubs without /api/attention are re-checked now and then (an
            // upgrade may add it), not on every poll.
            if attention_supported || polls % 20 == 0 {
                match h.attention().await {
                    Ok(items) => {
                        attention_supported = items.is_some();
                        for n in watcher.attention(items.as_deref()) {
                            notify(&app, &n.title, &n.body);
                        }
                    }
                    Err(e) => log::debug!("attention: {e}"),
                }
            }
        }
    });
}

/// After the person signs in inside the dashboard window, take that
/// session for the app's own calls.
fn watch_dashboard_sign_in(app: AppHandle, origin: String) {
    let gen = state(&app).signin_gen.fetch_add(1, Ordering::SeqCst) + 1;
    tauri::async_runtime::spawn(async move {
        let url: url::Url = match origin.parse() {
            Ok(u) => u,
            Err(_) => return,
        };
        for _ in 0..450 {
            tokio::time::sleep(Duration::from_secs(2)).await;
            if state(&app).signin_gen.load(Ordering::SeqCst) != gen {
                return;
            }
            let Some(w) = app.get_webview_window("dashboard") else { return };
            let u = url.clone();
            let cookies = tauri::async_runtime::spawn_blocking(move || w.cookies_for_url(u)).await;
            let Ok(Ok(cookies)) = cookies else { continue };
            let pairs: Vec<(String, String)> = cookies.iter().map(|c| (c.name().to_string(), c.value().to_string())).collect();
            let Some(cookie) = hub::session_cookie(&pairs) else { continue };
            if let Ok(who) = hub::Hub::new(&origin, &cookie).whoami().await {
                save_sign_in(&app, &origin, &cookie, who.principal.unwrap_or_default());
                notify(&app, "Rook", "Signed in. The hotkey and notifications are ready.");
                return;
            }
        }
    });
}

fn save_sign_in(app: &AppHandle, origin: &str, cookie: &str, principal: String) {
    let st = state(app);
    let mut c = st.creds.lock().unwrap();
    if c.hub != origin {
        // A different hub: its voice key and agent token don't carry over.
        *c = Credentials::default();
    }
    c.hub = origin.to_string();
    c.cookie = cookie.to_string();
    c.principal = principal;
    if let Err(e) = st.store.save_credentials(&c) {
        log::error!("saving sign-in: {e}");
    }
    drop(c);
    *st.quick_room.lock().unwrap() = None;
    changed(app);
}

// ---- tray -----------------------------------------------------------------------------

fn worker_label(info: &worker::Info) -> &'static str {
    match info.status {
        "running" => "Stop this computer's worker",
        "stopped" => "Start this computer's worker",
        _ => "Add this computer as a worker\u{2026}",
    }
}

fn update_worker_item(app: &AppHandle, info: &worker::Info) {
    if let Some(item) = state(app).worker_item.lock().unwrap().as_ref() {
        let _ = item.set_text(worker_label(info));
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let s = settings(app);
    let ask_label = if s.ask_shortcut.is_empty() {
        "Ask the home agent\u{2026}".to_string()
    } else {
        format!("Ask the home agent\u{2026}  ({})", s.ask_shortcut)
    };
    let info = state(app).worker.lock().unwrap().info();
    let dashboard = MenuItem::with_id(app, "dashboard", "Open dashboard", true, None::<&str>)?;
    let ask = MenuItem::with_id(app, "ask", ask_label, true, None::<&str>)?;
    let worker = MenuItem::with_id(app, "worker", worker_label(&info), true, None::<&str>)?;
    let install = MenuItem::with_id(app, "install", "Install a hub on this computer\u{2026}", true, None::<&str>)?;
    let settings_item = MenuItem::with_id(app, "settings", "Settings\u{2026}", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Rook", true, None::<&str>)?;
    let sep = || PredefinedMenuItem::separator(app);
    let menu = Menu::with_items(app, &[&dashboard, &ask, &sep()?, &worker, &install, &sep()?, &settings_item, &quit])?;
    *state(app).worker_item.lock().unwrap() = Some(worker);
    let mut tray = TrayIconBuilder::with_id("main").tooltip("Rook").menu(&menu).on_menu_event(|app, event| {
        match event.id.as_ref() {
            "dashboard" => {
                if open_dashboard(app, "/").is_err() {
                    show_settings(app, Some("hub"));
                }
            }
            "ask" => show_overlay(app),
            "worker" => {
                let st = state(app);
                let mut w = st.worker.lock().unwrap();
                let info = w.info();
                let res = match info.status {
                    "running" => w.stop(),
                    "stopped" => w.start(),
                    _ => {
                        drop(w);
                        if open_dashboard(app, "/#install").is_err() {
                            show_settings(app, Some("worker"));
                        }
                        return;
                    }
                };
                drop(w);
                match res {
                    Ok(i) => update_worker_item(app, &i),
                    Err(e) => notify(app, "Worker", &e),
                }
                changed(app);
            }
            "install" => show_install(app),
            "settings" => show_settings(app, None),
            "quit" => app.exit(0),
            _ => {}
        }
    });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

// ---- commands -------------------------------------------------------------------------

#[tauri::command]
fn get_state(app: AppHandle) -> UiState {
    ui_state(&app)
}

#[tauri::command]
fn save_settings(app: AppHandle, mut settings: Settings) -> Result<UiState, String> {
    settings = settings.sanitized();
    if !settings.hub_url.is_empty() {
        settings.hub_url = config::normalize_hub_url(&settings.hub_url)?;
    }
    if !settings.voice_url.is_empty() {
        voice::voice_base(&settings.voice_url)?;
    }
    parse_shortcut(&settings.ask_shortcut)?;
    parse_shortcut(&settings.ptt_shortcut)?;
    let st = state(&app);
    let old = std::mem::replace(&mut *st.settings.lock().unwrap(), settings.clone());
    if let Err(e) = apply_shortcuts(&app) {
        *st.settings.lock().unwrap() = old;
        let _ = apply_shortcuts(&app);
        return Err(e);
    }
    st.store.save_settings(&settings).map_err(|e| format!("Couldn't save settings: {e}"))?;
    let problem = apply_wake(&app);
    changed(&app);
    match problem {
        Some(p) => Err(format!("Saved, but the wake word is off: {p}")),
        None => Ok(ui_state(&app)),
    }
}

#[tauri::command]
fn save_secrets(app: AppHandle, voice_key: Option<String>, agent_token: Option<String>) -> Result<UiState, String> {
    let st = state(&app);
    {
        let mut c = st.creds.lock().unwrap();
        if let Some(k) = voice_key {
            c.voice_key = k.trim().to_string();
        }
        if let Some(t) = agent_token {
            c.agent_token = t.trim().to_string();
        }
        st.store.save_credentials(&c).map_err(|e| format!("Couldn't save: {e}"))?;
    }
    Ok(ui_state(&app))
}

fn set_hub(app: &AppHandle, hub: &str) -> Result<String, String> {
    let origin = config::normalize_hub_url(hub)?;
    let st = state(app);
    let mut s = st.settings.lock().unwrap();
    if s.hub_url != origin {
        s.hub_url = origin.clone();
        st.store.save_settings(&s).map_err(|e| format!("Couldn't save settings: {e}"))?;
    }
    Ok(origin)
}

#[tauri::command]
async fn sign_in_password(app: AppHandle, hub: String, username: String, password: String) -> Result<UiState, String> {
    let origin = set_hub(&app, &hub)?;
    let cookie = hub::password_sign_in(&origin, &username, &password).await.map_err(|e| e.to_string())?;
    let who = hub::Hub::new(&origin, &cookie).whoami().await.map_err(|e| e.to_string())?;
    save_sign_in(&app, &origin, &cookie, who.principal.unwrap_or_default());
    Ok(ui_state(&app))
}

#[tauri::command]
fn sign_in_browser(app: AppHandle, hub: String) -> Result<(), String> {
    let origin = set_hub(&app, &hub)?;
    open_dashboard(&app, "/")?;
    watch_dashboard_sign_in(app, origin);
    Ok(())
}

#[tauri::command]
fn sign_out(app: AppHandle) -> Result<UiState, String> {
    let st = state(&app);
    st.signin_gen.fetch_add(1, Ordering::SeqCst);
    {
        let mut c = st.creds.lock().unwrap();
        c.cookie.clear();
        c.principal.clear();
        st.store.save_credentials(&c).map_err(|e| e.to_string())?;
    }
    if let Some(w) = app.get_webview_window("dashboard") {
        let _ = w.clear_all_browsing_data();
        let _ = w.close();
    }
    changed(&app);
    Ok(ui_state(&app))
}

#[tauri::command]
fn open_dashboard_cmd(app: AppHandle, path: Option<String>) -> Result<(), String> {
    let p = path.unwrap_or_else(|| "/".into());
    if !p.starts_with('/') {
        return Err("bad path".into());
    }
    open_dashboard(&app, &p)
}

#[tauri::command]
async fn ask(app: AppHandle, question: String) -> Result<hub::Answer, String> {
    ask_impl(&app, &question).await
}

#[tauri::command]
fn overlay_hide(app: AppHandle) {
    if let Some(w) = app.get_webview_window("overlay") {
        let _ = w.hide();
    }
}

#[tauri::command]
fn open_settings(app: AppHandle, section: Option<String>) {
    show_settings(&app, section.as_deref());
}

#[tauri::command]
fn show_window(app: AppHandle, name: String) {
    match name.as_str() {
        "overlay" => show_overlay(&app),
        "install" => show_install(&app),
        _ => show_settings(&app, None),
    }
}

/// The overlay's microphone button: press once to talk, again to send.
#[tauri::command]
fn ptt_toggle(app: AppHandle) -> Result<bool, String> {
    #[cfg(feature = "voice")]
    {
        let recording = state(&app).recording.lock().unwrap().is_some();
        if recording {
            ptt_end(&app);
        } else {
            ptt_begin(&app);
        }
        Ok(!recording)
    }
    #[cfg(not(feature = "voice"))]
    {
        let _ = app;
        Err("This build has no voice support.".into())
    }
}

#[tauri::command]
fn worker_action(app: AppHandle, action: String) -> Result<UiState, String> {
    {
        let st = state(&app);
        let mut w = st.worker.lock().unwrap();
        match action.as_str() {
            "start" => w.start().map(|_| ())?,
            "stop" => w.stop().map(|_| ())?,
            _ => {}
        }
    }
    changed(&app);
    Ok(ui_state(&app))
}

#[tauri::command]
fn test_notification(app: AppHandle) {
    notify(&app, "Rook", "Notifications are working.");
}

#[tauri::command]
fn install_plan() -> installer::Plan {
    installer::plan(installer::current_os())
}

/// Run the hub installer, streaming its output as `install-log` events.
#[tauri::command]
async fn install_run(app: AppHandle, local_only: bool) -> Result<installer::Summary, String> {
    if !installer::plan(installer::current_os()).supported {
        return Err("The hub installer runs on Linux only.".into());
    }
    let st = state(&app);
    if st.installing.swap(true, Ordering::SeqCst) {
        return Err("The installer is already running.".into());
    }
    let res = install_inner(&app, local_only).await;
    state(&app).installing.store(false, Ordering::SeqCst);
    res
}

async fn install_inner(app: &AppHandle, local_only: bool) -> Result<installer::Summary, String> {
    use tokio::io::{AsyncBufReadExt, BufReader};
    let script = installer::script_path(app.path().resource_dir().ok().as_deref()).ok_or("The installer script is missing from this app.")?;
    let args = installer::args(local_only, installer::has_systemd_user());
    let mut child = tokio::process::Command::new("bash")
        .arg(&script)
        .args(&args)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("Couldn't start the installer: {e}"))?;
    let out = child.stdout.take().unwrap();
    let err = child.stderr.take().unwrap();
    let lines = std::sync::Arc::new(Mutex::new(Vec::<String>::new()));
    let pump = |r: Box<dyn tokio::io::AsyncRead + Unpin + Send>, app: AppHandle, lines: std::sync::Arc<Mutex<Vec<String>>>| async move {
        let mut br = BufReader::new(r).lines();
        while let Ok(Some(line)) = br.next_line().await {
            let _ = app.emit_to("install", "install-log", &line);
            lines.lock().unwrap().push(line);
        }
    };
    let a = tauri::async_runtime::spawn(pump(Box::new(out), app.clone(), lines.clone()));
    let b = tauri::async_runtime::spawn(pump(Box::new(err), app.clone(), lines.clone()));
    let status = child.wait().await.map_err(|e| e.to_string())?;
    let _ = a.await;
    let _ = b.await;
    let all = lines.lock().unwrap().clone();
    if !status.success() {
        return Err("The installer stopped with an error (see the log above). You can also run it in a terminal.".into());
    }
    installer::parse_summary(all.iter().map(String::as_str)).ok_or_else(|| "The installer finished but didn't print the hub's address.".into())
}

/// Connect to the hub the installer just made, with the password it printed.
#[tauri::command]
async fn install_connect(app: AppHandle, dashboard: String, password: String) -> Result<UiState, String> {
    sign_in_password(app, dashboard, "admin".into(), password).await
}

// ---- entry -----------------------------------------------------------------------------

fn config_dir(app: &AppHandle) -> PathBuf {
    app.path().app_config_dir().unwrap_or_else(|_| PathBuf::from(".rook-desktop"))
}

pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let show: Vec<String> = std::env::args().skip_while(|a| a != "--show").skip(1).take(1).collect();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, sc, event| on_shortcut(app, sc, event.state() == ShortcutState::Pressed))
                .build(),
        )
        .setup(move |app| {
            let handle = app.handle().clone();
            let store = Store::new(config_dir(&handle));
            let settings = store.load_settings();
            let creds = store.load_credentials();
            let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
            let first_run = settings.hub_url.is_empty();
            let autostart = settings.worker_autostart;
            app.manage(App {
                store,
                settings: Mutex::new(settings),
                creds: Mutex::new(creds),
                worker: Mutex::new(worker::Controller::new(worker::Layout::from_env(&home))),
                quick_room: Mutex::new(None),
                shortcuts: Mutex::new((None, None)),
                signin_gen: AtomicU64::new(0),
                installing: AtomicBool::new(false),
                worker_item: Mutex::new(None),
                #[cfg(feature = "voice")]
                recording: Mutex::new(None),
                #[cfg(feature = "wake-word")]
                listener: Mutex::new(None),
            });
            if let Err(e) = build_tray(&handle) {
                // No tray host (some Linux desktops): the settings window is the way in.
                log::warn!("no tray icon: {e}");
            }
            if let Err(e) = build_overlay(&handle) {
                log::error!("overlay: {e}");
            }
            if let Err(e) = apply_shortcuts(&handle) {
                log::warn!("{e}");
                notify(&handle, "Rook", &e);
            }
            if let Some(p) = apply_wake(&handle) {
                log::warn!("wake word: {p}");
            }
            if autostart {
                let st = state(&handle);
                let mut w = st.worker.lock().unwrap();
                if w.info().status == "stopped" {
                    if let Err(e) = w.start() {
                        log::warn!("worker autostart: {e}");
                    }
                }
            }
            spawn_poller(handle.clone());
            match show.first().map(String::as_str) {
                Some("overlay") => show_overlay(&handle),
                Some("install") => show_install(&handle),
                Some("dashboard") => {
                    let _ = open_dashboard(&handle, "/");
                }
                Some("settings") => show_settings(&handle, None),
                _ if first_run => show_settings(&handle, None),
                _ => {}
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_state,
            save_settings,
            save_secrets,
            sign_in_password,
            sign_in_browser,
            sign_out,
            open_dashboard_cmd,
            ask,
            overlay_hide,
            open_settings,
            show_window,
            ptt_toggle,
            worker_action,
            test_notification,
            install_plan,
            install_run,
            install_connect,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Rook");
    app.run(|app, event| match event {
        // A tray app keeps running with no window open; Quit exits.
        RunEvent::ExitRequested { api, code: None, .. } => api.prevent_exit(),
        RunEvent::Exit => {
            state(app).worker.lock().unwrap().shutdown();
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_shortcuts_parse_and_differ() {
        let ask = parse_shortcut(config::DEFAULT_ASK_SHORTCUT).unwrap().unwrap();
        let ptt = parse_shortcut(config::DEFAULT_PTT_SHORTCUT).unwrap().unwrap();
        assert_ne!(ask, ptt);
        assert_eq!(parse_shortcut("  ").unwrap(), None);
        assert!(parse_shortcut("Ctrl+Nope").unwrap_err().contains("isn't a shortcut"));
        // What the settings page's recorder produces.
        for s in ["Ctrl+Shift+K", "Super+Alt+1", "F9", "Ctrl+Alt+Space"] {
            assert!(parse_shortcut(s).unwrap().is_some(), "{s}");
        }
    }

    #[test]
    fn webview_cookie_matches_the_hub() {
        let c = session_cookie_for_webview("https://rook.example.com", "rook_account=abc").unwrap();
        assert_eq!((c.name(), c.value()), ("rook_account", "abc"));
        assert_eq!(c.domain(), Some("rook.example.com"));
        assert_eq!(c.secure(), Some(true));
        let lan = session_cookie_for_webview("http://192.0.2.10:7005", "rook_session=x").unwrap();
        assert_eq!(lan.secure(), Some(false));
        assert!(session_cookie_for_webview("https://a.example", "garbage").is_none());
    }
}
