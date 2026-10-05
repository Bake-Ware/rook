# Rook voice service

Version 2 replaces the unversioned `voice-agent/server.py` orchestration on the GPU host.
The model servers and Rook/Hermes capabilities remain in use. Pipecat's pinned
Smart Turn v3.2 model and feature extractor provide local turn completion;
Rook owns the transport, session and background-job lifecycle. The full Pipecat
transport framework is not a runtime dependency.

Run from the repository root with Python 3.12:

```sh
pip install -r services/voice/requirements.txt
python -m services.voice.server
```

Supply `VOICE_MODEL_DIR` containing `kokoro-v1.0.onnx`, `voices-v1.0.bin`, and
`smart-turn-v3.2-cpu.onnx`. The Smart Turn URL and digest are in `models.json`.
Existing Whisper configuration (`WHISPER_MODEL`, `WHISPER_DEVICE`,
`WHISPER_COMPUTE`) and `VLLM_URL`/`VLLM_MODEL` continue to work. Keep model weights
outside Git. The existing browser UI can live in `VOICE_MODEL_DIR/static`.

Runtime configuration:

* `VOICE_TOKEN`: bearer credential. No hardcoded credential or implicit public
  access. LAN deployments must explicitly set `VOICE_ALLOW_ANONYMOUS=1` if they
  intentionally have no credential. Keyless guests (and unprivileged keys) get
  conversation and web search only: no band device listing, no device reads and
  no agent. A key mapped to a device may read only that device; owner keys keep
  full access.
* `ROOK_MCP_URL`, `ROOK_MCP_TOKEN`: capability endpoint and runtime credential.
* `ACP_HOST`, `ACP_PORT`: Hermes ACP endpoint. `ACP_AUTO_APPROVE=1` preserves the
  existing unattended permission-choice policy; set `0` to decline ACP permission
  requests. Prompts are never automatically retried after transport failure.
* `VOICE_STATE_DB`: private SQLite file; defaults beneath `VOICE_MODEL_DIR`.
* `VOICE_BIND`, `VOICE_PORT`: default loopback port 8900.
* `VOICE_TLS_KEY`, `VOICE_TLS_CERT`: existing PEM paths when serving TLS directly.
* `ROOK_VOICE_ASSISTANT_NAME`: the name the assistant introduces itself with in
  its system prompt; defaults to `Rook`.
* `ROOK_VOICE_OWNER`: optional owner name used in the system prompt and tool
  descriptions ("Alex's personal voice assistant", "Alex's Rook band"). Empty
  (the default) uses neutral phrasing ("the user's").
  Both are also the hub settings `voice.assistant_name` / `voice.owner`
  (served by `settings.fetch("voice")`). Left blank there, the hub fills them
  from the persona assigned to family `voice` (per user from a user-scoped
  persona): see docs/design/persona.md. This environment still wins.

The mouthpiece uses structured selection of either a reply or a real tool.
It announces work only after queuing a job. Malformed plans may be retried once
before any work starts; external jobs are never implicitly retried.

The APK sends a protocol-2 hello with an opaque persisted conversation UUID.
History is scoped to the credential and conversation. A second simultaneous
connection to the same conversation is rejected. Reconnects preserve history;
credential or server changes use a separate conversation. Recent context is
bounded to 40,000 serialized characters and complete tool pairs, with at most
160 stored events per conversation. This is finite conversational memory, not
an unlimited personal memory store. Old records are pruned on startup after
seven days. Interrupted generated speech is labelled as possibly unheard.
Playback acknowledgements provide frame counts, not word-aligned timestamps.

Conversation modes (`services/voice/modes.py`, `GET /modes`): the hello may carry
`mode` (`assistant`, `conversation`, `dictate`, `brainstorm`, `roleplay`, `listen`)
and an optional `mode_prompt`; `{"type":"mode","mode":...,"prompt":...}` switches
live. A missing or unknown mode is `assistant`, the unchanged agent behaviour (a
custom prompt there is appended as extra style instructions). Other modes replace
the agent prompt with a spoken-conversation prompt and offer no tools except
`end_session`; the runtime refuses any other tool call. `dictate` never calls the
model: speech is stored as dictation, outside model-visible history, and "read it
back", "I'm done", "scratch that" and "start over" read, emit (`dictation` event),
trim or clear it. Mode prompts are client text: capped at 2,000 characters with
control characters removed, and they never grant tools. Job reports wait until
the session is back in `assistant` mode. The APK and browser keep a separate
conversation per non-assistant mode.

