# Speech

Talk to your agents by voice. Speech is the hub's voice assistant service
(it was `voice.*` in 0.4): it keeps voice sessions, puts a fast front talker
(System One) in front of an agent's own brain (System Two), and speaks the
replies. Voice is a motor skill: any agent can be talked to this way; speech
decides nothing itself.

## What it needs

- Something that provides `voice.stt` and `voice.tts`: the voice service
  worker (`speech-engine`, on a machine with the models) or a hosted voice
  (`speech-elevenlabs`, text to speech only). `voice.turn` (end of turn) and
  `voice.speaker` (who is speaking) are used when something provides them.
- The agents plugin (the agent a session talks to).
- An `llm.complete` provider for System One (`llm-anthropic`, `llm-openai`,
  `llm-cli`). Without one, every turn goes straight to the agent.

## A session

1. `speech.open {agent?, voice?, device?}` gives a `stream` id. The agent is
   the `speech.agent` setting unless you name one (default `house`).
2. `speech.hear {stream, audio}` for each utterance: audio is a file map
   `{data: base64, mime}`, `audio/L16` (16 kHz mono 16-bit PCM) or a mono
   16 kHz WAV, at most 30 s. Typed text (`text`) works too. The answer says
   what was `heard`, the `speaker` (advisory) and System One's reply in
   `say` (`text` plus its `audio`: WAV clips, a sentence or a few each).
   `complete: false` means the person only paused: send the rest;
   `final: true` skips that check (push-to-talk released).
3. When System One hands off (`handoff: true`), the agent answers in the
   background, as you, in your own session with it (the same one you type
   in with `agents.chat.send`). `speech.read {stream, after, wait, audio:
   true}` long-polls the session; each reply comes with its audio clips.
4. `speech.close {stream}` when done; the history stays.

Session events: `turn` `{role: prompt, text, speaker}` for what you said,
`turn` `{role: reply, text, system: one|two}` for replies, `status`
`{state: listening|thinking|idle|error}`. The latest speaker ID result is
also on the session (`attrs.speaker`).

## Settings

- `agent` (house): the agent sessions talk to by default.
- `talker` (on): System One answers greetings and small talk itself and
  hands everything else to the agent. Off: every turn goes to the agent.
- `talker_model` (`one`): System One's model, a model id or a system name.
- `voice` (per person, blank): the voice your sessions speak in.
- `history` (12): how many recent turns System One sees.

A voice that fails (a hosted service down) speaks in the speech engine's
default voice. Speaker ID never grants anything: it is shown to System One
and kept on the session, nothing more.

`speech.status` lists your open sessions and the speech engine's state;
`speech.voices` the voices on offer.
