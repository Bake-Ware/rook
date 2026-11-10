//! Rook desktop: a tray app for a Rook hub.
//!
//! * the hub's dashboard in a window (the hub's own pages; no app access);
//! * a global hotkey (default Ctrl+Alt+Space) for a quick-ask box that talks
//!   to the home agent, and "ask about this page", which opens the
//!   dashboard's own page-aware @home chat;
//! * the tray's Dashboard menu: the hub's five sections and their pages;
//! * the Rook panel: what waits on you, work in progress, knowledge search
//!   and agent sessions;
//! * desktop notifications for new chat messages and for what waits on you;
//! * push-to-talk, an optional wake word (feature `wake-word`), and answers
//!   read aloud by the voice server;
//! * add, start and stop this computer's worker;
//! * one copy at a time, starting at sign-in if asked, and an update check.
//!
//! The hub client is `hub`; the worker (the `worker/` crate, compiled in)
//! runs under `worker`.

pub mod config;
pub mod hub;
pub mod notify;
pub mod sections;
pub mod update;
pub mod voice;
pub mod worker;

use config::{Credentials, Settings, Store};
use serde::Serialize;
use serde_json::json;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::ManagerExt as _;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};
use tauri_plugin_notification::NotificationExt;

/// How long a quick ask waits for the home agent (its own timeout is 90 s).
const ASK_WAIT: Duration = Duration::from_secs(150);
/// The bundle identifier (tauri.conf.json): the config folder's name.
const IDENTIFIER: &str = "systems.bake.rook.desktop";
/// Started at sign-in: stay in the tray.
const BACKGROUND_ARG: &str = "--background";
/// How often to ask the hub for a newer app.
const UPDATE_EVERY: Duration = Duration::from_secs(6 * 3600);
/// Opens the hub site's dock (its button, rook/plugins/agents/shell/dock.js):
/// the house agent's chat, which knows the page you are on. Clicks the button
/// when the dock is closed, then focuses its text box. Waits up to 10 s for
/// the dock to mount; does nothing on a hub without one (the agent off).
const OPEN_DOCK_JS: &str = "(function t(n){var b=document.getElementById('dock-btn');\
if(b&&!b.hidden){if(!document.body.classList.contains('dock-open'))b.click();\
setTimeout(function(){var i=document.getElementById('dock-text');if(i)i.focus();},60);}\
else if(n>0)setTimeout(function(){t(n-1)},250);})(40);";

pub struct App {
    store: Store,
    settings: Mutex<Settings>,
    creds: Mutex<Credentials>,
    /// This computer's worker, run by the app.
    worker: Mutex<worker::Controller>,
    /// The hub's pages by section (`page.list`), for the tray and panel.
    pages: Mutex<Vec<sections::Section>>,
    shortcuts: Mutex<(Option<Shortcut>, Option<Shortcut>)>,
    /// Bumped on every sign-in attempt, so an older cookie watcher stops.
    signin_gen: AtomicU64,
    worker_item: Mutex<Option<MenuItem<tauri::Wry>>>,
    /// A newer app the hub offers, if any.
    update: Mutex<Option<update::Update>>,
    /// Set to stop the answer being read aloud.
    speaking: Mutex<Option<Arc<AtomicBool>>>,
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

/// The hub's door for the configured hub, with the person's token, if
/// signed in to it.
fn hub_client(app: &AppHandle) -> Result<hub::Hub, String> {
    let s = settings(app);
    let c = creds(app);
    let origin = config::normalize_hub_url(&s.hub_url)?;
    if !c.signed_in_to(&origin) {
        return Err(hub::HubError::NotSignedIn.to_string());
    }
    Ok(hub::Hub::new(&origin, &c.token))
}

/// The hub's own pages, by section.
fn current_sections(app: &AppHandle) -> Vec<sections::Section> {
    state(app).pages.lock().unwrap().clone()
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

/// The Rook panel: what waits on you, work, knowledge and sessions.
fn show_panel(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("panel") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
        let _ = w.emit("panel-refresh", ());
        return;
    }
    if let Err(e) = WebviewWindowBuilder::new(app, "panel", WebviewUrl::App("panel.html".into()))
        .title("Rook")
        .inner_size(600.0, 760.0)
        .min_inner_size(360.0, 420.0)
        .build()
    {
        log::error!("panel window: {e}");
    }
}

/// "Ask about this page": the dashboard window with its @home dock open, so
/// the house agent sees the page (the dashboard's page-aware chat). Opens
/// the dashboard first when it isn't open.
fn ask_on_page(app: &AppHandle) -> Result<(), String> {
    let fresh = app.get_webview_window("dashboard").is_none();
    if fresh {
        open_dashboard(app, "/")?;
    }
    let Some(w) = app.get_webview_window("dashboard") else {
        return Err("The dashboard window didn't open.".into());
    };
    let _ = w.unminimize();
    let _ = w.show();
    let _ = w.set_focus();
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        if fresh {
            // Let the first page start loading, or the script lands on about:blank.
            tokio::time::sleep(Duration::from_millis(1500)).await;
        }
        if let Some(w) = app.get_webview_window("dashboard") {
            if let Err(e) = w.eval(OPEN_DOCK_JS) {
                log::warn!("couldn't open the dashboard chat: {e}");
            }
        }
    });
    Ok(())
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
    let _ = app.emit_to(
        "overlay",
        "overlay-state",
        json!({"phase": phase, "text": text, "question": question}),
    );
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
    let target: url::Url = format!("{origin}{path}")
        .parse()
        .map_err(|_| "bad address".to_string())?;
    if let Some(w) = app.get_webview_window("dashboard") {
        let _ = w.navigate(target);
        let _ = w.show();
        let _ = w.set_focus();
        return Ok(());
    }
    // The window stays on the hub: anything else (a link out, Google's
    // sign-in page, a popup) opens in the system browser instead.
    let hub_origin = target.origin();
    let nav_app = app.clone();
    let popup_app = app.clone();
    let w = WebviewWindowBuilder::new(app, "dashboard", WebviewUrl::External(target.clone()))
        .title("Rook")
        .inner_size(1280.0, 860.0)
        .min_inner_size(380.0, 480.0)
        .on_navigation(move |url| {
            if stays_in_window(url, &hub_origin) {
                return true;
            }
            open_outside(&nav_app, url);
            false
        })
        .on_new_window(move |url, _features| {
            open_outside(&popup_app, &url);
            tauri::webview::NewWindowResponse::Deny
        })
        .build()
        .map_err(|e| e.to_string())?;
    // Signed in here already (password or token sign-in): hand the window
    // the same session so the dashboard opens signed in.
    let c = creds(app);
    if c.signed_in_to(&origin) && !c.cookie.is_empty() {
        if let Some(cookie) = session_cookie_for_webview(&origin, &c.cookie) {
            if w.set_cookie(cookie).is_ok() {
                let _ = w.navigate(target);
            }
        }
    }
    Ok(())
}

