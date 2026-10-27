---
id: desktop-app
title: The desktop app
order: 26
features:
  - features/desktop-app-preview
since: 0.1.0
deprecated: false
preview: true
---
# The desktop app

**Preview:** the desktop app is new. Its installers are not signed yet, so
your computer may ask you to confirm before opening it.

The Rook desktop app sits in your computer's tray (the menu bar on a Mac) on
Windows, macOS and Linux, and keeps your hub one keypress away:

- **The dashboard in its own window**, signed in with your usual Rook login.
  The tray's **Dashboard** menu lists its five sections and their pages.
- **The Rook panel**: what is waiting on you, the work in progress, a
  knowledge search and your agents' sessions.
- **Quick ask**: press `Ctrl+Alt+Space` anywhere, type a question and press
  Enter; your hub's home agent answers ([[manual-home-agent]]).
- **Ask about this page**: opens the dashboard's chat with the house agent,
  which sees the page you are on.
- **Notifications** when someone writes to you in a chat room
  ([[manual-chat]]), and when something waits on you.
- **Voice**: hold `Ctrl+Alt+V`, speak and let go, using your voice server
  ([[manual-voice]]), and hear the answer read aloud. Optionally, a wake word.
- **This computer's worker**: start and stop it from the tray
  ([[manual-adding-machines]]).
- **Install a hub on this computer**, on Linux ([[manual-running-a-hub]]).

Nothing changes on the hub to use it.

## Getting started

1. Install and open the app. The settings window opens the first time.
2. Type your hub's address, the one you open the dashboard at.
3. Click **Sign in**. Your dashboard opens in a window: sign in there the way
   you always do, and the settings page shows **Signed in**. Or click **Use a
   password instead** and type your account's user name and password.
4. Press `Ctrl+Alt+Space` and ask something.

Only the hub's owners (or the dashboard password) can use the app,
the same as the dashboard.

Opening the app again shows the Rook panel of the copy already running. To
start Rook when you sign in to your computer, tick **Start Rook when I sign
in** in Settings > This app.

## The Rook panel

Tray > **Open Rook**. Four tabs:

- **Waiting on you**: what the dashboard's Approvals page lists (an agent
  waiting for your OK, huddle questions and cards, the house agent's asks,
  trust changes). Click one to decide it on the dashboard.
- **Work**: tasks in progress and blocked tasks, by project.
- **Knowledge**: search your knowledge pages and tasks.
- **Sessions**: your agents' live sessions on every computer. This needs an
  owner account sign-in, not the dashboard password.

Click anything to open it in the dashboard window. The buttons at the top
open any dashboard page by section. The panel only shows things; you decide
on the dashboard.

## Ask about this page

Tray > **Ask about this page** (also in the quick-ask box and the panel)
brings up the dashboard window with its **@home** chat open. The house agent
sees the page you are on and what you selected, and can point things out or
fill them in for you ([[manual-home-agent]]).

## Quick ask

The quick-ask box talks to your hub's home agent; if the home agent is off,
the box says so. Your questions and its answers go to a private chat room
called **Quick ask (desktop)**, so you can scroll back through them on the
dashboard's Huddles page (under **Old chats**); **Open in Chat** under an
answer takes you there.

To change the shortcut, open Settings > Quick ask, click **Change** and press
the keys you want. On Linux, global shortcuts need an X11 session; under
Wayland, use the tray menu's **Ask the home agent**.

## Notifications

With **Tell me about new messages and things that need me** on (Settings >
Notifications), the app checks your hub every 30 seconds and shows a
notification for a new message in a chat room you're in, and for anything
new waiting on you. Checking never
marks anything as read, and the first check after the app starts only notes
where things stand, so you don't get a burst of old messages.

## Voice and the wake word

In Settings > Voice, enter your voice server's address (the one the Android
app uses) and its voice key, if it needs one. Then hold `Ctrl+Alt+V`, speak
and let go, or click the microphone in the quick-ask box. The answer is shown
in the box and read aloud. **Read answers aloud** chooses when: when you asked
by voice (the default), always, or never. Press Esc, or start talking, to stop
it.

A wake word is available in builds that include it: turn on **Listen for a
wake word** and pick your own wake word model (an openWakeWord `.onnx` file).
Nothing leaves your computer until the wake word is heard. Rook doesn't
include a model: openWakeWord's ready-made ones are for non-commercial use
only, so you train or bring your own.

## Updates

When a newer desktop app is published, the app tells you once, and
Settings > This app shows **Download version ...**: it opens the installer in
your browser. Run it to update; your settings are kept.

## This computer's worker

Settings > This computer's worker shows whether this computer has a worker
and whether it is running. If the hub's installer set it up as a background
service, **Start** and **Stop** control that service; otherwise the app runs
it while the app is open. With no worker, **Add this computer** opens your
hub's Install page.

## Install a hub on this computer

Tray > **Install a hub on this computer** opens a short wizard. On Linux it
runs Rook's hub installer for you and then shows the dashboard address and
password; **Connect this app to it** signs you in. If a hub from Rook's
installer is already there, the wizard offers to connect to it or update it.
On macOS and Windows it explains how to run the hub in Docker Desktop (or WSL
on Windows) instead.

## Privacy

The app keeps your hub sign-in, the voice key and any agent token in a file
in its settings folder that only you can read; signing out clears the
sign-in. The dashboard window shows only your hub's own pages: other links
open in your usual browser.
