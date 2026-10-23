# Rook voice service

Version 2 replaces the unversioned `voice-agent/server.py` orchestration on the GPU host.
The model servers and Rook capabilities remain in use. Pipecat's pinned
Smart Turn v3.2 model and feature extractor provide local turn completion;
Rook owns the transport, session and background-job lifecycle. The full Pipecat
transport framework is not a runtime dependency.

A hub can serve this service itself, at `/voice/` on its own address, either
in the hub process or by forwarding to this server running on a GPU host: see
the voice hub plugin, [docs/operations/voice.md](../../docs/operations/voice.md).
Running it standalone, as below, is unchanged. Behind a hub on another
machine, set `FORWARDED_ALLOW_IPS=<the hub's address>` so the admin page's
sign-in limit counts each client, not the hub.

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
  access. `VOICE_ALLOW_ANONYMOUS=1` lets keyless clients in as guests, with or
  without keys configured; a supplied key that isn't valid is still refused. Keyless guests (and unprivileged keys) get
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
Android also sends `"barge": "confirm"` in hello. Only when the session event echoes
it, sustained speech sends `{"type":"speech_start","hold":true}` instead: playback
stays paused with its queue, the mic streams, and the server answers `interrupt`
(real speech) or `{"type":"resume","turn":n}` (nothing transcribed). With neither
within 10 s the phone drops the reply locally.
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
Set `VOICE_ALLOW_ANONYMOUS=1` to enable keyless (guest) chat alongside keys.
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
  clean speech) becomes the voice `<name>`; the built-in voice is `default`.
  A clip named like an existing Kokoro voice is skipped (logged). If Chatterbox does not load, the server logs why, leaves
  it out of `/voices` and runs with Kokoro alone.

Voice ids are bare names, unique across engines: `af_heart`, `sojourn`. The
older engine-namespaced form (`kokoro:af_heart`, `chatterbox:sojourn`) is still
accepted everywhere (`voice` messages, `/api/voice`, `VOICE`,
`VOICE_TTS_DEFAULT`) and resolves to the bare id. `GET /voices` returns
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
24 kHz, so clients see one format. Every utterance from either engine is lifted so its
speech sits at `VOICE_TTS_TARGET_DBFS` (default -20 dBFS RMS), at most +12 dB and
never past -1 dBFS peak; louder audio is left alone. Release candidates set
`VOICE_CHATTERBOX_DEVICE` empty and never load a second copy on the GPU.

Installing the Chatterbox venv (on the GPU host, outside the voice venv;
Python 3.11, torch 2.6 with CUDA 12.4 wheels):

```sh
git clone https://github.com/resemble-ai/chatterbox.git chatterbox-src
uv venv -p 3.11 chatterbox-turbo/.venv
uv pip install -p chatterbox-turbo/.venv/bin/python ./chatterbox-src
```

Environment for the voice service (the model downloads from Hugging Face on
first load, ~10 s to load once cached):

```sh
VOICE_CHATTERBOX_DEVICE=cuda:0
VOICE_CHATTERBOX_PYTHON=/path/to/chatterbox-turbo/.venv/bin/python
CUDA_DEVICE_ORDER=PCI_BUS_ID      # cuda:N numbered like nvidia-smi
# VOICE_TTS_DEFAULT=default   # optional; otherwise the Kokoro default stays
```

Measured on an RTX 3090 (Chatterbox Turbo, built-in voice): ~3.2-3.7 GiB of
VRAM, first audio for a one-sentence reply ~0.65 s (Kokoro on 8 CPU threads:
~0.43 s), real-time factor ~0.21 (Kokoro ~0.18). Chatterbox has no streaming
API; the server synthesizes clause by clause, so first audio is the first
clause's synthesis time. While it synthesizes it time-slices the GPU with
anything else there (an LLM sharing the card decodes ~40% slower during
synthesis bursts).

## Speaker identification (shadow mode)