/// Whether a navigation in the dashboard window may happen there: the hub's
/// own origin only (plus about:blank, which WebKit uses internally).
fn stays_in_window(url: &url::Url, hub: &url::Origin) -> bool {
    url.origin() == *hub || url.as_str() == "about:blank"
}

/// Hand a link the dashboard window won't load to the system browser.
fn open_outside(app: &AppHandle, url: &url::Url) {
    use tauri_plugin_opener::OpenerExt;
    if matches!(url.scheme(), "http" | "https" | "mailto") {
        match app.opener().open_url(url.as_str(), None::<&str>) {
            Ok(()) => log::info!("opened a {} link in the browser", url.scheme()),
            Err(e) => log::warn!("couldn't open a link in the browser: {e}"),
        }
    } else {
        log::info!("blocked a {} link from the dashboard window", url.scheme());
    }
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
    features: Features,
    microphone: Option<String>,
    version: &'static str,
    os: &'static str,
    worker: worker::Info,
    /// The hub's sections and pages.
    sections: Vec<sections::Section>,
    /// A newer app the hub offers.
    update: Option<update::Update>,
    /// The dashboard window is open (for "Ask about this page").
    dashboard_open: bool,
    /// An answer is being read aloud.
    speaking: bool,
}

/// What the settings page shows. The worker status and the microphone name
/// come from system tools (systemctl, schtasks, pgrep, the audio host), so
/// they are read on a blocking thread, never the main or async threads.
async fn ui_state(app: &AppHandle) -> UiState {
    let s = settings(app);
    let c = creds(app);
    let origin = config::normalize_hub_url(&s.hub_url).unwrap_or_default();
    let (worker, microphone) = system_facts(app).await;
    update_worker_item(app, &worker);
    UiState {
        signed_in: c.signed_in_to(&origin),
        sections: current_sections(app),
        principal: c.principal.clone(),
        voice_key_set: !c.voice_key.is_empty(),
        hub: origin,
        settings: s,
        features: Features {
            voice: cfg!(feature = "voice"),
            wake_word: cfg!(feature = "wake-word"),
        },
        microphone,
        version: env!("CARGO_PKG_VERSION"),
        os: current_os(),
        worker,
        update: state(app).update.lock().unwrap().clone(),
        dashboard_open: app.get_webview_window("dashboard").is_some(),
        speaking: state(app).speaking.lock().unwrap().is_some(),
    }
}

async fn system_facts(app: &AppHandle) -> (worker::Info, Option<String>) {
    let a = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let info = state(&a).worker.lock().unwrap().info();
        (info, microphone_name())
    })
    .await
    .unwrap_or_else(|_| (worker::Info::unknown(), None))
}

/// Start or stop the worker on a blocking thread.
async fn worker_do(app: &AppHandle, action: &str) -> Result<worker::Info, String> {
    let a = app.clone();
    let action = action.to_string();
    tauri::async_runtime::spawn_blocking(move || {
        let st = state(&a);
        let mut w = st.worker.lock().unwrap();
        match action.as_str() {
            "start" => w.start(),
            "stop" => w.stop(),
            _ => Ok(w.info()),
        }
    })
    .await
    .map_err(|_| "The worker action failed.".to_string())?
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
    s.trim()
        .parse::<Shortcut>()
        .map(Some)
        .map_err(|_| format!("\"{s}\" isn't a shortcut the app understands (try Ctrl+Alt+Space)."))
}

