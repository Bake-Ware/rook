---
id: voice
title: Voice assistant services
order: 20
features:
  - features/voice-on-the-hub
  - plugin/hub/voice
  - settings/voice
  - settings/decision
since: 0.1.0
deprecated: false
---
# Voice assistant services

The phone app's voice assistant ([[manual-phones-and-tablets]]) talks to a
**voice service**. It turns speech into text, passes it to a language model
that can use Rook, and turns the answer back into speech. An optional
**decision engine** decides, moment to moment, whether speech near the phone
was meant for the assistant.

## Voice through your hub

The **voice** hub plugin puts the voice service on your hub's own address,
so phones, the desktop app and browsers only need to know the hub. Give the
apps `wss://<your hub>/voice/ws`. A browser can talk to it at
`https://<your hub>/voice/`, and voice keys are managed at
`https://<your hub>/voice/admin`. These paths use voice keys, not the
dashboard sign-in.

Open **Manage > Voice** (operator account). It shows whether voice is ready,
the **Voice address** to give your devices, and **Where speech runs**:

- **On this hub**: speech runs inside the hub. The hub needs the voice extras
  (`pip install 'rook[voice]'`, about a gigabyte) and three speech model files
  in its model folder; the page lists what is missing. The models take a
  little while to load after a start; until then voice answers "starting".
- **On another machine**: the hub passes everything on to a voice server on a
  machine with more power, such as one with a graphics card. Enter its
  **Voice server address** (without a user name or password in it) and use
  **Check connection**. Voice keys are then that server's own; its key page
  still works through the hub at `/voice/admin`.

The **Microphone check** records a few seconds in your browser and shows what
the hub heard. The recording goes to the hub once and is not kept. Your
browser needs a secure (https) connection to use the microphone.

If the plugin cannot run, it stays off, and both the Plugins page
([[manual-plugins]]) and the Voice page say exactly why: the extras or model
files are missing, the other machine's address is not set, or the hub runs
the older two-program layout. Voice on the hub's address needs the hub to run
as one program (`rook hub serve`); with the older layout, keep using the
separate voice service described below. A small hub that does not use voice
never loads any of it.

Changing a voice setting restarts voice (and reloads the models when speech
runs on the hub).

## Settings

Everything is on the Settings page, under **Voice**:

- **Engine**: where speech runs, the other machine's address (and, advanced,
  whether to check its certificate), the model folder, and whether the hub
  also answers the Android app's older addresses at the top of the site.
- **Access**: the token the apps use (an owner voice key; in "another
  machine" mode the hub also uses it for the microphone check) and whether
  clients without a token may connect (conversation and web search only).
- **Recognition**: the speech model and how sensitive it is.
- **Speech**: the voice, the assistant's name and whose assistant it is, and
  per-account choices such as the wake word.
- **Agent**: the language model and the Rook agent token the voice agent
  uses.
- **Standalone server** (advanced): the address and port the separate voice
  service listens on.

## The separate voice service

The voice service and the decision engine can also run as separate programs
(in `services/` in the Rook source), for example on a machine with a graphics
card. They fetch their settings from the hub, so you set them up on the
Settings page too: **Voice** as above, and **Decision engine** (where the
engine runs, the assistant's names, timings, and how long raw recordings are
kept). When your hub passes voice on to such a server, devices can use the
hub's address and the server no longer needs its own public address.

## Spoken replies

When an agent asks you something through the phone, it can wait for an answer.
After a short beep the phone listens; what you say goes back to the agent as
text. Anything near the phone can be heard, so agents treat a spoken reply as
what the microphone heard, not as a confirmed order.
