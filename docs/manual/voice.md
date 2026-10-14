---
id: voice
title: Voice assistant services
order: 20
features:
  - settings/voice
  - settings/decision
since: 0.1.0
deprecated: false
---
# Voice assistant services

The phone app's voice assistant ([[manual-phones-and-tablets]]) talks to a
**voice service** that runs next to your hub. It turns speech into text,
passes it to a language model that can use Rook, and turns the answer back
into speech. An optional **decision engine** decides, moment to moment,
whether speech near the phone was meant for the assistant.

These are separate programs (in `services/` in the Rook source). They fetch
their settings from the hub, so you set them up on the Settings page:

- **Voice**: Access (address, port and the token the app uses), Recognition
  (the speech model and how sensitive it is), Speech (the voice, the
  assistant's name, the wake word) and Agent (the language model and the Rook
  agent token it uses).
- **Decision engine**: where the engine runs, the assistant's names, timings,
  and how long raw recordings are kept.

## Spoken replies

When an agent asks you something through the phone, it can wait for an answer.
After a short beep the phone listens; what you say goes back to the agent as
text. Anything near the phone can be heard, so agents treat a spoken reply as
what the microphone heard, not as a confirmed order.