Jobs are independent of audio turns and survive socket closure. Read tools have
45-second limits; Hermes jobs have 10-minute limits. Outcomes are persisted,
including failed, unknown and cancellation-requested states. After a service
restart, formerly running jobs become unknown and are not rerun. An audio stop
never silently cancels external work. `cancel_job` requires the job to belong to
the current conversation; requesting cancellation does not undo completed actions.

Protocol-2 PCM packets begin with `RK2A` and a big-endian 32-bit response ID.
Playback is paced and Android uses a bounded queue, with stale responses discarded
at interruption. `speech_start` confirms local speech during playback. Android
sends it only with an enabled platform echo canceller and sustained Silero speech.
A shorter candidate pauses playback and can resume after a false interruption.
Older clients keep the original PCM protocol and explicit interrupt control.

Android 0.4.2 adds a Settings button for the default assistant. Android 12+ uses
VoiceInteractionService; older Android uses the ACTION_ASSIST activity. Both
open the normal Rook conversation and permission flow. Rook does not advertise
itself as a general dictation provider or promise proprietary hardware hotword
support. Native assistant entry does not enable lock-screen access.

Validation:

```sh
PYTHONPATH=. .venv/bin/pytest -q tests/test_voice_runtime.py
# On a candidate host with runtime credentials in the environment:
VOICE_SMOKE_URL=ws://127.0.0.1:8901/ws python -m services.voice.smoke
```

The smoke uses a synthetic conversation and a read-only `info.uptime` lookup
on the worker named by `VOICE_SMOKE_WORKER` (default `gpu-box`).
Android instrumentation mode `voice` checks silence/noise and the bundled
synthetic speech fixture. Physical-phone speaker, headset and Bluetooth acoustic
checks remain necessary; emulator success does not establish real-room false
wake rates or barge-in latency.

## Voice admin and keys

The bundled browser client is served when `VOICE_MODEL_DIR/static/index.html`
was not supplied. It includes an **Admin** link at `/admin`; a custom static
client can link to the same area. Admin login is separate from voice keys.
There are no built-in admin credentials. Initialize an account interactively:

```sh
python -m services.voice.admin --db /path/to/voice-admin.sqlite3 --username admin
```

Set `VOICE_ADMIN_DB` to the same database path when starting the voice service.
Its default is `VOICE_MODEL_DIR/voice-admin.sqlite3`. Passwords use scrypt; admin
cookies require HTTPS and expire after eight hours. The admin area creates,
edits and revokes voice keys, assigns a person and optional device, grants owner
access, and changes the admin password. New voice keys are shown once and stored
as digests. Revocation disconnects active sessions and requests cancellation of
running jobs; already completed external effects cannot be undone.

`VOICE_TOKEN`, when supplied, is imported as an owner key. Optional
`VOICE_IDENTITIES_FILE` imports an existing JSON dictionary keyed by SHA-256
credential digest, with `principal`, `worker` and `owner` fields. Revocation
persists in the admin database and overrides imported entries after restart.
Set `VOICE_TOKEN` empty and `VOICE_ALLOW_ANONYMOUS=1` to enable keyless chat.
Guest keys and keyless connections cannot read private phone data or delegate
unrestricted agent work. Device keys can read personal data from their mapped
device; owner keys can select other devices and start agent work.

## Speech speed and interruption

`VOICE_TTS_THREADS` controls Kokoro's ONNX CPU inference threads (default eight).
The bundled browser client uses protocol 2 and discards late audio from an
interrupted response. With browser echo cancellation enabled, sustained local
speech stops playback and sends `speech_start`; microphone preroll preserves the
start of the new utterance. Space or tapping the orb stops playback immediately
in all browsers. Automatic interruption still needs physical acoustic testing
with the intended microphone and speakers.

Additional checks (admin tests require the voice service dependencies):

```sh
pytest -q tests/test_voice_admin.py tests/test_voice_identity.py
node tests/test_voice_browser.cjs
```