fn apply_shortcuts(app: &AppHandle) -> Result<(), String> {
    let s = settings(app);
    let ask = parse_shortcut(&s.ask_shortcut)?;
    let ptt = if cfg!(feature = "voice") {
        parse_shortcut(&s.ptt_shortcut)?
    } else {
        None
    };
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
        Err(format!(
            "Couldn't claim the shortcut ({}). Another app may be using it.",
            problems.join("; ")
        ))
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

// ---- reading answers aloud -----------------------------------------------------------

/// Whether to read this answer aloud (Settings > Voice > Read answers aloud).
fn wants_speech(s: &Settings, spoken_question: bool) -> bool {
    cfg!(feature = "voice")
        && !s.voice_url.is_empty()
        && match s.speak_answers.as_str() {
            config::SPEAK_ALWAYS => true,
            config::SPEAK_VOICE => spoken_question,
            _ => false,
        }
}

/// Stop reading aloud (a new question, Esc in the quick-ask box).
fn stop_speech(app: &AppHandle) {
    if let Some(flag) = state(app).speaking.lock().unwrap().take() {
        flag.store(true, Ordering::Relaxed);
        let _ = app.emit("speaking", false);
    }
}

/// Read an answer aloud, piece by piece: each piece is fetched from the voice
/// server while nothing plays, so a busy server costs a pause, not the answer.
/// Failing to speak is a note in the box; the answer is on screen anyway.
#[cfg(feature = "voice")]
fn speak(app: &AppHandle, text: &str) {
    stop_speech(app);
    let pieces = voice::speak::chunks(text);
    if pieces.is_empty() {
        return;
    }
    let stop = Arc::new(AtomicBool::new(false));
    *state(app).speaking.lock().unwrap() = Some(stop.clone());
    let _ = app.emit("speaking", true);
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let s = settings(&app);
        let key = creds(&app).voice_key;
        for piece in pieces {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let (pcm, rate) = match voice::speak::synthesize(&s.voice_url, &key, &piece).await {
                Ok(audio) => audio,
                Err(e) => {
                    log::warn!("speaking: {e}");
                    let _ = app.emit_to("overlay", "speak-error", e);
                    break;
                }
            };
            let flag = stop.clone();
            let played = tauri::async_runtime::spawn_blocking(move || voice::playback::play(&pcm, rate, &flag)).await;
            if let Ok(Err(e)) = played {
                log::warn!("speaking: {e}");
                let _ = app.emit_to("overlay", "speak-error", e);
                break;
            }
        }
        // Clear the flag only if it is still ours (a newer answer replaces it).
        let st = state(&app);
        let mut slot = st.speaking.lock().unwrap();
        if slot.as_ref().is_some_and(|f| Arc::ptr_eq(f, &stop)) {
            *slot = None;
            let _ = app.emit("speaking", false);
        }
    });
}

#[cfg(not(feature = "voice"))]
fn speak(_: &AppHandle, _: &str) {}

fn maybe_speak(app: &AppHandle, text: &str, spoken_question: bool) {
    if wants_speech(&settings(app), spoken_question) {
        speak(app, text);
    }
}

// ---- asking -------------------------------------------------------------------------

async fn ask_impl(app: &AppHandle, question: &str) -> Result<hub::Answer, String> {
    let q = question.trim();
    if q.is_empty() {
        return Err("Type a question first.".into());
    }
    let answer = hub_client(app)?.ask(q, ASK_WAIT).await.map_err(|e| e.to_string())?;
    // Answered while the box was closed: say so.
    let visible = app
        .get_webview_window("overlay")
        .and_then(|w| w.is_visible().ok())
        .unwrap_or(false);
    if !visible {
        notify(
            app,
            "The home agent answered",
            &answer.text.chars().take(200).collect::<String>(),
        );
    }
    Ok(answer)
}

/// After speech: transcribe, then ask, reporting each step to the overlay.
#[cfg_attr(not(feature = "voice"), allow(dead_code))]
async fn voice_question(app: AppHandle, pcm: Vec<i16>) {
    if pcm.len() < (voice::RATE as usize) / 4 {
        overlay_state(
            &app,
            "error",
            "That was too short. Hold the shortcut while you speak.",
            "",
        );
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
        Ok(a) => {
            overlay_state(&app, "answer", &a.text, &text);
            maybe_speak(&app, &a.text, true);
        }
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
    stop_speech(app); // talking over the answer stops it
    show_overlay(app);
    *rec = Some(voice::capture::Recording::start());
    overlay_state(app, "recording", "", "");
    // A microphone that can't open fails at once: say so now, not on release.
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_millis(400)).await;
        let failed = {
            let st = state(&app);
            let mut slot = st.recording.lock().unwrap();
            match slot.as_ref() {
                Some(r) if r.ended() => slot.take(),
                _ => None,
            }
        };
        if let Some(r) = failed {
            if let Err(e) = r.finish() {
                overlay_state(&app, "error", &e, "");
            }
        }
    });
}

