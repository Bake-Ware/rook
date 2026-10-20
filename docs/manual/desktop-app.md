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
- **Quick ask**: press `Ctrl+Alt+Space` anywhere, type a question and press
  Enter; your hub's home agent answers ([[manual-home-agent]]).
- **Notifications** when someone writes to you in a chat room
  ([[manual-chat]]).
- **Voice**: hold `Ctrl+Alt+V`, speak and let go, using your voice server
  ([[manual-voice]]). Optionally, a wake word.
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

Only the hub's operator account (or the dashboard password) can use the app,
the same as the dashboard.

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
notification for a new message in a chat room you're in. Checking never
marks anything as read, and the first check after the app starts only notes
where things stand, so you don't get a burst of old messages.

## Voice and the wake word

In Settings > Voice, enter your voice server's address (the one the Android
app uses) and its voice key, if it needs one. Then hold `Ctrl+Alt+V`, speak
and let go, or click the microphone in the quick-ask box. The answer is shown
in the box.

A wake word is available in builds that include it: turn on **Listen for a
wake word** and pick your own wake word model (an openWakeWord `.onnx` file).
Nothing leaves your computer until the wake word is heard. Rook doesn't
include a model: openWakeWord's ready-made ones are for non-commercial use
only, so you train or bring your own.

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
