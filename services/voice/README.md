# Rook voice service

Version 2 replaces the unversioned `voice-agent/server.py` orchestration on the GPU host.
The model servers and Rook capabilities remain in use. Pipecat's pinned
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
* `VOICE_TOOL_REASONING_EFFORT`: thinking effort for every external tool job,
  default `high`. Mouthpiece replies explicitly use `reasoning_effort=none`.
* `VOICE_TOOL_MAX_TOKENS`, `VOICE_TOOL_MAX_STEPS`: per-generation budget (4096)
  and tool-loop bound (24). `VOICE_TOOL_MODEL_TIMEOUT_S` defaults to 180 seconds.
* `VOICE_IDENTITIES_FILE`: JSON mapping SHA-256 credential digests to principal,
  worker and owner fields. Writes and unrestricted hub tools require owner access.
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
Its HTTP client is reused across turns and closed at service shutdown.
It announces work only after queuing a job. Invalid plans are retried once with the rejected output in context before any
work starts. If both attempts were rejected and either one answered in plain
prose (no tool call, not JSON, at most 1,200 characters), that prose is spoken
as a `respond` reply; it can never start a job. Otherwise the fixed
clarification fallback is spoken. External jobs are never implicitly
retried. Raw rejected outputs go to mode-0600 `rejected-voice-plans.jsonl` beneath
`VOICE_MODEL_DIR` (1 MB plus two rotations); these private diagnostics can contain
conversation-derived data. Owner jobs preserve the actual (scrubbed) failure message. Rook worker
names refresh every 60 seconds for planner context and validation; unknown names
fail before a capability call.

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
live. A missing mode is `assistant`, the unchanged agent behaviour (a custom
prompt there is appended as extra style instructions). An unknown mode never
falls back: the hello gets an `error` with `code: "unknown_mode"` and the socket
closes (4400); a bad live switch gets the same error and the mode is unchanged.
The `session` event echoes `mode`; the APK and browser compare it with what they
asked for and disconnect with a warning on a mismatch, so a client asking an old
server for `conversation` never silently gets the full assistant. Other modes
replace the agent prompt with a spoken-conversation prompt and offer no tools
except `end_session`; the runtime refuses any other tool call. `dictate` never
calls the model: segments go to their own `dictation` table (not the trimmed
event history, never model-visible, capped at 200,000 characters; past that new
speech is refused, old text is never dropped), read-backs are not recorded as
history, and "read it back", "I'm done", "scratch that" and "start over" read,
emit (`dictation` event; "I'm done" then clears it for the next dictation), trim
or clear it. Mode prompts are client text: capped at 2,000 characters with
control characters removed, and they never grant tools. Job reports wait until
the session is back in `assistant` mode. The APK and browser keep a separate
conversation per non-assistant mode; the browser reconnects on a mode change
rather than switching the live session, so no mode inherits another's history.

Jobs are independent of audio turns and survive socket closure. Owner jobs have
10-minute limits in production, including single lookups. Every external lookup
and escalation runs through the same Qwen agent with thinking enabled, but only
an owner's `escalate` gets the full toolset (`rook_call`, `rook_mcp`). Lookup jobs
(`web_search`, `rook_read`, `rook_devices`) read untrusted text, so their loop gets
read-only tools only (`web_search`, identity-checked `rook_read`/`rook_describe`/
`rook_devices`, `finish`). Non-owner jobs time out after 60 seconds, and at most 2
per conversation and 8 in total run at once. Failures are reported to owners with
the scrubbed, truncated error text; other keys hear only "That didn't work."
Outcomes are persisted,
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

## Voices: Kokoro and Chatterbox

The server speaks with two TTS engines, both selectable per session:

* **Kokoro** (kokoro-onnx, CPU) is always loaded. `VOICE_TTS_THREADS` sets its
  ONNX threads.