#[cfg(feature = "voice")]
fn ptt_end(app: &AppHandle) {
    let Some(rec) = state(app).recording.lock().unwrap().take() else {
        return;
    };
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
fn wake_models(app: &AppHandle, s: &Settings) -> voice::wake::Models {
    let bundled = app
        .path()
        .resource_dir()
        .map(|d| d.join("wakeword"))
        .unwrap_or_default();
    // `cargo run` has no bundle: use the repository's copy.
    #[cfg(debug_assertions)]
    let bundled = if bundled.join("melspectrogram.onnx").is_file() {
        bundled
    } else {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../android/app/src/main/assets/wakeword")
    };
    voice::wake::Models {
        features_dir: bundled,
        classifier: PathBuf::from(&s.wake_model),
    }
}

/// Before the wake word setting is saved on: load the model (guarded, on a
/// blocking thread) and run it once, so a bad file is refused up front.
#[cfg(feature = "wake-word")]
async fn check_wake_model(app: &AppHandle, s: &Settings) -> Result<(), String> {
    if !s.wake_word {
        return Ok(());
    }
    let models = wake_models(app, s);
    let threshold = s.wake_threshold;
    tauri::async_runtime::spawn_blocking(move || voice::wake::load_guarded(&models, threshold, true).map(|_| ()))
        .await
        .map_err(|_| "The wake word model couldn't be checked.".to_string())?
}

#[cfg(not(feature = "wake-word"))]
async fn check_wake_model(_: &AppHandle, _: &Settings) -> Result<(), String> {
    Ok(())
}

/// Start or stop the wake word listener to match the settings, on a thread
/// of its own. If the model can't be loaded (a file changed or removed since
/// it was checked), the wake word is switched off and saved that way, and
/// the person is told: a bad model must not fail every start.
#[cfg(feature = "wake-word")]
fn apply_wake(app: &AppHandle) {
    let old = state(app).listener.lock().unwrap().take();
    let app = app.clone();
    std::thread::spawn(move || {
        if let Some(mut l) = old {
            l.stop();
        }
        let s = settings(&app);
        if !s.wake_word {
            return;
        }
        let handle = app.clone();
        let started = voice::wake::Listener::start(wake_models(&app, &s), s.wake_threshold, move |ev| match ev {
            voice::wake::Event::Woke => {
                show_overlay(&handle);
                overlay_state(&handle, "wake", "", "");
            }
            voice::wake::Event::Utterance(pcm) if pcm.is_empty() => {
                overlay_state(&handle, "error", "I didn't hear a question.", "")
            }
            voice::wake::Event::Utterance(pcm) => {
                tauri::async_runtime::spawn(voice_question(handle.clone(), pcm));
            }
            voice::wake::Event::Error(e) => {
                log::warn!("wake word stopped: {e}");
                notify(&handle, "Wake word stopped", &e);
            }
        });
        match started {
            Ok(l) => *state(&app).listener.lock().unwrap() = Some(l),
            Err(e) => {
                log::warn!("wake word off: {e}");
                let st = state(&app);
                let mut s = st.settings.lock().unwrap();
                s.wake_word = false;
                if let Err(err) = st.store.save_settings(&s) {
                    log::error!("saving settings: {err}");
                }
                drop(s);
                notify(&app, "Wake word turned off", &e);
                changed(&app);
            }
        }
    });
}

#[cfg(not(feature = "wake-word"))]
fn apply_wake(_: &AppHandle) {}

// ---- background: notifications and the sign-in watcher -----------------------------

fn spawn_poller(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let mut watcher = notify::Watcher::new();
        let mut watched_hub = String::new();
        let mut polls: u64 = 0;
        let mut warned = false;
        let mut update_checked: Option<std::time::Instant> = None;
        loop {
            let s = settings(&app);
            tokio::time::sleep(Duration::from_secs(s.poll_seconds)).await;
            polls += 1;
            if update_checked.map_or(true, |t| t.elapsed() >= UPDATE_EVERY) {
                update_checked = Some(std::time::Instant::now());
                check_update(&app).await;
            }
            if polls % 4 == 0 {
                let (info, _) = system_facts(&app).await;
                update_worker_item(&app, &info);
            }
            let Ok(h) = hub_client(&app) else { continue };
            if h.origin != watched_hub {
                watcher = notify::Watcher::new();
                watched_hub = h.origin.clone();
                warned = false;
            }
            // Pages come and go (agents make them); the site session lasts 30 days.
            if polls % 20 == 0 {
                refresh_pages(&app).await;
                keep_web_session(&app).await;
            }
            if !s.notifications {
                continue;
            }
            match h.attention().await {
                Ok(items) => {
                    warned = false;
                    for n in watcher.attention(items.as_deref()) {
                        notify(&app, &n.title, &n.body);
                    }
                }
                Err(hub::HubError::NotSignedIn) => {
                    if !warned {
                        notify(&app, "Rook", "Your hub sign-in expired. Sign in again in Settings.");
                        warned = true;
                    }
                }
                Err(e) => log::debug!("holds: {e}"),
            }
        }
    });
}

/// The hub's pages, for the tray's Dashboard menu and the panel.
async fn refresh_pages(app: &AppHandle) {
    let Ok(h) = hub_client(app) else { return };
    match h.pages().await {
        Ok(pages) => {
            let st = state(app);
            let changed_now = *st.pages.lock().unwrap() != pages;
            if changed_now {
                *st.pages.lock().unwrap() = pages;
                rebuild_tray(app);
                changed(app);
            }
        }
        Err(e) => log::debug!("pages: {e}"),
    }
}

/// Keep the site session the dashboard window opens with: a new one from
/// the token when the hub has forgotten the old.
async fn keep_web_session(app: &AppHandle) {
    let c = creds(app);
    let Ok(origin) = config::normalize_hub_url(&settings(app).hub_url) else {
        return;
    };
    if !c.signed_in_to(&origin) {
        return;
    }
    if !c.cookie.is_empty() && hub::web_me(&origin, &c.cookie).await.is_ok() {
        return;
    }
    match hub::web_sign_in(&origin, &c.token).await {
        // Unless the person signed out (or in again) meanwhile.
        Ok((cookie, person)) if creds(app).token == c.token => save_sign_in(app, &origin, &c.token, &cookie, person),
        Ok(_) => {}
        Err(e) => log::info!("site session: {e}"),
    }
}

