# Decision model

`decide.run` asks a one-pass decision model (Diffucision, or the typed
decision engine) a batch of typed questions about one state, and gets every
answer back from a single forward pass: fast and cheap enough for a gate.

## Set up

Set **endpoint** to the service's base URL (`http://kaiju:8710`), **adapter**
to its dialect, and **token** if it wants a bearer token. `decide.status`
says whether it is reachable and loaded.

## Questions

```json
{"state": "the email text ...",
 "questions": [
   {"id": "spam", "type": "noul", "question": "Is this spam?"},
   {"id": "topic", "type": "choice", "question": "Topic?", "options": ["billing", "support", "other"]},
   {"id": "urgency", "type": "score", "question": "How urgent?", "levels": ["low", "medium", "high"]}]}
```

Answers: `noul` gives `p` (P(yes)); `choice` gives `choice`; `score` gives
`level` and the expected `score`. Each has `probabilities` and `confidence`
(the top-two margin). Probabilities are uncalibrated.

## Drive a screen

`decide.drive {goal, screen_worker, input_worker?, touch?}` loops inside one
call: `screen.capture` on the screen worker, what is on it (the **perceiver**
service, else the device's `ui.screen`, else nothing: a blind run), one
decision pass (action, target element or grid cell, which prepared text or
key, done / ready / needs confirmation / abort), then `hid.*` on the input
worker. It is a dry run unless `dry_run: false` (the **dry_run** setting):
it reports the calls it would make.

It ends when the goal is reached, the model asks to stop, a budget runs out,
or an input fails. **halt** (the kill switch) stops new drives; a running one
ends at its `max_seconds`, and each input it sends goes through the gate like
any call. Two ends need you:

- `needs_confirmation`: its gates flagged the next action (blind, low
  confidence, destructive-looking words or keys, the model's own flag, or
  **confirm** = always). `pending` has the exact `hid.*` calls: make them
  yourself (or ask the person first), then drive again.
- `held`: the hub held one of its input calls for a person (the trust map
  or a hard stop); `hold` names it. Once it is approved and runs, drive again.

## Changed from 0.4

`decide.health` and `decide.info` are one `decide.status`. A drive runs
inside its call and answers with how it ended; `decide.runs`, `confirm` and
`stop` are gone (one approval path: a gated step comes back to you, a held
input is an ordinary hold). It speaks the device contracts (`screen.capture`,
`ui.screen`, `hid.click/key/type/drag`) instead of backend-specific caps. The
`cap://` endpoint is gone: a model served from a worker is reached by its URL.