* **Chatterbox Turbo** (Resemble AI, ~350M parameters, GPU, MIT) is loaded when
  `VOICE_CHATTERBOX_DEVICE` is set (for example `cuda:0`). It runs in its own
  Python so torch never enters the voice venv: `VOICE_CHATTERBOX_PYTHON` points
  at a venv with `chatterbox-tts` installed, and the server starts
  `chatterbox_worker.py` with it. Without `VOICE_CHATTERBOX_PYTHON` the worker
  code is imported in-process (only if the voice venv itself has chatterbox).
  `VOICE_CHATTERBOX_VOICES_DIR` (optional): each `<name>.wav` there (5-20 s of
  clean speech) becomes the voice `chatterbox:<name>`; the built-in voice is
  `chatterbox:default`. If Chatterbox does not load, the server logs why, leaves
  it out of `/voices` and runs with Kokoro alone.

Voice ids are engine-namespaced: `kokoro:af_heart`, `chatterbox:default`. A bare
id such as `af_heart` is a legacy Kokoro id and still works everywhere (`voice`
messages, `/api/voice`, `VOICE`, `VOICE_TTS_DEFAULT`). `GET /voices` returns
`voices` (ids), `default`, `catalog` (`id`, `engine`, `name`, `label`) and
`engines` (availability, load error). The default is `VOICE_TTS_DEFAULT`, else
the saved `voice.state`, else `VOICE` (default `af_heart`); an unavailable
default becomes the Kokoro default. A `{"type":"voice","voice":"<id>"}` message
switches the session's voice; unknown ids are ignored.

Chatterbox renders sound tags such as `[laugh]`, `[chuckle]` and `[sigh]`; they
are stripped before Kokoro. When the session's voice is Chatterbox, Front's
prompt allows those three tags, sparingly. If Chatterbox fails for an utterance,
Kokoro (the default Kokoro voice) speaks it, and the session gets one `error`
event with `code: "tts_fallback"` plus an activity error. A crashed worker is
restarted at most once a minute. Chatterbox audio is resampled to Kokoro's
24 kHz, so clients see one format. Release candidates set
`VOICE_CHATTERBOX_DEVICE` empty and never load a second copy on the GPU.

Installing the Chatterbox venv (on the GPU host, outside the voice venv):

```sh
python3 -m venv /home/bake/voice-tts/chatterbox-turbo/.venv
/home/bake/voice-tts/chatterbox-turbo/.venv/bin/pip install chatterbox-tts
```

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
### Decision-engine shadow mode

`DECISION_URL` enables an advisory HTTP side channel, e.g.
`http://127.0.0.1:8910`. Unset/empty disables requests, feedback collection, and
decision events. `DECISION_TIMEOUT_MS` defaults to **150 ms**, including connection,
queueing and response parsing. A failed or slow engine never changes the reply,
tool selection, confirmation policy, or interruption behavior. Opt-in turns dispatch a bounded background task which waits for reply completion
before inference and persistence. This avoids competition for the mouthpiece GPU
and history SQLite writer; no inference is awaited by reply generation.
The voice process loads no additional model or CUDA allocation.

Only protocol-2 clients with the literal `"thinking": true` in hello receive
`decision` events; `session.thinking` echoes the opt-in. See the exact
[shared contract](../../CONTRACT-decision-event.md). One event at most is emitted
per external turn; it can arrive after the reply and retains the original turn
number. Typed turns omit `needs_response`. Clients without literal protocol-2
`thinking:true` create no shadow object, decision requests or feedback writes.
Feedback tables and engine metadata are initialized lazily at the first opt-in
hello; a service receiving only non-thinking clients performs no decision I/O. Internal job-completion narration does not create another decision.

The state is text (at most 8,000 characters) and factual context: voice/text
source, recent assistant speech, literal wake/name match, previous reply (1,000
characters), and whether playback was interrupted. It does not assert who was
addressed. `DECISION_RECENT_SPEECH_SECONDS=15` controls recency;
`DECISION_ASSISTANT_NAMES=rook,assistant` controls case-insensitive whole-word
matching. Recent speech is measured from audio sent on this connection, not
proof that a listener heard it; it resets on reconnect. Previous reply context
is recovered from that conversation's existing history on reconnect.