/// Ask the hub whether a newer app is published; announce each new version
/// once.
async fn check_update(app: &AppHandle) {
    let s = settings(app);
    // The hub serves /updates/desktop.json (rook.core2.hub.updates.desktop_feed).
    let Ok(origin) = config::normalize_hub_url(&s.hub_url) else {
        return;
    };
    let current = env!("CARGO_PKG_VERSION");
    let downloads = match hub::desktop_downloads(&origin, current, update::platform()).await {
        Ok(d) => d,
        Err(e) => {
            log::debug!("update check: {e}");
            return;
        }
    };
    let found = update::pick(
        &downloads,
        current,
        update::platform(),
        std::env::var_os("APPIMAGE").is_some(),
    );
    *state(app).update.lock().unwrap() = found.clone();
    let Some(u) = found else { return };
    if u.version != s.update_notified && s.notifications {
        notify(
            app,
            "Rook update",
            &format!(
                "Version {} of the desktop app is out. Settings has the download.",
                u.version
            ),
        );
        let st = state(app);
        let mut cur = st.settings.lock().unwrap();
        cur.update_notified = u.version.clone();
        if let Err(e) = st.store.save_settings(&cur) {
            log::error!("saving settings: {e}");
        }
    }
    changed(app);
}

/// A sign-in: the person's token, and the site session from it.
fn save_sign_in(app: &AppHandle, origin: &str, token: &str, cookie: &str, person: String) {
    let st = state(app);
    let mut c = st.creds.lock().unwrap();
    if c.hub != origin {
        *c = Credentials::default();
    }
    c.hub = origin.to_string();
    c.token = token.to_string();
    c.cookie = cookie.to_string();
    c.principal = person;
    if let Err(e) = st.store.save_credentials(&c) {
        log::error!("saving sign-in: {e}");
    }
    drop(c);
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

/// The tray's menu, for the hub's pages of the moment.
fn tray_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let s = settings(app);
    let ask_label = if s.ask_shortcut.is_empty() {
        "Ask the home agent\u{2026}".to_string()
    } else {
        format!("Ask the home agent\u{2026}  ({})", s.ask_shortcut)
    };
    let panel = MenuItem::with_id(app, "panel", "Open Rook", true, None::<&str>)?;
    // Dashboard > Feed / Work / Intelligence sources / Trust / Settings > page.
    let dashboard = Submenu::with_id(app, "dashboard-menu", "Dashboard", true)?;
    dashboard.append(&MenuItem::with_id(
        app,
        "dashboard",
        "Open dashboard",
        true,
        None::<&str>,
    )?)?;
    dashboard.append(&PredefinedMenuItem::separator(app)?)?;
    for section in current_sections(app) {
        let sub = Submenu::with_id(app, format!("section:{}", section.id), &section.title, true)?;
        for page in &section.pages {
            sub.append(&MenuItem::with_id(
                app,
                sections::menu_id(&page.hash),
                &page.title,
                true,
                None::<&str>,
            )?)?;
        }
        dashboard.append(&sub)?;
    }
    let ask = MenuItem::with_id(app, "ask", ask_label, true, None::<&str>)?;
    let page_ask = MenuItem::with_id(app, "page-ask", "Ask about this page", true, None::<&str>)?;
    // The real label (start, stop, add) comes from refresh_worker_item, which
    // asks the system on a blocking thread.
    let worker = MenuItem::with_id(app, "worker", "This computer's worker\u{2026}", true, None::<&str>)?;
    let settings_item = MenuItem::with_id(app, "settings", "Settings\u{2026}", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Rook", true, None::<&str>)?;
    let sep = || PredefinedMenuItem::separator(app);
    let (sep1, sep2) = (sep()?, sep()?);
    let menu = Menu::with_items(
        app,
        &[
            &panel,
            &dashboard,
            &ask,
            &page_ask,
            &sep1,
            &worker,
            &sep2,
            &settings_item,
            &quit,
        ],
    )?;
    *state(app).worker_item.lock().unwrap() = Some(worker);
    Ok(menu)
}

/// A new menu after the hub's pages change.
fn rebuild_tray(app: &AppHandle) {
    let Some(tray) = app.tray_by_id("main") else { return };
    match tray_menu(app) {
        Ok(menu) => {
            let _ = tray.set_menu(Some(menu));
            refresh_worker_item(app);
        }
        Err(e) => log::warn!("tray menu: {e}"),
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let menu = tray_menu(app)?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("Rook")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "panel" => show_panel(app),
            "dashboard" => {
                if open_dashboard(app, "/").is_err() {
                    show_settings(app, Some("hub"));
                }
            }
            id if id.starts_with("page:") => {
                let opened = sections::path_for(id, &current_sections(app)).map(|p| open_dashboard(app, &p));
                if !matches!(opened, Some(Ok(()))) {
                    show_settings(app, Some("hub"));
                }
            }
            "ask" => show_overlay(app),
            "page-ask" => {
                if ask_on_page(app).is_err() {
                    show_settings(app, Some("hub"));
                }
            }
            "worker" => {
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    let (info, _) = system_facts(&app).await;
                    let action = match info.status {
                        "running" => "stop",
                        "stopped" => "start",
                        _ => {
                            show_settings(&app, Some("worker"));
                            return;
                        }
                    };
                    match worker_do(&app, action).await {
                        Ok(i) => update_worker_item(&app, &i),
                        Err(e) => notify(&app, "Worker", &e),
                    }
                    changed(&app);
                });
            }
            "settings" => show_settings(app, None),
            "quit" => app.exit(0),
            _ => {}
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

fn refresh_worker_item(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let (info, _) = system_facts(&app).await;
        update_worker_item(&app, &info);
    });
}

// ---- commands -------------------------------------------------------------------------

