# Speech engine

The voice service (services/voice) as a worker on the band: Whisper speech
recognition, Kokoro and Chatterbox voices, Smart Turn end-of-turn detection
and TitaNet speaker ID, on a machine with the models (a GPU helps). It
provides `voice.stt`, `voice.tts`, `voice.turn` and `voice.speaker`, which
the hub's speech plugin uses.

This manifest is builtin: the voice service implements its caps in its own
process and announces it; the hub never pushes it.

## Run it

On the voice machine, from the repository, in the voice service's
environment (`pip install -r services/voice/requirements.txt`, plus the
worker's own dependencies: `pip install -e .`):

```sh
python -m rook.core2.worker --config worker.yaml enroll https://your-hub   # once
python -m services.voice.band --config worker.yaml
```

It reads the same settings as the standalone server (`VOICE_MODEL_DIR`,
`WHISPER_MODEL`/`WHISPER_DEVICE`/`WHISPER_COMPUTE`, `VOICE_CHATTERBOX_*`,
`VOICE_TTS_DEFAULT`, `VOICE_SPEAKER_MODEL`/`VOICE_SPEAKER_DB`; see
services/voice/README.md). Speaker ID is on only with `VOICE_SPEAKER_MODEL`.
The standalone server (`python -m services.voice.server`) can keep running
beside it for 0.4 clients.

## The caps

- `stt {audio, language?}`: `{text, language, reason?}`; quiet or
  unintelligible audio is an empty text with the reason.
- `tts {text, voice?}`: a WAV; a Chatterbox failure speaks in the Kokoro
  default.
- `turn {audio}`: `{complete}`, Smart Turn on the last 8 s.
- `speaker {audio}`: `{label, score, candidate}`: an enrolled name, `guest`
  or `assistant` (its own voice, the echo guard). Advisory; the voiceprint
  never leaves the machine. Enrollment stays in the voice service for now.
- `voices`, `status`.

Audio in is `{data: base64, mime}`: `audio/L16` (16 kHz mono 16-bit) or a
mono 16-bit 16 kHz WAV, at most 30 s.