`feedback.py` adds two migration-safe tables to `VOICE_STATE_DB`:

* `decisions`: random decision ID, credential-scoped session, conversation UUID,
  connection-local turn, source, state JSON, normalized answers, engine metadata,
  cached `/info` version including adapter digest, status, latency, timestamps,
  parent decision ID and generated reply. IDs remain unique across reconnects.
* `decision_outcomes`: target decision ID, observing decision ID (when another
  utterance supplied the signal), kind, JSON value/provenance and timestamp.
  Unique target/observer/kind prevents duplicate observations.

Correction phrases and engine `is_correction > 0.5` point to the **previous
replied-to decision**, not the correction's own classification. Repeat signals
use normalized exact text within 15 seconds. Yes/no signals require a recognizable
confirmation prompt in the previous actual reply, not merely a high predicted
`needs_confirmation`. Reply interruption targets the reply being interrupted.
`no_followup` requires a completed reply and an open, quiet connection for
`DECISION_SILENCE_SECONDS=15` after playback drain. Input, interruption or
disconnect cancels that observation. Silence is **not approval**. These are weak
signals with provenance, not gold labels; no training happens here. In particular,
the engine's household calibration does not validate the new correction question
or these live context fields.

Database writes are buffered until the dispatched reply finishes, then batched
on a dedicated worker/connection with a bounded 256-operation backlog and 5 ms
SQLite lock timeout. Inference has a bounded per-connection task count.
Telemetry can be dropped under overload rather than blocking normal turns;
failed writes log exception types only. Raw state (including prior-reply context)
and reply text are cleared after `DECISION_RAW_RETENTION_DAYS=30`, at first opt-in initialization and
every minute thereafter. `secure_delete` is enabled on feedback writes. Numeric observations
remain; existing history/job retention remains seven days. Backup/export files
have their own retention obligations.

Export only retained examples with linked outcomes, with labels explicitly marked
`weak_outcome_signals` (no network or model calls):

```sh
umask 077
python -m services.voice.feedback --db /path/to/voice-state.sqlite3 > labeled.jsonl
PYTHONPATH=. python -m pytest -q tests/test_voice_runtime.py tests/test_voice_decision.py
DECISION_URL=http://127.0.0.1:8910 VOICE_SMOKE_URL=wss://127.0.0.1:8900/ws \
  python -m services.voice.decision_smoke --output shadow-smoke.json
```

Provide `VOICE_TOKEN` privately in the environment. For the existing self-signed
loopback TLS endpoint, `VOICE_SMOKE_INSECURE=1` is an explicit test-only override.
The smoke exercises both thinking settings, original turn IDs, late-event absence,
reply timings and the engine's voice lights example.

The decision engine is paused (docs/design/voice-front-background.md): leave
`DECISION_URL` empty. The code stays for later; with it empty there is no
engine traffic, no feedback tables and `decision` events report `disabled`.

### Qwen thinking and vision

The mouthpiece selects structured replies, reads or `escalate` with thinking off.
`escalate` activates the voice agent's own native tool loop; it does not connect
or hand off to Hermes. Persisted `delegate_to_hermes` jobs use this same loop for
compatibility. The agent discovers exact worker and hub schemas, executes tools
sequentially, records progress and journal IDs, and stops on uncertain writes.
Images received through protocol-2 `image` messages remain available in native
image content when a task escalates. The Qwen backend must advertise image input.
The reference host uses a CPU vision encoder and a 32,768-token model context.

## Per-turn timing

Every turn logs one JSON line on stderr (logger `voice.timing`, so it reaches
the journal at the service's warning log level; `VOICE_TIMING_LOG=0` disables
it) and puts the same object in the activity `done` event as `timing`:

```json
{"event":"voice_turn_timing","turn":4,"status":"ok","source":"voice","endpoint_ms":640,
 "stt_ms":180,"plan_ms":2310,"llm_ms":1480,"first_text_ms":2312,"first_audio_ms":2690,"total_ms":3900}
```