#[tauri::command]
async fn get_state(app: AppHandle) -> UiState {
    ui_state(&app).await
}

#[tauri::command]
async fn save_settings(app: AppHandle, mut settings: Settings) -> Result<UiState, String> {
    settings = settings.sanitized();
    if !settings.hub_url.is_empty() {
        settings.hub_url = config::normalize_hub_url(&settings.hub_url)?;
    }
    if !settings.voice_url.is_empty() {
        voice::voice_base(&settings.voice_url)?;
    }
    parse_shortcut(&settings.ask_shortcut)?;
    parse_shortcut(&settings.ptt_shortcut)?;
    // A wake model that can't load is refused here, before it is saved.
    check_wake_model(&app, &settings)
        .await
        .map_err(|e| format!("The wake word can't use that model: {e}"))?;
    let st = state(&app);
    let old = std::mem::replace(&mut *st.settings.lock().unwrap(), settings.clone());
    if let Err(e) = apply_shortcuts(&app) {
        *st.settings.lock().unwrap() = old;
        let _ = apply_shortcuts(&app);
        return Err(e);
    }
    st.store
        .save_settings(&settings)
        .map_err(|e| format!("Couldn't save settings: {e}"))?;
    if old.hub_url != settings.hub_url && !st.pages.lock().unwrap().is_empty() {
        // Another hub: its pages come with its sign-in.
        st.pages.lock().unwrap().clear();
        rebuild_tray(&app);
    }
    apply_wake(&app);
    if let Err(e) = apply_start_at_login(&app) {
        changed(&app);
        return Err(e);
    }
    changed(&app);
    Ok(ui_state(&app).await)
}

/// Make the system's start-at-sign-in entry match the setting (the Run key
/// on Windows, a LaunchAgent on macOS, ~/.config/autostart on Linux).
fn apply_start_at_login(app: &AppHandle) -> Result<(), String> {
    let want = settings(app).start_at_login;
    let al = app.autolaunch();
    let have = al.is_enabled().unwrap_or(false);
    let r = match (want, have) {
        (true, false) => al.enable(),
        (false, true) => al.disable(),
        _ => Ok(()),
    };
    r.map_err(|e| format!("Couldn't change starting at sign-in: {e}"))
}

#[tauri::command]
async fn save_secrets(app: AppHandle, voice_key: Option<String>) -> Result<UiState, String> {
    let st = state(&app);
    {
        let mut c = st.creds.lock().unwrap();
        if let Some(k) = voice_key {
            c.voice_key = k.trim().to_string();
        }
        st.store
            .save_credentials(&c)
            .map_err(|e| format!("Couldn't save: {e}"))?;
    }
    Ok(ui_state(&app).await)
}

fn set_hub(app: &AppHandle, hub: &str) -> Result<String, String> {
    let origin = config::normalize_hub_url(hub)?;
    let st = state(app);
    let mut s = st.settings.lock().unwrap();
    if s.hub_url != origin {
        s.hub_url = origin.clone();
        st.store
            .save_settings(&s)
            .map_err(|e| format!("Couldn't save settings: {e}"))?;
    }
    Ok(origin)
}

/// Sign in with the person's own door token.
#[tauri::command]
async fn sign_in_token(app: AppHandle, hub: String, token: String) -> Result<UiState, String> {
    let token = token.trim().to_string();
    if token.is_empty() {
        return Err("Paste your sign-in token first.".into());
    }
    let origin = set_hub(&app, &hub)?;
    let (cookie, person) = hub::web_sign_in(&origin, &token).await.map_err(|e| e.to_string())?;
    save_sign_in(&app, &origin, &token, &cookie, person);
    refresh_pages(&app).await;
    Ok(ui_state(&app).await)
}

/// Add this computer as a worker with a one-time code the app gets as the
/// signed-in person (they clicked Add), then start it.
#[tauri::command]
async fn worker_add(app: AppHandle) -> Result<UiState, String> {
    let h = hub_client(&app)?;
    let code = h.join_code().await.map_err(|e| e.to_string())?;
    let layout = state(&app).worker.lock().unwrap().layout.clone();
    let said = worker::enroll(&layout, &h.origin, &code, None).await?;
    log::info!("worker enrolled: {said}");
    use_services(&app);
    worker_do(&app, "start").await?;
    notify(&app, "Rook", "This computer is a worker on your hub now.");
    changed(&app);
    Ok(ui_state(&app).await)
}

/// The config bundle's voice service fills the voice settings left empty.
fn use_services(app: &AppHandle) {
    let st = state(app);
    let Some(services) = st.worker.lock().unwrap().layout.services() else {
        return;
    };
    let voice = &services["voice"];
    let url = voice["url"].as_str().unwrap_or("").trim().to_string();
    let key = voice["key"].as_str().unwrap_or("").trim().to_string();
    let mut s = st.settings.lock().unwrap();
    if s.voice_url.is_empty() && voice::voice_base(&url).is_ok() {
        s.voice_url = url;
        if let Err(e) = st.store.save_settings(&s) {
            log::error!("saving settings: {e}");
        }
        drop(s);
        let mut c = st.creds.lock().unwrap();
        if c.voice_key.is_empty() && !key.is_empty() {
            c.voice_key = key;
            if let Err(e) = st.store.save_credentials(&c) {
                log::error!("saving the voice key: {e}");
            }
        }
    }
}

