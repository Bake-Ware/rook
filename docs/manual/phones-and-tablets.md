---
id: phones-and-tablets
title: Phones and tablets
order: 17
features:
  - features/hardware-integrations
  - features/android-phones-and-tablets
since: 0.1.0
deprecated: false
---
# Phones and tablets

The Rook app turns an Android phone or tablet into a worker. It runs in the
background, survives the phone's battery saving and starts again after a
reboot. Phones and tablets show on the Workers page with their own icons and a
battery level.

## Installing the app

1. On the phone, open `https://<your-hub>/apk` and install the app. (Android
   asks you to allow installs from your browser.)
2. Open the app's settings and either **sign in with Google** or enter a
   **pairing code** from the Install page ([[manual-adding-machines]]).
3. The phone joins the band and appears on the Workers page.

The app updates itself: it checks your hub when it starts and every six hours,
and only installs an update that is signed by the same developer key as the
installed app.

## Permissions

Each sensitive ability needs its own Android permission, which you grant in the
app one by one: screen capture, accessibility (for typing and tapping), SMS,
call log, contacts, location and notifications. Grant only what you want Rook
to use.

## What a phone can do

Besides running commands and handling files like any worker, a phone can:

- take a screenshot of its screen;
- type, tap, swipe and read the text on screen (no root needed);
- list and send texts, read the call log and search contacts;
- read, clear and post notifications;
- report where it is, with a map link;
- report its battery;
- **ring at full volume** to find it, even on silent;
- **speak** a message out loud, so any agent or job can talk to you, and listen
  for your spoken reply;
- wake the screen, unlock a PIN lock screen, open an app or a link;
- switch the torch on, vibrate, and read or set the clipboard.

## The voice assistant

The app is also a front end for your agents: chat or talk to them, use a
hands-free wake word, pick a voice, and follow what the agent is doing in the
Chat, Activity and Decisions tabs. You can make it Android's default assistant.
It needs the voice service on your hub, see [[manual-voice]].
