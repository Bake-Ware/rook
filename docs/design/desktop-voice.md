# Desktop voice: push-to-talk and wake word

Status: v0.2 of the desktop app (desktop/). Push-to-talk and answers read
aloud ship in the default build; the wake word ships behind the `wake-word`
cargo feature and a setting that is off by default.

## Why voice on the desktop

Bake: "Voice agent with wake word via desktop app, the android implementation
has been problematic given androids greedy mic quirks." On Android, Rook's
VoiceService has to hold `AudioRecord` for the wake word and then yield it to
calls, recorders and the Assistant (MicArbiter, MicYieldPolicy; see
docs/android-microphone-0.4.6.md). Desktop operating systems share the
microphone between applications, so the desktop app opens the default input
and keeps it while listening, with no arbitration.

## Pipeline (v0.1)

```
mic (cpal, device rate, any channels)
  -> downmix + linear resample -> 16 kHz mono int16
  -> push-to-talk: buffer while the shortcut is held (max 30 s)
     wake word:    openWakeWord pipeline -> on wake, record until a pause
  -> POST <voice server>/api/transcribe   (audio/L16, bearer voice key)
  -> text -> quick ask (home agent: chat room, or home.ask over MCP)
  -> answer shown in the quick-ask box
```

- **Transcription** reuses the voice server's `/api/transcribe`, the
  endpoint the phone uses for spoken replies (services/voice/transcribe_api.py):
  same voice key and guest rules, 30 s cap, Whisper already loaded there.
- **The question goes to the home agent**, like a typed quick ask, not to the
  voice server's planner. One place answers questions from the desktop, and
  the conversation lands in the same chat room as typed ones.
- **End of speech** (wake word mode) is a loudness endpointer
  (`voice::Endpointer`): 30 ms frames against a running noise floor; done
  after 0.9 s of quiet following at least 0.24 s of speech; gives up after
  5 s of nothing; caps at 15 s.

## Wake word

- **Engine: openWakeWord** (code Apache-2.0), the same models and streaming
  pipeline as the Android app's `WakeWordDetector`, ported to Rust:
  melspectrogram over the last 1,760 samples every 80 ms, 76-frame windows
  into the 96-d embedding model, the last 16 embeddings into the wake
  classifier, fire on two consecutive scores at or above the threshold while
  someone is speaking.
- **Runtime: tract** (MIT/Apache-2.0), pure Rust ONNX. No onnxruntime shared
  library to ship per platform, nothing downloaded at build time. Tests check
  tract's mel and embedding outputs against onnxruntime 1.31 on fixed inputs.
- **Models and licences:**
  - `melspectrogram.onnx`, `embedding_model.onnx`: openWakeWord's shared
    feature models (the embedding model is Google's speech_embedding,
    Apache-2.0). Bundled, from android/app/src/main/assets/wakeword/.
  - the wake classifier (`hey_rook.onnx`, ...): **not bundled.**
    openWakeWord's pre-trained classifiers are CC BY-NC-SA 4.0
    (non-commercial), so, as on Android, the user supplies their own
    (trained with openWakeWord's notebook, or the private model the phone
    build uses).
- **Speech gate:** the phone gates on Silero VAD; v0.1 uses a loudness gate
  (chunk RMS) to stay dependency-free. Silero (MIT, already in the Android
  assets) can run on tract too if false wakes in noise turn out to matter.
- **Options considered:** Porcupine (accurate, but proprietary with per-user
  keys: no); Vosk/whisper keyword spotting (heavy for always-on); Rustpotter
  (Apache-2.0, pure Rust, but its own model format: a second wake model to
  train and keep, where openWakeWord matches the phone).

## Not yet (and how)

1. ~~**Spoken answers.**~~ Done in 0.2: the answer goes to the voice
   server's `/api/voice` in pieces of at most 700 characters (sentence ends,
   markup and links dropped, 2,400 characters at most), each WAV played
   through cpal on the default output, resampled to its rate. Pressing
   push-to-talk or Esc stops it. Setting `speak_answers`: `voice` (after a
   spoken question, the default), `always`, `never`.
2. **The full voice agent.** Stream to the voice server's `/ws` with the
   protocol-2 hello (conversation id, `mode`, `device` = this worker's name),
   send PCM frames, play the TTS frames it returns, handle barge-in. That
   makes the desktop a peer of the phone (tools, timers, background jobs),
   at the cost of echo handling (`aec`) on speakers.
3. **Wayland hotkeys.** Global shortcuts need X11 on Linux; the XDG
   GlobalShortcuts portal is the Wayland route.
4. **Voice settings from the hub.** The hub's `voice.*` settings could hand
   the app the voice URL and a per-device key instead of typing them.
