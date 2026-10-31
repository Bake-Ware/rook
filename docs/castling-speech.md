# Castling 0.5: speech

The 0.4 voice service on the 0.5 band. Bake's naming (knowledge
castling-naming-decisions, B10): the hub's voice assistant service is
**speech**; **voice** is the speech contracts (and a device speaking aloud).
Design: castling-agents (voice is a motor skill; System One talks fast,
System Two is the agent's brain), castling-streams (a voice session is a
stream), castling-inference (System One is model `one`).

## What runs where

| Piece | Where | What |
|---|---|---|
| `speech` (rook/plugins/speech) | hub | voice sessions, System One, hand-off to the agent, spoken replies |
| `speech-engine` (rook/plugins/speech-engine, builtin) | the voice machine (kaiju) | Whisper, Kokoro/Chatterbox, Smart Turn, TitaNet: `voice.stt/tts/turn/speaker` |
| `speech-elevenlabs` (rook/plugins/speech-elevenlabs) | hub | hosted `voice.tts` (opt-in) |
| `agents` | hub | System Two: the agent answers in the person's own session |
| an `llm.complete` provider | hub or worker | System One (model `one`: Haiku by default with Claude) |

Core's contracts (rook/core2/builtin/core.yaml) gained `voice.turn` and
`voice.speaker` beside `voice.tts` and `voice.stt`: the four parts of the
motor skill. All four route to the chosen provider, so a hosted STT or a
second engine is one more plugin.

`speech-engine` is a `builtin: true` manifest: the voice service implements
its caps in its own process. `python -m services.voice.band --config
worker.yaml` is an ordinary 0.5 worker (enrolled the usual way) that also
announces speech-engine with handlers over the service's loaded models
(services/voice/band.py; `Worker(cfg, builtins=[...])` is the hook). The
hub holds the manifest in its library, so it knows it by hash. The models,
the Chatterbox subprocess and the speaker store are the 0.4 service's, with
the same environment. Nothing on kaiju changes until someone runs it; the
standalone 0.4 server keeps serving 0.4 clients.

## One utterance

`speech.hear {stream, audio}`:

1. `voice.stt`, `voice.speaker` and `voice.turn` run at once on the audio
   (the last two are optional: nothing provides them, they're skipped).
2. Turn says the person only paused: the text waits on the session
   (`attrs.pending`, a `listening` status) for the rest. `final: true` (push
   to talk released) skips the check.
3. The speaker result goes on the session (`attrs.speaker`) and on the
   person's `turn` event. It is advisory: System One sees it in its prompt,
   nothing is granted by it. The voiceprint never leaves the engine.
4. System One (`llm.complete`, `speech.talker_model`, default `one`) answers
   with `{"say", "handoff"}`: greetings and small talk it says itself;
   anything else gets a short acknowledgement and goes to the agent. No
   provider, `talker: false` or an unreadable answer: everything goes to
   the agent.
5. System Two: `agents.chat.send` as the person, so the request lands in
   their persistent session with that agent (the one they type in) and runs
   with their rights (the intersection rule). It runs in the background; the
   reply is a `turn {role: reply, system: two}` event.

Replies are spoken with `voice.tts` in the session's voice, in clips of a
few sentences (Chatterbox stops at about 30 s); a failing voice (a hosted
service down) speaks in the engine's default. System One's clips come back
in the `hear` answer; the agent's with `speech.read {audio: true}`.

## What the phone and watch apps need

The 0.4 apps stream PCM over the voice WebSocket (`/voice/ws`, protocol 2)
to the standalone server. On 0.5 they talk to the hub's door instead:

- **A person's token** for the hub door (the app signs in at enrollment,
  spec 12) and calls `/call` with `worker: rook`, a `why`, and the cap.
- **Utterances, not a stream.** Keep the on-device Silero VAD
  (SpeechDetector.kt) and end an utterance on its silence; send it as
  `speech.hear {stream, audio: {data: base64 L16 16 kHz, mime: "audio/L16"}}`
  (at most 30 s). `complete: false` means keep listening and send the next
  part. Push-to-talk sends `final: true`.
- **Play `say.audio` at once** (WAV clips in order), then long-poll
  `speech.read {stream, after, wait: 25, audio: true}` for the agent's reply
  and `status` events (show `thinking`; `error` says why).
- **Barge-in is local:** speech starting during playback stops playback
  (there's no server-side reply stream to interrupt); what was said goes in
  as the next utterance.
- `speech.open` once per conversation (keep the stream id across
  reconnects; `device` = the phone's worker name), `speech.close` at the
  end. `speech.voices` feeds the voice picker; the choice is the person's
  `speech.voice` setting or `voice` on open.
- **Watch:** as today, the watch hands its audio to the phone
  (WatchVoiceListener.kt) and the phone makes the calls; a watch with its
  own network could call the door itself with its own token.
- Wake word, echo cancellation and the mic stay on the device. `voice.speak`
  (the phone speaking aloud for an agent outside a session) is unchanged.

## Not ported yet

- Streaming: partial transcripts while speaking, and a reply spoken clause
  by clause while the model is still writing (0.4's front talker streams).
- Conversation modes (dictate, brainstorm, roleplay, listen).
- Speaker enrollment caps (enroll, forget); enrollment stays in the 0.4
  service, whose store the engine reads.
- Voice keys and the voice admin page: the hub's people and tokens replace
  them.
- Core's guest preset grants no `speech.*` (as with `agent.backend` for
  agents): Bake decides whether guests may talk.
- Sessions live in the plugin's own stream store until the hub keeps one
  stream index (TODO castling:streams), as the agents plugin's do.

## Tests

`tests/plugins/test_speech.py` (a real hub, fake engine, fake System One,
the agents plugin over a fake backend), `test_speech_engine.py` (the band
adapter and audio handling), `test_speech_elevenlabs.py` (a fake ElevenLabs
API) and `tests/e2e/test_speech_e2e.py` (the voice service's worker as a real
process over Telesthete, fake models).
