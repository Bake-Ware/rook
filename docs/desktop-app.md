# Rook on your desktop (preview)

The Rook desktop app keeps your hub one keypress away. It sits in the tray
and gives you:

- **your hub's site in a window**, signed in, and a tray **Dashboard** menu
  with the hub's own pages by section (they change as agents make pages);
- **the Rook panel**: the hub's pages, and the calls waiting for your
  approval, each one click from the page that handles it;
- **quick ask**: press `Ctrl+Alt+Space` anywhere, type a question, press
  Enter, and your hub's house agent answers;
- **ask about this page**: the site's dock, your conversation with the
  house agent, which sees the page you are on;
- **notifications** when a call waits for your approval;
- **voice**: hold `Ctrl+Alt+V`, speak, let go, and hear the answer read
  aloud. Optionally, a wake word;
- **this computer's worker**: add this computer to your hub, and start or
  stop its worker from the tray.

It works on Windows and Linux. The app is a preview: installers are
not signed yet, so your system may ask you to confirm before opening it.

## Getting started

1. Install and open the app. The settings window opens the first time.
2. Type your hub's address (the one you open its site at), for example
   `https://rook.example.com` or `http://192.0.2.10:8740` at home.
3. Click **Sign in** and paste your own sign-in token, the one you paste on
   the hub site's sign-in page. An agent's token is refused: the app acts as
   you. The settings page shows **Signed in**, and the hub's window opens
   signed in.
4. Press `Ctrl+Alt+Space` and ask something.

No hub yet? See [Install and first run](manual-0.5/install.md).

Opening the app again (the Start menu, a shortcut) shows the Rook panel of
the copy already running; only one copy runs at a time. To have Rook start
when you sign in to the computer, tick **Start Rook when I sign in** in
Settings > This app. Started that way, it stays in the tray.

## The Rook panel

Tray > **Open Rook** (or open the app again). The buttons at the top open
any of the hub's pages by section, the same list as the tray's **Dashboard**
menu. **Waiting on you** lists the calls held for your approval, read from
your hub as you; click one to open the journal page where it is decided. The
panel decides nothing itself. **Ask** opens the quick-ask box.

There are no built-in task or knowledge lists: those are the hub's pages,
and an agent opens a page when one is needed.

## Ask about this page

Tray > **Ask about this page**, or the link in the quick-ask box or the
panel, brings up the hub's window with its dock open: your conversation with
the house agent, which sees the page you are on. The window opens first if it
isn't open.

## Quick ask

The quick-ask box talks to your hub's **house agent**, in your own
conversation with it: the same one the site's dock shows. If the house agent
is off, the box says so.

Change the shortcut in Settings > Quick ask: click **Change** and press the
keys you want. On Linux, global shortcuts need an X11 session; under Wayland
use the tray menu's **Ask the home agent**.

## Notifications

With **Tell me about new messages and things that need me** on (Settings >
Notifications), the app checks your hub every 30 seconds (you can change
that) and shows a notification when a call newly waits for your approval.
Agents also reach this screen through this computer's worker
(`notification.post`).

The first check after the app starts only notes where things stand, so you
don't get a burst of old ones. More than three at once become one summary.

## Voice

Voice uses your voice server (the one the Android app talks to) to turn
speech into text, then asks the house agent like a typed question.

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
computer's worker shows whether this computer is added and its worker
running.

- **Add this computer** makes it a worker on your hub. The worker is built
  into the app (no Python needed): it joins with a one-time code the app
  gets as you, then starts. It runs commands and terminals, reads and writes
  files, and opens network connections and serial ports, while the app is
  open; Settings shows how many streams it has open and its last call. The
  app updates it with itself. Its files are in the app's settings folder,
  under `worker05/`. A
  voice server in the hub's config bundle fills the voice settings you left
  empty. Off this computer the hub needs https for this (the band key
  travels in the bundle).
- **Start** and **Stop** start and stop it; tick **Start it when Rook
  starts** to have it come up with the app.

## Updates

The app asks your hub about once every six hours whether a newer desktop app
is published (on the latest Rook release). When there is one, it tells you
once, and Settings > This app shows **Download version ...**, which opens the
installer for your system in your browser (the setup program on Windows,
the AppImage or .deb on Linux, matching how you
installed it). Run it to update; your settings are kept. The app only offers
installers published on Rook's GitHub releases, whatever the hub lists.

## Checking an install

`rook-desktop --check` (on Windows, `rook-desktop.exe --check report.json` in
`%LOCALAPPDATA%\Rook`, since the Windows app has no console) prints what the
app sees on this computer without starting it: its version, settings folder,
sign-in, this computer's worker, the microphone and speaker, the web view,
and the hub's answers. It changes nothing.

## Privacy and security

- The app stores your sign-in token, the hub site's session for its window
  and the voice key in `credentials.json` in its settings folder, readable
  only by you. Signing out clears them, signs the window's session out at
  the hub and clears the window's cookies.
- The hub's window shows your hub's own pages and nothing else: a link to
  another site opens in your usual browser. Hub pages can't use the app's
  features (microphone, hotkeys, files); only the app's built-in pages can.
- Every call the app makes runs as you, with your rights, and is journaled
  with a why. The panel only reads.
- "Ask about this page" runs one fixed script in the hub's window (open the
  dock, focus its text box); the window still has no access to the app.

## For developers: what the app reads

With the person's token on the HTTP door (`Authorization: Bearer`): `GET
/holds` (waiting on you, and its notifications), and `POST /call` on the
hub's node with a why for `page.list` and `agent.chat.send`; `POST
/enroll/code` for its own worker's one-time code. The hub site's `/login`
(the token, once) gives the window's session; `/web/api/me` names the
person; `/updates/desktop.json` (no token) lists the release's
`rook-desktop-*` files. tests/test_desktop_contract.py checks these against
the hub's code.

See also: [desktop/README.md](../desktop/README.md) (building, design
choices) and [design/desktop-voice.md](design/desktop-voice.md).