`speaker.py` labels each voice turn with who spoke it: an enrolled person, a
`guest`, or `assistant` (the server's own TTS voice coming back through a
loudspeaker: echo). It is **shadow mode**: the label and scores are logged per
turn (`speaker`, `speaker_score`, `assistant_score` in the timing line) and
never change a reply, a tool choice or a permission. The one exception is the
echo guard below. The server starts it when `VOICE_SPEAKER_MODEL` is set; the
store is `VOICE_SPEAKER_DB` (default `VOICE_MODEL_DIR/voice-speakers.sqlite3`).
The assistant's voice reference is learned in the background the first time a
session's voice has none (cached in the store afterwards).

**Echo guard in the server** (`VOICE_ECHO_GUARD`: `enforce` (default),
`shadow`, `off`). While the assistant speaks, is held for a barge-in, or within
1.5 s after, an utterance is dropped (logged as `dropped` with the reason) when
its transcript repeats a run of at least 3 words, and at least 60% of its words,
of what the assistant said in this or the previous turn (`echo_text`), or, when
enforcing, its voice scores at least `VOICE_ECHO_SPEAKER_MIN` (0.6, stricter
than the 0.50 label) against the assistant's reference (`echo`). Commands it
just taught ("say forget my voice") are never treated as echo.

**Enrollment by voice.** An owner key saying "enroll my voice" (also learn /
register / remember) starts it: the assistant asks for three of the
enrollment sentences, one at a time, without calling the model; "stop" cancels;
"forget my voice" deletes the voiceprint. Other keys are told it needs the owner
to set it up (children only with the owner's say-so). The voiceprint is stored
under the key's principal.

* Model: NeMo TitaNet small (`nemo_en_titanet_small.onnx`, 192-dim embeddings;
  URL and digest in `models.json`) through sherpa-onnx on the CPU, no torch. It
  runs in its own Python like Chatterbox: `speaker_worker.py` started with
  `VOICE_SPEAKER_PYTHON` (a venv with `sherpa-onnx`), or in-process when that is
  unset and sherpa-onnx is importable here.
* `VOICE_SPEAKER_MODEL`: path to the `.onnx`. Unset, or the model/worker fails to
  load: the feature is off (logged once) and turns are untouched.
* `VOICE_SPEAKER_THREADS` (2): ONNX threads; the GPU host's cores are shared.
* `VOICE_SPEAKER_THRESHOLD` (0.45): cosine to an enrolled voiceprint to name the person.
* `VOICE_SPEAKER_ASSISTANT_THRESHOLD` (0.50): cosine to the assistant voice's
  reference to call a turn echo (and only when above the best person score).

Install on the GPU host, outside the voice venv:

```sh
uv venv -p 3.12 ~/voice-speaker/.venv
uv pip install -p ~/voice-speaker/.venv/bin/python sherpa-onnx numpy
curl -L -o ~/voice-speaker/models/nemo_en_titanet_small.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
sha256sum ~/voice-speaker/models/nemo_en_titanet_small.onnx    # compare with models.json
# voice service environment:
VOICE_SPEAKER_PYTHON=/home/<user>/voice-speaker/.venv/bin/python
VOICE_SPEAKER_MODEL=/home/<user>/voice-speaker/models/nemo_en_titanet_small.onnx
```

Audio under 0.6 s gets no embedding; audio past 10 s is cut to 10 s. Each
embedding is bounded by 2 s; a worker that crashes or hangs is killed and
restarted at most once a minute. Measured on kaiju (2 threads, through the pipe):
8 ms for 1 s of speech, 22 ms for 3 s, 73 ms for 10 s; about 2 cores while
busy; worker RSS 123 MB after load, ~315 MB peak after 10 s inputs. TitaNet
large was ~3x slower (57 ms for 3 s, 192 ms for 10 s), only slightly better on
people (same-person minimum 0.54 vs 0.43) and no better as an echo guard.

**Echo guard.** `learn_assistant_voice(voice, synthesize)` renders four
sentences in the session's TTS voice and stores the mean embedding of each
render clean *and* through two simulated echo paths (phone: 300-3400 Hz, 8 kHz
round trip, -10 dB, 15 dB SNR; loudspeaker in a room: 300 Hz high-pass, 0.4 s
reverb, 20 dB SNR). A clean-only reference failed: TitaNet scores lean on the
low band, so band-limited echo of the right voice scored medians of only
0.33-0.50, overlapping other voices and people. The reference is cached per voice and model.

**How the thresholds were chosen** (2026-10-22, kaiju, TitaNet small). People:
three Hi-Fi TTS readers, LJSpeech, Jenny and three voice-clone source clips (8
speakers, studio audio). A voiceprint (mean of 3 utterances) scored 0.43-0.92
(5th percentile 0.56) against the same person's other utterances and at most
0.27 (single utterances: 0.32) against other people; at most 0.44 against
Kokoro/Chatterbox voices. 1 s snippets of the same person: 5th percentile 0.34,
so short turns are often `guest`. Threshold 0.45. Through a phone-band channel
the same person drops to a median 0.22: enroll on the device you use.
Assistant: 8 Kokoro voices (8 sentences each) and Chatterbox output/reference
clips. Multi-condition reference vs held-out sentences of the same voice: clean
>= 0.57 (p5 0.66), phone channel p5 0.63 (min 0.49), phone + reverb p5 0.59
(min 0.41), loudspeaker room p5 0.68 (min 0.54); vs people (clean or through
the same channels) at most 0.43. Threshold 0.50 leaves a margin of only ~0.07
over the highest person; telephone-band echo with a brick-wall filter still
scored a median 0.43 (missed). Similar Kokoro voices score up to 0.63 against
each other (af_bella/af_heart), which only matters if two assistants share a
room. Revisit both thresholds with real shadow logs before ever acting on them.

**Enrollment and privacy.** Enrollment is opt-in, per person: read the four
`enrollment_sentences()` aloud; `enroll_add` refuses samples under 1.5 s, quieter
than -45 dBFS, clipped, or unlike that person's earlier samples, and
`enroll_finish` averages at least three into the voiceprint. Voiceprints
(embeddings, never audio) stay in one SQLite file on the voice host (mode 0600,
secure delete); pending samples expire after a day; `forget` deletes everything
for that person. A voice match is never authentication: it must not unlock,
approve or authorize anything risky (replayed or cloned voices pass easily).
Children are enrolled only with the owner's say-so.

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

## Turns: intake, barge-in and what was heard

* **Transcribe first.** A finished utterance (`Connection.heard`) is transcribed,
  and speaker-labelled when that is on, before it may touch the current turn.
  Utterances are handled one at a time, in order. Silence, noise, Whisper's stock
  phrases (`stt_gate.py`) and echo never cancel a reply; they are logged as
  `voice_turn_timing` with `status: dropped` and a `reason` (`quiet`,
  `no_speech`, `too_short`, `low_confidence`, `repetitive`, `hallucination`,
  `empty`, `echo_text`, `echo`).
* **Whisper's gate** (`stt_gate.py`): faster-whisper's VAD (min speech 200 ms,
  200 ms padding) then per segment `no_speech_prob` > `MAX_NO_SPEECH` (0.6),
  `avg_logprob` < `MIN_LOGPROB` (-1.0) or `compression_ratio` >
  `MAX_COMPRESSION_RATIO` (2.4) drops it; under `VOICE_MIN_SPEECH_S` (0.25 s) of
  voiced audio is too short; a stock phrase ("thank you", "thanks for watching",
  "you", "bye", ...) is believed only when Whisper was sure (no_speech <= 0.15,
  logprob >= -0.45, >= 0.6 s of speech). `condition_on_previous_text` is off.
* **One turn at a time.** Words heard while a spoken turn is still pending and
  none of its reply has played (within `VOICE_MERGE_WINDOW_S`, 10 s) join it:
  one user line, one reply. Otherwise the new turn replaces it. A new turn stops
  older Background runs that have not started an action; follow-ups of older
  turns are never spoken.
* **Confirmed barge-in.** A hello with `"barge": "confirm"` (Android from PR
  #154) gets `"barge": "confirm"` back in `session`. On local speech the client
  pauses playback and sends `speech_start` with `"hold": true`; the server stops
  sending audio and lets the utterance through. Real words: `interrupt` and a new
  turn. Nothing usable (or echo): `{"type": "resume", "turn": n}` and the reply
  continues where it paused. No utterance within `VOICE_HOLD_QUIET_S` (2.5 s):
  resume. Held for `VOICE_HOLD_MAX_S` (8 s): resume, or interrupt if the user is
  still talking (the phone gives up at 10 s).
* **History is what was heard.** An interrupted reply is recorded up to the
  played sample count (the client's `playback` acknowledgements, else the
  server's send clock), cutting the clause in progress by its played share and
  marking it `[cut off]`.
* **Endpointing.** Smart Turn scores the utterance at `VOICE_TURN_ANALYZE_MS`
  (300) of silence; a complete verdict ends it at `VOICE_TURN_COMPLETE_MS` (400,
  was 600); `VOICE_TURN_MAX_SILENCE_MS` (2000, was 2500) ends it regardless.
  Smart Turn takes ~65 ms on kaiju's CPU. On synthetic speech it scored
  cut-off sentences as complete too, so the thresholds were not lowered further.

### Replay set

`replay.py` makes and replays synthetic turns over a real socket, streamed in
real time like the Android client (aec, `barge: confirm`, playback acks):

```sh
python -m services.voice.replay make --url https://127.0.0.1:8900 --out ~/voice-replay/set1 --insecure
python -m services.voice.replay run --url wss://127.0.0.1:8931/ws --dir ~/voice-replay/set1 --insecure
```

Cases: silence, low noise, a noise burst, mains hum (no turn); three questions
(one turn, right answer); a question split by a pause (right answer); a real
barge-in (interrupt, new question answered); a noise barge-in (resume, no
interrupt); echo of the assistant's voice during its reply (informational). One
JSON line per case, exit 1 on a failed required case. Run it against the
candidate after every change, and against live after activation.

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

Voice turns also carry the STT gate's numbers (`speech_s`, `no_speech`,
`logprob`, `compression`), the speaker label when that is on (`speaker`,
`speaker_score`, `assistant_score`) and `merged` (the turn it joined). Dropped
utterances log `status: dropped` with a `reason` (see "Turns" above).

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
  (default `VLLM_URL`/`VLLM_MODEL`), no tools, thinking off (`reasoning_effort:
  none` plus `chat_template_kwargs.enable_thinking: false`; verified on kaiju:
  no reasoning tokens). The prompt is an **append-only transcript**
  (`front.Transcript`): the fixed block (persona, rules, mode) as the system
  message, then every exchange as it happened; new board facts ride in the
  next user message as a bracketed note. kaiju's model (Qwen3-Next on Strata)
  reuses its cache only when a prompt extends the previous prompt and reply: an
  append costs ~0.1-0.3 s to first token, any earlier edit a full prefill at
  ~600 tokens/s. It starts from the last `VOICE_FRONT_TURNS` (6) stored
  exchanges and restarts past `VOICE_FRONT_TRANSCRIPT_CHARS` (14,000) or when the
  fixed block changes. At session start a silent one-line exchange warms it
  (`VOICE_FRONT_WARM=0` disables). The first clause may end at a comma once it
  has `VOICE_FRONT_EARLY_WORDS` (5) words, so speech starts sooner. Front never
  says an action is done; a follow-up corrects an earlier wrong answer.
  `VOICE_FRONT_MAX_TOKENS` (200), `VOICE_FRONT_TIMEOUT_S` (30).
* House agent (owners): Background's `house_agent` tool asks the hub's house
  agent (`home.ask` on worker `VOICE_HOUSE_WORKER`, default `rook`; up to
  `VOICE_HOUSE_TIMEOUT_S`, 100 s) for anything its own tools cannot do; the
  answer is the result Front speaks. Front's transcript starts with the house
  agent's shared profile and fact memories (`memory.list`), fetched at session
  start.
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

## House engine (`VOICE_ENGINE=house`, opt-in)

Design: [docs/design/persistent-agents.md](../../docs/design/persistent-agents.md)
3.4. The house agent's persistent voice session (Claude Haiku on the session
host, cachyrig) replaces Front and Background as the brain of a
`front_background` turn. Everything else stays here: STT and its gate,
endpointing, echo guard, speaker ID, TTS with clause chunking and the early
first clause, barge-in, timing. Code: `house.py` (`Lane`, `HouseVoice`), hooked
into `FrontBackground.turn` in `pipeline.py`.

| Variable | Meaning |
|---|---|
| `VOICE_ENGINE` | `house` turns it on; unset or anything else: Front/Background as before |
| `VOICE_HOUSE_URL` | the session host's voice lane, e.g. `ws://192.168.1.144:8791/voice/house` |
| `VOICE_HOUSE_TOKEN` | its bearer token (secret: from the secret store, never logged or sent to clients) |
| `VOICE_HOUSE_FOR` | `owner` (default: owner keys only; guests and other keys keep the old path) or `all` |
| `VOICE_HOUSE_FIRST_DELTA_S` | no first words this long after the turn started: the old path answers it (4) |
| `VOICE_HOUSE_CONNECT_S` | budget for opening the lane, `ready` included (2) |
| `VOICE_HOUSE_TURN_S` | longest silence inside a turn once it spoke, e.g. between tool calls (60) |

* Only connections with hello `"pipeline": "front_background"` use it, in the
  assistant mode and in `listen` (active listening) and `brainstorm` with their
  built-in prompts; their `session` event carries `"engine": "house"`. The turn
  carries `"mode"` when it isn't assistant. Conversation (children), roleplay,
  custom prompts, the classic pipeline and dictation are unchanged.
* The voice may switch the connection's mode itself ("switch to dictation"):
  the host sends `{"t":"mode","mode","by":"voice"}`; the server switches only
  between `modes.VOICE_SWITCHABLE` (assistant, dictate, listen, brainstorm) and
  only from a mode the house voice serves, and emits `{"type":"mode",...,"by":
  "voice"}`, which the APK accepts only between those same modes. A dictation
  the voice started goes back to the previous mode on "I'm done", and its text
  goes to the voice as a `[dictation] ...` turn (it offers to save or send it).
* The conductor routes with hidden lines that the host cuts out; as a backstop
  nothing from a `<<` on in a delta is spoken.
* The engine is the hub's choice (`home.voice_engine`), sent in the lane's
  `ready` and `config`: with `local` every turn quietly takes the old
  Front/Background path (timing `house_engine: local`), no kaiju edit needed.
* The app's Voice models screen sends `{"type":"models_get"}` or
  `{"type":"models_set","args":{...}}`; they go to the hub through the lane as
  this connection's identity and the answer comes back as a `voice_models`
  event.
* One lane websocket per voice connection, opened on its first turn and
  reopened after it drops. Per turn it sends `{"t":"user","turn","text",
  "identity":{"owner","name"}}` (`name`: the speaker ID label when it named an
  enrolled person, else the key's name; plus `merged_from` when the utterance
  joined an earlier, cancelled turn). `delta` text is spoken clause by clause;
  words after the conductor's tool calls continue the same turn; `tool` events
  are owner `background` events (`tool_call`); `end` closes the turn. Barge-in
  or a newer turn sends `{"t":"cancel","turn"}`.
* `push` (a back-end result) is spoken like a follow-up, once nothing is being
  said and the user is quiet (up to `VOICE_FOLLOWUP_WAIT_S`). One that cannot be
  spoken (the client said stop, the connection closed, it stayed busy) is
  reported: an owner `background` `dropped` event with its text and a log line
  without it.
* History is what was heard, as for Front: the echo guard knows the house
  voice's words, an interrupted reply is cut where it stopped, and Front's
  transcript gets the same exchange.
* Fallback: lane unreachable, refused, no `ready`, an error before any words,
  or no first words within `VOICE_HOUSE_FIRST_DELTA_S`: this turn runs on
  Front/Background and the log says why. After a failed connect the lane is not
  tried for 2 s doubling to 60 s (60 s after a refusal), so turns don't each pay
  the connect timeout. If the conductor already called a tool but has said
  nothing, it is not run twice: "One moment." and the turn keeps waiting.
* Timing: `voice_turn_timing` adds `engine` (`house`, or `front_background`
  with `house_fallback`: `unreachable`, `refused`, `timeout`, `not_ready`,
  `backoff`, `no_first_delta`, `error`, `closed`, `send_failed`);
  `front_first_token_ms` is the first delta, `front_first_audio_ms` its audio.
* Back to the old path: remove `VOICE_ENGINE=house` from
  `/etc/voice-agent/unified.env` and restart the service.

## Deployment

See [deploy/README.md](deploy/README.md): releases are built from a commit,
smoke-tested as a loopback candidate beside the live unit, switched with one
systemd drop-in and rolled back with one command.