/// Sign out here and at the hub: the hub forgets the session (its /logout),
/// and the dashboard window forgets its cookies.
#[tauri::command]
async fn sign_out(app: AppHandle) -> Result<UiState, String> {
    let st = state(&app);
    st.signin_gen.fetch_add(1, Ordering::SeqCst);
    let old = {
        let mut c = st.creds.lock().unwrap();
        let old = (c.hub.clone(), std::mem::take(&mut c.cookie));
        c.token.clear();
        c.principal.clear();
        st.store.save_credentials(&c).map_err(|e| e.to_string())?;
        old
    };
    if !old.1.is_empty() {
        if let Err(e) = hub::web_sign_out(&old.0, &old.1).await {
            log::info!("hub logout: {e}");
        }
    }
    st.pages.lock().unwrap().clear();
    rebuild_tray(&app);
    if let Some(w) = app.get_webview_window("dashboard") {
        let _ = w.clear_all_browsing_data();
        let _ = w.close();
    }
    changed(&app);
    Ok(ui_state(&app).await)
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
    stop_speech(&app);
    let a = ask_impl(&app, &question).await?;
    maybe_speak(&app, &a.text, false);
    Ok(a)
}

#[tauri::command]
fn stop_speaking(app: AppHandle) {
    stop_speech(&app);
}

#[tauri::command]
fn ask_on_page_cmd(app: AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("overlay") {
        let _ = w.hide();
    }
    ask_on_page(&app)
}

fn to_json<T: Serialize>(r: Result<T, hub::HubError>) -> Result<serde_json::Value, String> {
    let v = r.map_err(|e| e.to_string())?;
    serde_json::to_value(v).map_err(|e| e.to_string())
}

/// The Rook panel's list: `waiting`, read-only, as the signed-in person.
/// The panel shows holds, the hub's pages and the house agent, and an agent
/// opens a page when one is needed (castling-attention: "No built-in views
/// needed for things like knowledge or tasks").
#[tauri::command]
async fn panel_data(app: AppHandle, kind: String) -> Result<serde_json::Value, String> {
    let h = hub_client(&app)?;
    match kind.as_str() {
        "waiting" => to_json(h.waiting().await),
        _ => Err("unknown list".into()),
    }
}

/// Open the newer app's download in the browser.
#[tauri::command]
fn open_update(app: AppHandle) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    let Some(u) = state(&app).update.lock().unwrap().clone() else {
        return Err("No update is waiting.".into());
    };
    app.opener()
        .open_url(&u.url, None::<&str>)
        .map_err(|e| format!("Couldn't open the download: {e}"))
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
        "panel" => show_panel(&app),
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
async fn worker_action(app: AppHandle, action: String) -> Result<UiState, String> {
    if action == "start" || action == "stop" {
        worker_do(&app, &action).await?;
    }
    changed(&app);
    Ok(ui_state(&app).await)
}

#[tauri::command]
fn test_notification(app: AppHandle) {
    notify(&app, "Rook", "Notifications are working.");
}

// ---- entry -----------------------------------------------------------------------------

fn config_dir(app: &AppHandle) -> PathBuf {
    app.path()
        .app_config_dir()
        .unwrap_or_else(|_| PathBuf::from(".rook-desktop"))
}

/// `--show <window>` from a command line.
fn show_arg(args: &[String]) -> Option<String> {
    args.iter().skip_while(|a| *a != "--show").nth(1).cloned()
}

/// What a launch asks for: a window, or (started at sign-in, or a plain
/// second launch) nothing special.
fn show_requested(app: &AppHandle, which: Option<&str>) {
    match which {
        Some("overlay") => show_overlay(app),
        Some("panel") => show_panel(app),
        Some("dashboard") => {
            if open_dashboard(app, "/").is_err() {
                show_settings(app, Some("hub"));
            }
        }
        Some(_) => show_settings(app, None),
        None => {}
    }
}

fn current_os() -> &'static str {
    if cfg!(target_os = "linux") {
        "linux"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(windows) {
        "windows"
    } else {
        "other"
    }
}

// ---- --check: what the app sees on this computer, without starting it ------------------

/// `rook-desktop --check [file]`: the app's view of this computer as JSON
/// (version, settings folder, sign-in, the worker, microphone, speaker, web
/// view, the hub's answer), printed and, with a file, written there too (a
/// Windows build has no console). Changes nothing.
fn check(out: Option<&str>) -> i32 {
    let dir = dirs::config_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(IDENTIFIER);
    let store = Store::new(dir.clone());
    let s = store.load_settings();
    let c = store.load_credentials();
    let origin = config::normalize_hub_url(&s.hub_url).unwrap_or_default();
    let worker = worker::Controller::new(worker::Layout::new(&dir)).info();
    let hub = tauri::async_runtime::block_on(async {
        if origin.is_empty() {
            return json!({"configured": false});
        }
        let mut v = json!({"configured": true, "origin": origin});
        if c.signed_in_to(&origin) {
            v["waiting"] = match hub::Hub::new(&origin, &c.token).waiting().await {
                Ok(w) => json!({"ok": true, "count": w.len()}),
                Err(e) => json!({"ok": false, "error": e.to_string()}),
            };
        }
        v
    });
    let report = json!({
        "app": "rook-desktop",
        "version": env!("CARGO_PKG_VERSION"),
        "os": current_os(),
        "features": {"voice": cfg!(feature = "voice"), "wake_word": cfg!(feature = "wake-word")},
        "config_dir": dir.display().to_string(),
        "settings_saved": dir.join("settings.json").is_file(),
        "signed_in": c.signed_in_to(&origin),
        "principal": c.principal,
        "speak_answers": s.speak_answers,
        "start_at_login": s.start_at_login,
        "worker": worker,
        "microphone": microphone_name(),
        "speaker": speaker_name(),
        "webview": tauri::webview_version().ok(),
        "hub": hub,
    });
    let text = serde_json::to_string_pretty(&report).unwrap_or_default();
    println!("{text}");
    if let Some(path) = out {
        if let Err(e) = std::fs::write(path, &text) {
            eprintln!("couldn't write {path}: {e}");
            return 1;
        }
    }
    0
}

