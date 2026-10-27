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
   asks you to allow installs from your browser.) A hub set up with the
   installer has the release's app there from the start, without signing in.
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

## Home-screen widgets

Long-press the home screen, choose **Widgets** and find the Rook app. There
are three:

- **Talk to <assistant>**: one tap starts talking to your house agent, the
  same as the app's Talk button. Tap again while it answers to stop it. The
  first time, the app opens to ask for the microphone.
- **Waiting on you**: the decisions, questions and alerts waiting on you, with
  urgent ones and ones that didn't go through at the top. Tap one to answer it
  on its card, tap the title to open the app, or use the mic to talk. Make it
  taller to see more.
- **Chat with <assistant>**: push-to-talk with the conversation above it, as
  the app's Chat tab shows it: what you said on the right, the agent's replies
  on the left, newest at the bottom. Replies appear as they come in, and the
  agent's name shows whether it is listening or thinking. Tap **Talk** to
  talk (tap again while it answers to stop it), scroll for earlier turns, and
  tap the name or a bubble to open the app. Like the app, it starts empty
  again after the app restarts or updates.

They show what is already on the phone, so they work without a connection and
change as soon as a card arrives or is answered ([[manual-phone-attention]]).

## A watch (Wear OS)

The Rook watch app (`rook-wear.apk` in each release, version 0.1.0 and up)
puts the voice assistant on a Wear OS watch paired with a phone that runs the
Rook app. Open Rook on the watch (from the app list, its tile or a key you map
to it) and it starts listening; tap to end the conversation or stop a reply.

The screen is the conversation as it goes: what you said, Rook's replies as
they come in, and the earlier lines from the phone's chat, scrolling with the
bezel. Your watch conversations show in the phone's chat too. Below it is a
small Rook (also on the tile) whose color says what it is doing: gray idle,
indigo connecting, green listening, amber thinking, blue speaking (until the
watch has played the last word), red when the conversation stopped on an
error (the reason is the last line).

The watch is only a microphone and a speaker. It sends your voice to the Rook
app on the paired phone, which holds the voice session with the phone's own
voice settings and account, and plays the reply on the watch or its earbuds.
Nothing new is trusted: the voice still checks who is speaking, as it does on
the phone. The watch app must come from the same release as the phone app
(same package and signature), or the two can't talk.

To install it, turn on ADB debugging and Wireless debugging in the watch's
Developer options, pair a computer with the code the watch shows, and run
`adb install rook-wear.apk`. Allow the microphone the first time you open it.
