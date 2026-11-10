# Rook desktop

A tray app for a Rook hub, for Windows, macOS and Linux. Version 0.2 is a
preview. User guide: [docs/desktop-app.md](../docs/desktop-app.md). Voice
design: [docs/design/desktop-voice.md](../docs/design/desktop-voice.md).

What it does:

- shows the hub's site in a window, signed in, with a tray Dashboard menu
  of the hub's own pages by section (`page.list`);
- the Rook panel (`ui/panel.html`): the hub's pages, and the calls waiting
  for your approval (`GET /holds`), each item a link into the site;
- opens a quick-ask box on a global hotkey (default `Ctrl+Alt+Space`) and
  asks the hub's house agent (`agent.chat.send`); "Ask about this page"
  opens the site's dock instead;
- shows desktop notifications for what waits on you;
- push-to-talk (hold `Ctrl+Alt+V`) with answers read aloud by the voice
  server (`/api/voice`, played with cpal) and, in builds with the
  `wake-word` feature, a wake word;
- one copy at a time (a second launch shows the panel), start at sign-in,
  and an update check against the hub's `/updates/desktop.json`;
- adds this computer to the hub as a worker, and starts and stops it.

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
  ui/                 the app's own pages: settings, quick-ask overlay, panel (plain HTML/JS, no build step)
  src-tauri/
    src/lib.rs        tray, windows, hotkeys, commands, background polling
    src/config.rs     settings.json and credentials.json (0600)
    src/hub.rs        the hub: token sign-in, the HTTP door, holds, pages, quick ask, the update feed
    src/notify.rs     what deserves a notification (pure)
    src/sections.rs   the hub's pages by section, for the tray menu
    src/update.rs     which published file is a newer app for this install (pure)
    src/worker.rs     this computer's worker (the worker/ crate, compiled in): join with a one-time code, run as a task
    src/voice/        audio helpers, transcription, capture and playback (cpal),
                      speech pieces and WAV (speak.rs), wake word (tract)
    tests/live_hub05.rs  end-to-end against a running hub, adding a worker too (ignored by default)
```

The dashboard window loads the hub's own site and stays there:

- **No app access for hub pages.** `build.rs` declares every app command in
  an ACL manifest, so a command is callable only from a window whose
  capability allows it (`capabilities/settings.json`, `overlay.json`,
  `panel.json`, each listing just what that page uses). No
  capability names the dashboard window. "Ask about this page" runs one
  constant script there (`OPEN_DOCK_JS`: click the dock's button, focus its
  text box); nothing from the page comes back to the app.
- **Navigation guard.** It may navigate only within the hub's origin. Links
  elsewhere, popups and `target=_blank` open in the system browser (the
  opener plugin); other schemes are dropped.

## Talking to the hub

- **Sign in** with the person's door token, the one the hub site's sign-in
  page takes. The site's `/login` checks once that it is a person's token
  (an agent's is refused) and gives the hub's window its `rook_web` session;
  `/web/api/me` names the person.
- **Everything else** is the HTTP door's JSON API with `Authorization:
  Bearer <token>`, as the person: `POST /call` (each with a why, journaled)
  for `agent.chat.send` (quick ask, in the person's own session with the
  house agent, the dock's) and `page.list` (the tray and panel pages, every
  20 polls), and `GET /holds` (the panel and notifications). No MCP door: its
  tokens are an agent's, acting for a person; the app acts as the person.
- **This computer's worker**: "Add this computer" gets a one-time code as
  the person (`POST /enroll/code`) and runs the worker's `enroll` with it;
  the config bundle configures the worker, and its
  voice service fills empty voice settings. The worker crate (`worker/`)
  is compiled into the app and runs as a task, from `<settings>/worker05/`.
- **Updates**: the public `/updates/desktop.json`.

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

`--show settings|overlay|panel|dashboard` opens that window at start
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

Bundles include the two openWakeWord feature models from
`android/app/src/main/assets/wakeword/` (no copies in this directory).

## Tests

```sh
cd desktop/src-tauri
cargo test --features wake-word --lib       # unit tests (no hub, no microphone)
cargo clippy --features wake-word --all-targets
# against a running hub (docs/castling-e2e.md, "A 0.5 hub and worker by hand"):
ROOK_TEST_HUB05=http://127.0.0.1:8740 ROOK_TEST_TOKEN05=<a person's token> \
  ROOK_TEST_ADD=1 cargo test --test live_hub05 -- --ignored
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
- **The sign-in token is stored in a 0600 file** in the app's config
  directory, not the OS keychain (follow-up).
- **Publishing:** CI builds the bundles on every push to master;
  `python tools/desktop_publish.py --run <run id> --tag <release>` attaches
  them to a GitHub release as `rook-desktop-<version>-*`, which is what the
  hub's update check offers (only a green run of a push to master). The app
  offers only installers from Rook's GitHub releases (`update::RELEASES`).
- **Tray on GNOME** needs the AppIndicator extension (Ubuntu ships it). Where
  no tray shows, the hotkey still works, and running the app again shows the
  panel of the running copy (`--show settings` for its settings).
- **Answers are read aloud piece by piece** (`/api/voice`, up to 700
  characters a request), not streamed: a long answer pauses between pieces.
  The full voice agent (the voice server's streaming protocol) is the next
  step; see the voice design doc.
- **Updates are manual**: the app tells you and opens the download; it
  doesn't install it itself (that needs signed builds).