#[cfg(feature = "voice")]
fn speaker_name() -> Option<String> {
    use cpal::traits::{DeviceTrait, HostTrait};
    cpal::default_host().default_output_device().and_then(|d| d.name().ok())
}
#[cfg(not(feature = "voice"))]
fn speaker_name() -> Option<String> {
    None
}

pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let args: Vec<String> = std::env::args().collect();
    if let Some(i) = args.iter().position(|a| a == "--check") {
        std::process::exit(check(args.get(i + 1).map(String::as_str)));
    }
    let show = show_arg(&args);
    let background = args.iter().any(|a| a == BACKGROUND_ARG);
    let app = tauri::Builder::default()
        // First: a second copy hands its request to this one and exits.
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            let want = show_arg(&argv).or_else(|| Some("panel".into()));
            show_requested(app, want.as_deref());
        }))
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .args([BACKGROUND_ARG])
                .app_name("Rook")
                .build(),
        )
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
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
            let first_run = settings.hub_url.is_empty();
            let autostart = settings.worker_autostart;
            app.manage(App {
                store,
                settings: Mutex::new(settings),
                creds: Mutex::new(creds),
                worker: Mutex::new(worker::Controller::new(worker::Layout::new(&config_dir(&handle)))),
                pages: Mutex::new(Vec::new()),
                shortcuts: Mutex::new((None, None)),
                signin_gen: AtomicU64::new(0),
                worker_item: Mutex::new(None),
                update: Mutex::new(None),
                speaking: Mutex::new(None),
                #[cfg(feature = "voice")]
                recording: Mutex::new(None),
                #[cfg(feature = "wake-word")]
                listener: Mutex::new(None),
            });
            let no_tray = build_tray(&handle)
                .map_err(|e| log::warn!("no tray icon: {e}"))
                .is_err();
            if let Err(e) = build_overlay(&handle) {
                log::error!("overlay: {e}");
            }
            if let Err(e) = apply_shortcuts(&handle) {
                log::warn!("{e}");
                notify(&handle, "Rook", &e);
            }
            apply_wake(&handle);
            if let Err(e) = apply_start_at_login(&handle) {
                log::warn!("{e}");
            }
            if !no_tray {
                refresh_worker_item(&handle);
            }
            if autostart {
                let h = handle.clone();
                tauri::async_runtime::spawn(async move {
                    let (info, _) = system_facts(&h).await;
                    if info.status == "stopped" {
                        if let Err(e) = worker_do(&h, "start").await {
                            log::warn!("worker autostart: {e}");
                        }
                    }
                });
            }
            spawn_poller(handle.clone());
            let h = handle.clone();
            tauri::async_runtime::spawn(async move {
                refresh_pages(&h).await;
                keep_web_session(&h).await;
            });
            match show.as_deref() {
                Some(w) => show_requested(&handle, Some(w)),
                // Without a tray icon the settings window is the way in.
                None if first_run || no_tray => show_settings(&handle, None),
                // Started by hand (not at sign-in): show the panel.
                None if !background => show_panel(&handle),
                None => {}
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_state,
            save_settings,
            save_secrets,
            sign_in_token,
            sign_out,
            open_dashboard_cmd,
            ask,
            overlay_hide,
            open_settings,
            show_window,
            ptt_toggle,
            worker_action,
            worker_add,
            test_notification,
            panel_data,
            ask_on_page_cmd,
            stop_speaking,
            open_update,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Rook");
    app.run(|app, event| match event {
        // A tray app keeps running with no window open; Quit exits.
        RunEvent::ExitRequested { api, code: None, .. } => api.prevent_exit(),
        RunEvent::Exit => state(app).worker.lock().unwrap().shutdown(),
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
    fn dashboard_window_stays_on_the_hub() {
        let hub: url::Url = "https://rook.example.com".parse().unwrap();
        let o = hub.origin();
        let ok = |u: &str| stays_in_window(&u.parse().unwrap(), &o);
        assert!(ok("https://rook.example.com/#chat"));
        assert!(ok("https://rook.example.com/account/login?x=1"));
        assert!(ok("about:blank"));
        assert!(!ok("https://accounts.google.com/o/oauth2/auth"));
        assert!(!ok("http://rook.example.com/"), "another scheme is another origin");
        assert!(!ok("https://rook.example.com:8443/"));
        assert!(!ok("https://evil.example/rook.example.com"));
        assert!(!ok("file:///etc/passwd"));
    }

    #[test]
    fn webview_cookie_matches_the_hub() {
        let c = session_cookie_for_webview("https://rook.example.com", "rook_web=abc").unwrap();
        assert_eq!((c.name(), c.value()), ("rook_web", "abc"));
        assert_eq!(c.domain(), Some("rook.example.com"));
        assert_eq!(c.secure(), Some(true));
        let lan = session_cookie_for_webview("http://192.0.2.10:7005", "rook_web=x").unwrap();
        assert_eq!(lan.secure(), Some(false));
        assert!(session_cookie_for_webview("https://a.example", "garbage").is_none());
    }
}
