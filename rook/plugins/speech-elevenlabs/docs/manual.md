# ElevenLabs voices

A hosted `voice.tts` provider: spoken replies in ElevenLabs voices, with an
API key. Text goes to ElevenLabs only when this plugin speaks: when it is the
chosen `voice.tts` provider (the hub config's `contracts:`), or when a
session's voice is one of its voices. Each line spends ElevenLabs
characters.

## Set up

Make an API key in your ElevenLabs profile, store it in the vault and set
`speech-elevenlabs.key` to the vault name (`secret.set {name: elevenlabs, value: ...}`,
then `elevenlabs`).

- `voices`: the voices to offer, `Name=voice_id` comma separated; blank: the
  account's voices (up to 40). Each is `eleven_<name>`, as in 0.4.
- `default`: the voice a blank voice speaks in (blank: the first).
- `model` (`eleven_flash_v2_5`, the fastest), `url`, `timeout` (10 s).

Audio comes back as a 24 kHz mono WAV, like the speech engine's. When it
fails, the speech plugin speaks the line in the speech engine's default
voice instead.
