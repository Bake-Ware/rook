# Contract: activity + decision events (protocol 2, opt-in)

Client hello may include `"activity": true` and/or `"thinking": true`. Servers that don't know the flags ignore them; old clients never send them, so old APKs (family devices on 0.4.1) see no new events.

## `activity` event (only if hello had activity:true)
{ "type":"activity", "turn":<turn id>, "seq":<int, monotonic per connection>, "ts":<epoch ms, server>, "phase":<see below>, "label":<short human text>, "detail":<optional text>, "tool":<opt>, "worker":<opt>, "cap":<opt>, "elapsed_ms":<opt, duration of the phase that just ended>, "timeout_ms":<opt>, "status":<opt ok|failed|cancelled> }
Phases: `heard` (final transcript, detail=text), `planning` (LLM call started), `planned` (chosen function, elapsed_ms), `retry` (invalid plan, retrying), `fallback` (safe fallback spoken), `tool_start` (tool/worker/cap, timeout_ms), `tool_wait` (heartbeat every 2s while a tool/job is outstanding, elapsed_ms so far), `tool_result` (status, elapsed_ms, detail=short error text if failed), `speaking` (TTS started), `done` (turn finished, elapsed_ms total), `error` (detail=readable message).
Emission must be cheap and non-blocking (queue onto the existing websocket send path; never await anything slow). No raw prompt text; transcripts are fine (the user already sees them).

## `decision` event (only if hello had thinking:true) - extends CONTRACT-decision-event.md
Exactly one per turn, ALWAYS sent, even when the engine wasn't called: add `"engine_status"`: `ok` | `disabled` (no DECISION_URL) | `skipped` (with reason) | `timeout` | `error` (detail). `answers` present only when ok. Keep `mode:"shadow"`, `elapsed_ms`, `model`/adapter id if known.

## Client stall rule
During a turn, if no activity event arrives for 15s and no tool_wait heartbeat is flowing, show "No response - may be stalled" (amber), and after 45s "Stalled" (red). If the server never sends activity (old server), fall back to current behavior silently.

## progress phase (added)
`progress`: server spoke a status update while a job is outstanding; label = spoken text, plus tool/worker/elapsed_ms. Clients render it like any other activity row and may show it in the status strip. Hello may include `progress_updates: {enabled, first_after_s, every_s}`; defaults enabled/25/45.

Spoken progress uses real outstanding job metadata and templates, with no LLM
request. The first update is due after 25 seconds without speech for that job;
then every 45 seconds after the previous update finishes. At most three ordinary
updates plus one final “This is taking a while - I'll tell you when it's done.”
are spoken per job. Positive finite `first_after_s` / `every_s` numbers override
these intervals; invalid values use defaults. `enabled:false` disables speech.
Activity remains independently opt-in: old clients get spoken progress by default
but never receive activity or decision events without their respective flags.

Updates defer during user speech, VAD speech within three seconds, current
playback, active foreground turns, or sleep/off mode. Deferred updates coalesce;
completion, failure and cancellation stop them immediately. The current audio
path exposes VAD but no separate noise/energy classifier, so suppression uses
speech only. Server-selected `end_session` sets sleep/off; clients may also send
`{"type":"client_state","mode":"sleep"|"off"|"awake"}`. New user input wakes
the connection. No client implementation change is required for server-selected
sleep or clients that disconnect on sleep.

Progress speech uses the current audio turn (so playback accepts it); its
`activity.progress` retains the originating job's turn. It is not a new planner
turn and does not cause another decision event. Internal result-narration turns
have their own turn ID and a decision with `engine_status:"skipped"` and a reason
(or `disabled` when the engine is disabled). Disconnected clients cannot receive
terminal events. Decision inference starts in the background only after reply
dispatch and foreground completion; SQLite telemetry runs in a single executor.

## Trusted device mapping for personal reads

`VOICE_IDENTITIES_FILE` points to a private, server-owned JSON object keyed by
SHA-256 of a bearer credential. Each entry has `principal`, exact live Rook
`worker` name, and optional `owner:true` (only Bake). The server accepts those
credentials alongside the existing `VOICE_TOKEN`; conversation keys remain
credential-scoped. Missing mappings have no personal-data access. Neither hello
`principal`/`client`/`worker` fields nor natural-language claims grant access.
A mapped non-owner can read only their assigned worker; Bake may name another
worker. An unmapped connection asks which device and explains that a verified
mapping is required; answering with a name alone does not grant access.

The tool adapter enforces this for sms.list, calllog.list, contacts.search,
notify.list and location.get before any Rook request. The planner gets the same
trusted device context and must never send privacy refusals to Hermes. The live
read catalog is READ_CAPS intersected with cached worker capabilities, including
cached caps.describe argument names. Inventory/schema refresh runs in background;
there is no discovery request in the planner's reply path.
