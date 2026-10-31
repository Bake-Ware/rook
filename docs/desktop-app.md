# Rook on your desktop (preview)

The Rook desktop app keeps your hub one keypress away. It sits in the tray
(the menu bar on a Mac) and gives you:

- **your dashboard in a window**, signed in, and a tray **Dashboard** menu
  with its five sections (Feed, Work, Intelligence sources, Trust,
  Settings) and every page in them;
- **the Rook panel**: what is waiting on you, the work in progress, a
  knowledge search and your agents' sessions, each one click from the
  dashboard page that handles it;
- **quick ask**: press `Ctrl+Alt+Space` anywhere, type a question, press
  Enter, and your hub's home agent answers;
- **ask about this page**: the dashboard's own chat with the house agent,
  which sees the page you are on;
- **notifications** when someone messages you in Chat, and when something
  waits on you (an agent's held call, a huddle question or card, a house
  agent ask, a trust change);
- **voice**: hold `Ctrl+Alt+V`, speak, let go, and hear the answer read
  aloud. Optionally, a wake word;
- **this computer's worker**: start or stop it from the tray;
- **a hub of your own**: on Linux, the app can install one for you.

It works on Windows, macOS and Linux. The app is a preview: installers are
not signed yet, so your system may ask you to confirm before opening it.

## Getting started

1. Install and open the app. The settings window opens the first time.
2. Type your hub's address (the one you open the dashboard at), for example
   `https://rook.example.com` or `http://192.0.2.10:7005` at home.
3. Click **Sign in**. Your dashboard opens in a window: sign in there the way
   you always do. The settings page shows **Signed in** when the app has it.
   Or click **Use a password instead** and type your account's user name and
   password, or the dashboard password (any user name).
4. Press `Ctrl+Alt+Space` and ask something.

No hub yet? Click **Install a hub on this computer** (see below).

## A Rook 0.5 hub (preview)

Rook 0.5 (castling) is being tried beside 0.4. The app speaks to either:
Settings > Your hub > **Rook version** picks one (0.4 until you change it).
On 0.5:

- **Sign in** with your own sign-in token, the one you paste on the hub
  site's sign-in page. An agent's token is refused: the app acts as you.
  The dashboard window opens signed in.
- The tray's **Dashboard** menu and the panel list the hub's own pages (they
  change as agents make pages).
- **The panel**: calls waiting for your approval, tasks in progress or
  blocked, a knowledge (and task) search, and the hub's agents (awake ones
  first). Each opens the dashboard page that handles it.
- **Quick ask** talks to the house agent in your own conversation with it,
  the same one the dashboard's dock shows. **Ask about this page** opens
  that dock.
- **Notifications** tell you when a call waits for your approval. Agents
  reach you on this screen through this computer's worker
  (`notification.post`).
- **This computer's worker**: **Add this computer** makes it a worker on
  your hub. The app runs `python -m rook.core2.worker` from a Python with
  Rook installed (Settings > This computer's worker > Python; empty is
  `python3`), enrolls it with a device code and approves that code as you,
  then starts it. It loads the shell, file, notification and screenshot
  plugins and runs while the app is open. Its files are in the app's
  settings folder, under `worker05/`. A voice server in the hub's config
  bundle fills the voice settings you left empty.
- Not on 0.5 yet: the update check, the hub installer, the 0.4 agent-token
  option. Off this computer the hub needs https for **Add this computer**
  (the band key travels in the bundle).

Opening the app again (the Start menu, a shortcut) shows the Rook panel of
the copy already running; only one copy runs at a time. To have Rook start
when you sign in to the computer, tick **Start Rook when I sign in** in
Settings > This app. Started that way, it stays in the tray.

## The Rook panel

Tray > **Open Rook** (or open the app again). Four tabs, read from your hub
as you, the way the dashboard reads them:

- **Waiting on you**: everything the dashboard's Approvals page lists, with
  who asks, for what, and how long it has waited. Click one to decide it on
  the dashboard (held calls on Approvals, cards and questions on Huddles,
  house asks on Decisions, trust changes on Trust map). The panel decides
  nothing itself.
