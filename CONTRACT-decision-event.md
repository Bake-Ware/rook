# Voice protocol addition: opt-in `decision` ("thinking") event  - SHARED CONTRACT (server + APK)

Opt-in: the client adds `"thinking": true` to its protocol-2 `hello`. Absent/false => server NEVER sends `decision` events (old clients unaffected). Session event echoes it: `{"type":"session", ..., "thinking": true|false}`.

Per turn (voice or typed text), server emits at most one:
```json
{"type": "decision",
 "turn": <int, same turn/epoch id used by state/assistant_* events>,
 "source": "voice" | "text",
 "mode": "shadow",
 "status": "ok" | "timeout" | "error" | "disabled",
 "latency_ms": <float|null>,
 "engine": {"model": str|null, "adapter": str|null, "calibration": str|null},
 "answers": [
   {"id": "needs_response", "type": "noul", "p": 0.97, "confidence": 0.94},
   {"id": "intent", "type": "choice", "choice": "device_control", "probabilities": {"device_control": 0.94, "...": 0.01}, "confidence": 0.9},
   {"id": "needs_confirmation", "type": "noul", "p": 0.02, "confidence": 0.98},
   {"id": "context_source", "type": "choice", "choice": "none", "probabilities": {...}, "confidence": 0.8},
   {"id": "urgency", "type": "score", "level": 1, "expected": 1.3, "probabilities": {...}},
   {"id": "is_correction", "type": "noul", "p": 0.03, "confidence": 0.95}
 ],
 "error": null | str}
```
- `needs_response` is omitted for `source: "text"` (typed input is always addressed).
- `mode` is always "shadow" for now: the decision NEVER changes what the assistant does.
- The event may arrive before OR after `assistant_delta` for the same turn; clients attach it by `turn`.
- Unknown answer ids must be ignored by clients (forward compatible). Numbers are 0..1 probabilities.
- Clients must tolerate `status != ok` with empty `answers`.

The activity contract (`services/voice/CONTRACT-activity.md`) strengthens this to
exactly one event per connected turn with thinking:true. `engine_status` is
`ok|disabled|skipped|timeout|error`; `answers` exists only for ok. `elapsed_ms`,
optional `detail`, and model/adapter aliases are also supplied. For compatibility,
legacy `status` represents skipped as disabled. Engine work starts only after
reply dispatch and foreground completion; an interrupted undispatched turn gets
a visible skipped event. Activity and thinking are independent opt-ins.