All `*_ms` except `stt_ms` and `llm_ms` are measured from the end of speech
(the last voiced microphone frame; typed and internal turns from the turn
start). `endpoint_ms` is the end-of-utterance wait, `stt_ms` the transcription,
`plan_ms` when the planner's choice was known, `llm_ms` the planner call itself,
`first_text_ms`/`first_audio_ms` the first reply text and first audio packet,
`total_ms` the end of the turn. Missing keys mean the stage did not happen
(typed, unspoken or tool-only turns). No transcript or reply text is logged.

In the `front_background` pipeline the line is written once Background (and
its follow-up) finished, and adds `front_first_token_ms`,
`front_first_audio_ms`, `background_ms` and `followup_ms` (first follow-up
audio); the same object is in the Background `done` event.

## Front/Background pipeline (opt-in)

Design: [docs/design/voice-front-background.md](../../docs/design/voice-front-background.md).
A connection opts in with hello `"pipeline": "front_background"` (protocol 2);
anything else, or no field, keeps the classic planner path unchanged. Code:
`pipeline.py` (orchestration, Background agent, `background` events),
`front.py` (prompt layout and streaming), `board.py`, `policy.py` (tool table),
`bgtools.py` (timers, weather, calendar, mail, Rook tasks, music, Home Assistant).

* Front: one streaming completion per turn to `VOICE_FRONT_URL`/`VOICE_FRONT_MODEL`
  (default `VLLM_URL`/`VLLM_MODEL`), no tools, thinking off. Prompt = fixed block (persona, rules, mode) + board + last
  `VOICE_FRONT_TURNS` (6) exchanges + utterance. `VOICE_FRONT_MAX_TOKENS` (200),
  `VOICE_FRONT_TIMEOUT_S` (30).
* Background: the thinking agent's tool loop on `VOICE_BACKGROUND_URL`/
  `VOICE_BACKGROUND_MODEL` (default `VLLM_*`) with only the policy tools, effort
  `VOICE_BACKGROUND_EFFORT` (`low`), `VOICE_BACKGROUND_MAX_STEPS` (8),
  `VOICE_BACKGROUND_TIMEOUT_S` (120). It sees only the user's own lines and
  trusted board facts; after any tool that returns outside text (web, mail,
  calendar, device or hub reads) the rest of that run has no acting tools.
  Follow-ups wait up to `VOICE_FOLLOWUP_WAIT_S` (90) for Front to finish.
* Pin Front and Background to separate model instances (e.g. one llmanifold
  model per GPU) so Front never queues behind Background and each side keeps a
  warm prompt cache. The classic path and the classic thinking agent keep `VLLM_*`.
* Prefetch (session start, wake, speech onset when older than
  `VOICE_PREFETCH_EVERY_S` (120)): clock, caller device battery, timers, owner
  tasks; bounded by `VOICE_PREFETCH_TIMEOUT_S` (4).
* `background` events go only to owner keys whose hello had `background: true`;
  `timer` events and timer tools need hello `timers: true`.
* Weather: Open-Meteo, device location when the key may read its device, else
  `VOICE_HOME_LAT`/`VOICE_HOME_LON`; `VOICE_WEATHER_UNITS` (`celsius` or
  `fahrenheit`), `VOICE_TZ` (IANA zone for "at 7pm" timers; default host zone).
* Music: `VOICE_PIANOBAR_WORKER` (worker holding `cmd.pianobar-*`).
* Home Assistant: `VOICE_HASS_URL`, `VOICE_HASS_TOKEN` (from the secret store
  at deploy time, never in a file in Git), `VOICE_HASS_VERIFY_TLS=0` for a
  self-signed certificate. Lights, switches, scenes and media players only;
  services turn_on/turn_off/toggle, scene activate, media play/pause/next.

## Deployment

See [deploy/README.md](deploy/README.md): releases are built from a commit,
smoke-tested as a loopback candidate beside the live unit, switched with one
systemd drop-in and rolled back with one command.
