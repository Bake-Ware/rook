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