- **Work**: tasks in progress (and who holds them) and blocked tasks (and
  why), by project. Click one to open it on Work.
- **Knowledge**: search your knowledge pages and tasks; click a page to read
  it on Knowledge.
- **Sessions**: your agents' live sessions on every computer (Claude Code,
  Codex, shells), with where they run. Click one to open it on Sessions.
  This list needs an owner account sign-in; the shared dashboard password
  can't open it.

The buttons above the tabs open any dashboard page by section, the same list
as the tray's **Dashboard** menu. **Ask** opens the quick-ask box.

## Ask about this page

Tray > **Ask about this page**, or the link in the quick-ask box or the
panel, brings up the dashboard window with its **@home** chat open. That
chat sends the house agent the page you are on and what you selected, and
the agent can point at, fill in or click things on the page for you (on
hubs with the page-aware chat; see the home agent's manual page). The
dashboard window opens first if it isn't open.

## Quick ask

The quick-ask box talks to your hub's **home agent** (Intelligence sources > Home agent in
the dashboard). If the home agent is off, the box says so.

Your questions and its answers go to a private chat room named
**Quick ask (desktop)**, so you can scroll back through them in Chat, and the
home agent sees your recent questions there as context. **Open in Chat** under
an answer takes you there.

If you'd rather the app ask the home agent directly, the way agents do, make
a token on the dashboard's Tokens page and paste it into Settings > Quick ask
> Advanced. Then questions aren't kept in Chat.

Change the shortcut in Settings > Quick ask: click **Change** and press the
keys you want. On Linux, global shortcuts need an X11 session; under Wayland
use the tray menu's **Ask the home agent**.

## Notifications

With **Tell me about new messages and things that need me** on (Settings >
Notifications), the app checks your hub every 30 seconds (you can change
that) and shows a notification for:

- a new message in a chat room you're in (not your own messages, and not the
  quick-ask room, whose answers already show in the box);
- something new waiting on you: the same items as the panel's **Waiting on
  you** tab.

The first check after the app starts only notes where things stand, so you
don't get a burst of old messages. More than three at once become one summary.
Checking never marks anything as read.

## Voice

Voice uses your voice server (the one the Android app talks to) to turn
speech into text, then asks the home agent like a typed question.

