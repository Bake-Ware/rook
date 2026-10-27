# Rook desktop

A tray app for a Rook hub, for Windows, macOS and Linux. Version 0.2 is a
preview. User guide: [docs/desktop-app.md](../docs/desktop-app.md). Voice
design: [docs/design/desktop-voice.md](../docs/design/desktop-voice.md).

What it does:

- shows the hub's dashboard in a window, signed in, with a tray Dashboard
  menu of its five sections (`ui/sections.json`, checked against the hub's
  sidebar by tests/test_desktop_contract.py);
- the Rook panel (`ui/panel.html`): what waits on you (`approvals.list`),
  tasks in progress and blocked (`task.read` deck), knowledge search
  (`knowledge.read`) and agent sessions (`/account/work/sessions`), each item
  a link into the dashboard;
- opens a quick-ask box on a global hotkey (default `Ctrl+Alt+Space`) and
  asks the hub's home agent; "Ask about this page" opens the dashboard's
  page-aware @home dock instead;
- shows desktop notifications for new chat messages and for what waits on
  you;
- push-to-talk (hold `Ctrl+Alt+V`) with answers read aloud by the voice
  server (`/api/voice`, played with cpal) and, in builds with the
  `wake-word` feature, a wake word;
- one copy at a time (a second launch shows the panel), start at sign-in,
  and an update check against the hub's `/updates/desktop.json`;
- starts and stops this computer's worker;
- installs a hub on this computer (Linux) or explains how to (macOS: Docker;
  Windows: WSL or Docker).

## Why Tauri 2 and not Electron

Bake's brief was "a desktop app, electron maybe, rust exe maybe?". This is a
Rust exe: [Tauri 2](https://v2.tauri.app) (Apache-2.0 or MIT).

| | Tauri 2 | Electron |
|---|---|---|
| Installer size | .deb 7.5 MB with the wake word (uses the system webview); the AppImage is 82 MB because it carries WebKitGTK | 80 to 120 MB on every platform (bundles Chromium and Node) |
| Memory at idle in the tray | tens of MB | 100 MB or more (a Chromium browser and a Node process) |
| Web UI | the hub's own pages, unchanged, in the system webview | the same pages in bundled Chromium |
| Native work (mic, wake word, global hotkey, child processes) | Rust, in-process | Node native modules, rebuilt per Electron version |
| Hub content and the app's powers | remote pages get no IPC unless a capability names them | needs careful `contextIsolation` and preload work |
| Toolchain | cargo; no Node needed to build (the UI is plain files) | Node, npm, electron-builder |

The app spends most of its life in the tray with a microphone open (with the
wake word on), so idle memory and CPU matter more than anything else; Tauri
wins there by a wide margin. The rest of Rook is Python and Rust already
(the relay is Rust), and the ONNX wake word runs well in Rust (tract).

The cost is that the dashboard renders in each platform's webview (WebView2
on Windows, WKWebView on macOS, WebKitGTK on Linux) rather than one Chromium.
The dashboard is plain HTML, CSS and JS that already works in Safari and
Firefox, so this is a small risk; WebKitGTK is the one to test.

## Layout

```
desktop/
  ui/                 the app's own pages: settings, quick-ask overlay, hub installer (plain HTML/JS, no build step)
  src-tauri/
    src/lib.rs        tray, windows, hotkeys, commands, background polling
    src/config.rs     settings.json and credentials.json (0600)
    src/hub.rs        sign-in, home agent (chat or MCP), chat rooms, the panel's lists, updates
    src/notify.rs     what deserves a notification (pure)
    src/sections.rs   the dashboard's sections for the tray menu (ui/sections.json)
    src/update.rs     which published file is a newer app for this install (pure)
    src/worker.rs     find and start/stop this computer's worker
    src/installer.rs  the hub installer wizard's plan, arguments and output parsing
    src/voice/        audio helpers, transcription, capture and playback (cpal),
                      speech pieces and WAV (speak.rs), wake word (tract)
    tests/live_hub.rs end-to-end against a running hub (ignored by default)
```

The dashboard window loads the hub's own site and stays there:

- **No app access for hub pages.** `build.rs` declares every app command in
  an ACL manifest, so a command is callable only from a window whose
  capability allows it (`capabilities/settings.json`, `overlay.json`,
  `install.json`, `panel.json`, each listing just what that page uses). No
  capability names the dashboard window. "Ask about this page" runs one
  constant script there (`OPEN_DOCK_JS`: click the dock's launcher, focus its
  text box); nothing from the page comes back to the app.
- **Navigation guard.** It may navigate only within the hub's origin. Links
  elsewhere, popups and `target=_blank` open in the system browser (the
  opener plugin); other schemes are dropped.

## Talking to the hub

Nothing new is needed on the hub. The app uses the dashboard API with the
same session cookie a browser gets:

- **Sign in** in the dashboard window (the app reads the `rook_account` or
  `rook_session` cookie from the webview's cookie store once you are in), or
  with a user name and password (the account login form, else the dashboard
  password). Only the hub's operator can use the dashboard API, the same rule
  as the browser.
- **Quick ask.** `home.ask` is the API `rook_home_ask` uses, but the hub
  refuses write-risk caps over the band, so `/api/band/call` can't reach it
  (by design: the dashboard's band path must not widen what callers may do).
  So by default the app asks in a private two-person chat room with the home
  agent (`Quick ask (desktop)`), which the home agent answers like any
  `@home` message, with the room's recent messages as context. With an agent
  token (Tokens page) in Settings > Quick ask > Advanced, it calls
  `rook_call(worker="rook", cap="home.ask")` over the hub's `/mcp`, exactly as
  `rook_home_ask` does.
- **Notifications** poll `/api/chat/rooms` (unread counts and the last
  message; it never moves your read marker) and `approvals.list` (a hub
  without it is asked again only every 20 polls).
- **The panel** calls read-risk hub caps through `/api/band/call`, which
  the hub forwards to its MCP side and runs as the signed-in person.

## Building

Prerequisites: Rust 1.80+, and on Linux the WebKitGTK and audio headers:

```sh
sudo apt install libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev \
  libxdo-dev libssl-dev libasound2-dev pkg-config
```

The Tauri CLI bundles installers: `cargo install tauri-cli --version '^2'`, or
`npx @tauri-apps/cli@2` (prebuilt, faster).

```sh
cd desktop/src-tauri
cargo run                                   # dev build, default features (push-to-talk)
cargo run --features wake-word -- --show settings
cargo tauri build --features wake-word      # installers in target/release/bundle/
```

`--show settings|overlay|install|panel|dashboard` opens that window at start
(the settings window also opens on first run, and a start by hand shows the
panel; `--background`, which start-at-sign-in passes, shows nothing).
`--check [file]` prints what the app sees on this computer as JSON and exits
(the Windows build has no console, so give it a file there).

Release builds unwind on panic (no `panic = "abort"`): the wake model is
loaded inside `catch_unwind`, so a malformed model file turns the wake word
off instead of crashing the app on every start.

Features:

- `voice` (default): microphone capture (cpal) and push-to-talk.
- `wake-word`: openWakeWord on tract. Off at runtime until the person turns
  it on and picks a model.

Bundles include `scripts/install-hub.sh` and the two openWakeWord feature
models from `android/app/src/main/assets/wakeword/` (no copies in this
directory).

## Tests

```sh
cd desktop/src-tauri
cargo test --features wake-word --lib       # unit tests (no hub, no microphone)
cargo clippy --features wake-word --all-targets
# against a local hub (scripts/local-hub.sh) with the home agent on:
ROOK_TEST_HUB=http://127.0.0.1:7005 ROOK_TEST_PASS=... ROOK_TEST_MCP_TOKEN=... \
  cargo test --test live_hub -- --ignored
```

CI (`.github/workflows/desktop.yml`) runs fmt, clippy and the tests on Linux
and builds unsigned bundles on Linux, macOS and Windows. Actions are pinned
by commit, the Tauri CLI by exact version, and cargo runs with `--locked`.

## Known limits (v0.1)

- **Signing:** bundles are unsigned. macOS Gatekeeper and Windows SmartScreen
  will warn. Signing and notarisation are not wired into CI yet.
- **Global hotkeys on Linux need X11.** Under Wayland the hotkey does not
  register (a platform limit of global shortcuts); use the tray menu, or a
  desktop-environment shortcut that runs the app's "Ask" action (follow-up).
- **The sign-in cookie is stored in a 0600 file** in the app's config
  directory, not the OS keychain (follow-up).
- **Publishing:** CI builds the bundles on every push to master;
  `python tools/desktop_publish.py --run <run id> --tag <release>` attaches
  them to a GitHub release as `rook-desktop-<version>-*`, which is what the
  hub's update check offers (only a green run of a push to master). The app
  offers only installers from Rook's GitHub releases (`update::RELEASES`).
- **Google sign-in** leaves the hub's origin, so the navigation guard sends
  it to the system browser, where it signs in the browser, not the app. Use
  the password sign-in, or a local account.
- **Tray on GNOME** needs the AppIndicator extension (Ubuntu ships it). Where
  no tray shows, the hotkey still works, and running the app again shows the
  panel of the running copy (`--show settings` for its settings).
- **Answers are read aloud piece by piece** (`/api/voice`, up to 700
  characters a request), not streamed: a long answer pauses between pieces.
  The full voice agent (the voice server's streaming protocol) is the next
  step; see the voice design doc.
- **Updates are manual**: the app tells you and opens the download; it
  doesn't install it itself (that needs signed builds).
- **Bundle a hub with the app** ("one binary that also installs the hub"): the
  wizard runs the hub installer; shipping the hub inside the app is a
  follow-up.
