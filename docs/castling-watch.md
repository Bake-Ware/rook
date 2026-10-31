# The watch on Rook 0.5

Castling wave 6. The Wear OS app (`android/wear`) is a microphone and a
speaker: it streams its mic to the Rook app on the paired phone over the Wear
Data Layer (`android/wearlink`, path `/rook/voice`) and plays what comes back.
The phone does the network work in `WatchBridge`. That split stays in 0.5, so
**the watch app and the link protocol didn't change**. The phone picks the
voice session behind the bridge with its own 0.4/0.5 switch (Settings > Band >
Rook version, docs/castling-phone.md).

| | 0.4 (default) | 0.5 |
|---|---|---|
| Session | `VoiceClient`: the 0.4 voice server's WebSocket (Settings > App's voice address and key) | `WatchSpeech`: the hub's `speech` plugin (rook/plugins/speech, docs/castling-speech.md) |
| Reaching it | the voice server directly | the hub's HTTP door, `POST /call` as the person (`HubDoor`), at the address the phone signed in with |
| Who it acts as | the voice key's account | the person whose token is in Settings > Band > "Your hub token". Every call has a why and is journaled |
| Audio up | 20 ms frames streamed; the server finds turns | the phone's VAD (`SpeechDetector`) cuts utterances (`Segmenter`); each is one `speech.hear` (L16 16 kHz, 20 s at most: the door takes 1 MiB) |
| Replies | streamed PCM | System One's clips come with `speech.hear`; the agent's (System Two) arrive through a `speech.read` long poll, and their audio is fetched with `speech.read {after: seq-1, limit: 1, audio: true}`. WAV clips go to the watch in 200 ms pieces, at most 2 s ahead of real time |
| History on the watch | the phone app's chat (`VoiceBus`) | the session's own turns. The phone keeps one session for the watch (per hub address) and returns to it, so a reply that lands after the watch hung up shows next time. The phone's 0.4 chat is left alone |
| Rook icon (status) | the server's state | `listening`, `thinking` (an utterance in `speech.hear`, a reply waiting for its audio, or `status: thinking` until the agent answers; after 3 min without an answer it stops counting) and `speaking` while a reply plays. A `status: error` shows as a note |
| Stop (tap) | the server stops the reply | local: playback is flushed, and later replies are shown but not spoken until the person talks again. The agent still finishes its turn on the hub |
| Barge-in | the server's echo guard | none: half duplex. The mic is ignored while a reply plays and for 0.4 s after it, because a watch speaker would talk over itself. Tap to stop |

Code: `android/app/src/main/java/systems/bake/rook/` `WatchSpeech.kt` (the
session), `HubDoor.kt` (door client; plain http only to the phone itself),
`SpeechAudio.kt` (`Segmenter`, `Wav`), the `v05` branches in `WatchBridge.kt`,
the token field in Settings > Band (`Castling.TOKEN_PREF`).

## Tests

- `app/src/test/.../WatchSpeechTest.kt`: a scripted hub. It covers a new
  session, coming back to a kept session with its history, a closed session
  replaced, System One's reply spoken, the agent's reply fetched with its
  audio (System One's not twice), a tap muting until the person speaks, the
  mic ignored while a reply plays, and a lost hub ending the conversation.
  `HubDoorTest.kt` covers the door's request, refusals, holds, 401 and
  https-only. `SpeechAudioTest.kt` covers utterance cutting and WAV parsing.
- `WatchSpeechLiveTest.kt`: the same client through a real hub's door with the
  real `speech` and `agents` plugins and fake engine, talker and brain. It is
  skipped unless `ROOK_LIVE_DOOR` and `ROOK_LIVE_TOKEN` are set.
  `android/live_speech_hub.py` sets them:

  ```sh
  # Rook test venv (rook-pytest's) first on PATH; from the repo root
  python android/live_speech_hub.py ./gradlew :app:testDebugUnitTest --rerun --tests '*WatchSpeechLiveTest*'
  ```

## Not done yet

- **Token sign-in.** The person pastes a door token, as the desktop app does.
  The device's own 0.5 enrollment doesn't hand out a person token
  (docs/castling-desktop.md, "Token, not a new sign-in flow"). It is stored in
  the app's private preferences, like the 0.4 voice key.
- **The phone's own voice, Talk widget and Chat widget** still use the 0.4
  voice server in both modes. `WatchSpeech` is the piece they could share.
- No streaming partials, voice barge-in, conversation modes or voice picker
  (speech has `speech.voices`; the session uses the person's `speech.voice`
  setting). `docs/castling-speech.md` lists what speech itself hasn't ported.
- Device checks. Both APKs build (`:app:assembleDebug`, `:wear:assembleDebug`),
  but nothing has been installed or published. Bake's watch install is its own
  task.
