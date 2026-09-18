# Rook voice service

Version 2 replaces the unversioned `voice-agent/server.py` orchestration on kaiju.
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
  intentionally have no credential.
* `ROOK_MCP_URL`, `ROOK_MCP_TOKEN`: capability endpoint and runtime credential.
* `ACP_HOST`, `ACP_PORT`: Hermes ACP endpoint. `ACP_AUTO_APPROVE=1` preserves the
  existing unattended permission-choice policy; set `0` to decline ACP permission
  requests. Prompts are never automatically retried after transport failure.
* `VOICE_STATE_DB`: private SQLite file; defaults beneath `VOICE_MODEL_DIR`.
* `VOICE_BIND`, `VOICE_PORT`: default loopback port 8900.
* `VOICE_TLS_KEY`, `VOICE_TLS_CERT`: existing PEM paths when serving TLS directly.

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

The smoke uses a synthetic conversation and a read-only `info.uptime` lookup.
Android instrumentation mode `voice` checks silence/noise and the bundled
synthetic speech fixture. Physical-phone speaker, headset and Bluetooth acoustic
checks remain necessary; emulator success does not establish real-room false
wake rates or barge-in latency.

### Decision-engine shadow mode

`DECISION_URL` enables an advisory HTTP side channel, e.g.
`http://127.0.0.1:8910`. Unset/empty disables requests, feedback collection, and
decision events. `DECISION_TIMEOUT_MS` defaults to **150 ms**, including connection,
queueing and response parsing. A failed or slow engine never changes the reply,
tool selection, confirmation policy, or interruption behavior. Requests run
concurrently with normal turns; no inference is awaited by reply generation.
The voice process loads no additional model or CUDA allocation.

Only protocol-2 clients with the literal `"thinking": true` in hello receive
`decision` events; `session.thinking` echoes the opt-in. See the exact
[shared contract](../../CONTRACT-decision-event.md). One event at most is emitted
per external turn; it can arrive after the reply and retains the original turn
number. Typed turns omit `needs_response`. Clients without opt-in still generate
shadow feedback when the server feature is enabled, but receive no decision
events. Internal job-completion narration does not create another decision.

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

Database writes use a dedicated worker with a bounded 256-operation backlog and
short SQLite lock timeout. Inference has a bounded per-connection task count.
Telemetry can be dropped under overload rather than blocking normal turns;
failed writes log exception types only. Raw state (including prior-reply context)
and reply text are cleared after `DECISION_RAW_RETENTION_DAYS=30`, at startup and
every minute. `secure_delete` is enabled on feedback writes. Numeric observations
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
reply timings and the engine's voice lights example. See
[deployment and rollback](DEPLOYMENT-decision-shadow.md) for the kaiju release.