1. In Settings > Voice, enter the voice server address (for example
   `https://voice.example.com`; the phone's `wss://.../ws` address works too)
   and its voice key, if it needs one.
2. Hold `Ctrl+Alt+V`, speak, and let go. Or click the microphone in the
   quick-ask box, speak, and click it again.

**Wake word** (builds with the wake word feature): turn on **Listen for a
wake word**, and pick your wake word model, an openWakeWord `.onnx` file such
as the one your phone uses. Say the wake word, then your question; the app
sends what you say after it once you pause. The microphone stays open while
the wake word is on, but nothing leaves your computer until the wake word is
heard. Rook doesn't include a wake word model: openWakeWord's ready-made ones
are licensed for non-commercial use only, so you train or bring your own.

The app checks the model when you save. If a model it accepted later can't
be loaded (the file was moved or replaced), it turns the wake word off and
tells you, rather than failing every time it starts. Models over 10 MB are
refused (wake word models are well under 1 MB).

**Answers read aloud.** Settings > Voice > **Read answers aloud**: *When I
asked by voice* (the default), *Always*, or *Never*. The voice server reads
the answer in its own voice (the one the phone uses); links, code and tables
are left out, and very long answers stop after a few paragraphs (the rest is
on screen). Press Esc in the quick-ask box, or start talking, to stop it. A
voice server without speech (`/api/voice`) just shows the answer.

## This computer's worker

A worker lets your hub and its agents use this computer. Settings > This
computer's worker shows whether one is installed and running:

- installed by the hub's installer as a background service (Linux, Windows):
  **Start** and **Stop** control that service. On Windows a worker that
  restarted itself after an update runs outside its Scheduled Task; the app
  still shows it as running, and **Stop** stops it;
- installed without a service (macOS, some Linux): the app runs it while the
  app is open. Tick **Start it when Rook starts** to have it come up with
  the app;
- not installed: **Add this computer** opens your hub's Install page, where
  you get the one-line install command with a pairing code.

## Install a hub on this computer

Tray > **Install a hub on this computer** opens a short wizard.

- **Linux:** choose who should reach the hub (your home network, or only
  this computer) and click **Install the hub**. It runs Rook's hub installer
  for your user account (no administrator password unless Python is
  missing), shows its progress, and then shows the dashboard address and
  password. **Connect this app to it** signs you in. If something fails, the
  wizard shows the command to run in a terminal instead.
- **Already installed:** if this computer already has a hub from Rook's
  installer, the wizard says so, shows its address and whether its service
  (`rook-hub`) is running, and offers **Connect this app to it** and
  **Update it** (re-runs the installer; your data and passwords are kept). A
  system-wide install is updated from a terminal with the command shown.
- **macOS:** the hub installer is for Linux. Run the hub in Docker Desktop
  (`docker compose up -d` in the Rook repository), or on a Linux machine on
  your network, and connect the app to it.
- **Windows:** run the hub in WSL (Windows Subsystem for Linux) with the
  Linux installer, or in Docker Desktop.

## Updates

The app asks your hub about once every six hours whether a newer desktop app
is published (on the latest Rook release). When there is one, it tells you
once, and Settings > This app shows **Download version ...**, which opens the
installer for your system in your browser (the setup program on Windows,
the disk image on macOS, the AppImage or .deb on Linux, matching how you
installed it). Run it to update; your settings are kept. The app only offers
installers published on Rook's GitHub releases, whatever the hub lists.

## Checking an install

`rook-desktop --check` (on Windows, `rook-desktop.exe --check report.json` in
`%LOCALAPPDATA%\Rook`, since the Windows app has no console) prints what the
app sees on this computer without starting it: its version, settings folder,
sign-in, this computer's worker, the microphone and speaker, the web view,
and the hub's answers. It changes nothing.

## Privacy and security

- The app stores your hub sign-in (the same session cookie your browser
  keeps), the voice key and the optional agent token in `credentials.json`
  in its settings folder, readable only by you. Signing out clears the
  sign-in and the dashboard window's cookies.
- The dashboard window shows your hub's own pages and nothing else: a link
  to another site opens in your usual browser. Hub pages can't use the app's
  features (microphone, hotkeys, files); only the app's built-in pages can.
- Only the hub's owners (or the dashboard password) can use the
  app, the same as the dashboard. The panel only reads; everything it shows
  is decided or changed on the dashboard.
- "Ask about this page" runs one fixed script in the dashboard window (open
  the chat, focus its text box); the window still has no access to the app.

## For developers: what the app reads

All with the dashboard session, as the signed-in person:

- `/api/chat/rooms`: new messages (never moves the read mark);
- `/api/band/call` on the hub's node, read-risk caps only, which the hub runs
  as the person: `approvals.list` (waiting on you, and its notifications),
  `task.read` (`action=deck`, in progress and blocked), `knowledge.read`
  (`action=search`), `home.status`;
- `/account/work/sessions?live_only=1` (the Sessions page's list);
- `/updates/desktop.json` (public): the release's `rook-desktop-*` files.

On a 0.5 hub, with the person's token on the HTTP door (`Authorization:
Bearer`): `GET /holds`, and `POST /call` on the hub's node with a why for
`task.list`, `project.list`, `knowledge.search`, `task.search`,
`agents.agent.list`, `pages.list` and `agents.chat.send`; `POST /enroll/approve`
for its own worker's device code. The hub site's `/login` (the token, once)
gives the dashboard window's session; `/web/api/me` names the person.
tests/test_desktop_contract05.py checks these against the castling code.

tests/test_desktop_contract.py checks these, the dashboard links the panel
opens (`#work?t=`, `#knowledge?p=`, `#sessions?key=`), the @home dock's
markup and the sidebar's sections against the hub's code.

See also: [desktop/README.md](../desktop/README.md) (building, design
choices) and [design/desktop-voice.md](design/desktop-voice.md).
