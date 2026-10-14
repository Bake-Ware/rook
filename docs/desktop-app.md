# Rook on your desktop (preview)

The Rook desktop app keeps your hub one keypress away. It sits in the tray
(the menu bar on a Mac) and gives you:

- **your dashboard in a window**, signed in;
- **quick ask**: press `Ctrl+Alt+Space` anywhere, type a question, press
  Enter, and your hub's home agent answers;
- **notifications** when someone messages you in Chat, and (on hubs that
  support it) when an agent asks you a question or a task needs you;
- **voice**: hold `Ctrl+Alt+V`, speak, let go. Optionally, a wake word;
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

## Quick ask

The quick-ask box talks to your hub's **home agent** (Manage > Home agent in
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
- a huddle question for you, or a task that needs you, on hubs that list them
  (see [For developers](#for-developers-the-attention-list) below).

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

The answer is shown in the quick-ask box; spoken answers are planned.

## This computer's worker

A worker lets your hub and its agents use this computer. Settings > This
computer's worker shows whether one is installed and running:

- installed by the hub's installer as a background service (Linux, Windows):
  **Start** and **Stop** control that service;
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
- **macOS:** the hub installer is for Linux. Run the hub in Docker Desktop
  (`docker compose up -d` in the Rook repository), or on a Linux machine on
  your network, and connect the app to it.
- **Windows:** run the hub in WSL (Windows Subsystem for Linux) with the
  Linux installer, or in Docker Desktop.

## Privacy and security

- The app stores your hub sign-in (the same session cookie your browser
  keeps), the voice key and the optional agent token in `credentials.json`
  in its settings folder, readable only by you. Signing out clears the
  sign-in and the dashboard window's cookies.
- The dashboard window shows your hub's own pages. They can't use the app's
  features (microphone, hotkeys, files); only the app's built-in pages can.
- Only the hub's operator account (or the dashboard password) can use the
  app, the same as the dashboard.

## For developers: the attention list

The app asks `GET /api/attention` on every check (with the dashboard
session). Hubs that don't have it answer 404; the app then asks again only
every 20 checks. The contract, for the huddle and task work to implement:

```json
{"items": [
  {"id": "huddle:7f3c:42", "kind": "question", "title": "Release huddle",
   "text": "Should I roll the update to the garage worker now?"},
  {"id": "task:t_91ab", "kind": "task", "title": "Rotate the voice key",
   "text": "Blocked: needs your approval"}
]}
```

`id` must be stable while the item is open (the app notifies once per id);
`kind` is `question` or `task` (anything else shows as "Needs you"). Items
disappear from the list once answered or done.

See also: [desktop/README.md](../desktop/README.md) (building, design
choices) and [design/desktop-voice.md](design/desktop-voice.md).
