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
  **Voice server** address (without a user name or password in it) and use
  **Check**. Voice keys are then that server's own; its key page
  still works through the hub at `/voice/admin`.

The **Mic check** records a few seconds in your browser and shows what
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
- **Speech**: the voice, the voice to start with (from either engine, cloned
  voices included), the assistant's name and whose assistant it is, and
  per-account choices such as the wake word. Advanced: how loud replies are
  and how many processor threads the standard voices use.
- **Chatterbox**: cloned voices on a graphics card. Set the card (for example
  `cuda:0`), the Python of a separate environment with Chatterbox installed,
  and a folder of voice recordings: each `<name>.wav` (5 to 20 seconds of
  clean speech) becomes a voice called `<name>`. Left blank, Chatterbox stays
  off and the standard voices speak.
- **Agent**: the language model and the Rook agent token the voice agent
  uses (advanced: how hard it thinks and how long it may work).
- **Models**: for Front/Background conversations, a separate model for the
  quick spoken reply and one for the work behind it (blank uses the Agent
  model), and an optional hosted model tried first, with its API key; if the
  hosted model fails, voice falls back to the Agent model.
- **Home**: your Home Assistant address and token (lights, switches, scenes
  and media players), and the Rook worker that plays music (pianobar).
- **Location**: your home's latitude and longitude for the weather when the
  phone's location is not available, the time zone for timers like "at 7pm",
  and Celsius or Fahrenheit.
- **Standalone server** (advanced): the address and port the separate voice
  service listens on.

Tokens and API keys are kept in the hub's secret store and never shown again
after you save them.

These settings take effect when speech runs **on this hub**. When the hub
passes voice on to **another machine**, that voice server uses its own
configuration, so set Chatterbox, Home Assistant and the rest there.

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

## Reading huddle messages aloud

Every message and action card in a huddle ([[manual-huddles]]) has a small
**Speak** button (▶). Tap it to hear the message in the browser you are
using; tap it again (■) to stop. Only one message speaks at a time.

- **Which voice.** A role's messages are read in that role's **Speaking
  voice**, set on the Roles page from the voices your voice engine has.
  Messages from the chair and from roles without one use the band's default
  voice (the voice engine's own). If a role's voice fails, the default reads
  it instead.
- **Where the speech comes from.** The hub makes the audio with its voice
  engine (on this hub, or the voice server it passes voice to; see above), so
  it sounds like your phones. When the hub has no voice engine it can reach,
  your browser reads the message with its own voice, and the button's tooltip
  says so.
- **What is read.** Formatting, code blocks and links are left out, and record
  ids are read by their three-word names ([[manual-names]]).
- **Play on a phone.** Press and hold the button (or right-click it) to play
  the message on a device with `voice.speak`, such as your phone, in the same
  voice. Devices listed in **Protected devices** (`role.protected_workers`)
  are never offered, and none are offered until that list exists.
  **Play on devices** (`huddle.speak_devices`) narrows the list to the
  workers you name.
