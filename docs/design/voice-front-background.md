# Voice: a fast front voice over a background worker

Status: approved plan (2026-10-05), being built. Tracking: rook project
`rook-oct-2026-batch`, tasks "Voice P0" … "Voice P6".

## Why

A voice turn today waits for one model call to produce a complete, validated
tool plan (`respond` or a tool) before any audio plays. The prompt is ~2,300
tokens and changes near the top every turn, so the model's prompt cache misses
(2.5 s cold vs 0.4 s warm on kaiju's llmanifold), and an invalid plan costs a
full retry. Measured on 2026-10-05: ~8 s from transcript to the first plan.

## Shape

```
speech ─► STT ─┬─► FRONT  streams speech at once; no tools, no thinking
               │          stable prompt prefix; board + recent turns at the end
               │          speaks in the first person about what Background does
               └─► BACKGROUND  same transcript, in parallel
                          decides nothing / fetch / act; runs tools under the
                          caller's identity and mode; writes the board; emits
                          progress + follow-ups; every step goes to the
                          Background tab
```

### Front
- One streaming chat call per turn, no `tools`, thinking disabled. Text is
  split into clauses and sent to TTS as it arrives.
- Prompt order (cache-friendly): persona + mode instructions + rules (fixed),
  then the context board, then the last N turns, then the utterance. Nothing
  that changes per turn sits before the fixed block. History is stored without
  the old "[Spoken response generated…]" marker (the model copied it).
- Rules: talk as the one doing the work ("Let me check your calendar…").
  Only state facts that are common knowledge, already in the conversation, or
  on the board. If the request needs a lookup or action, say a short natural
  acknowledgement and stop; never invent a result.
- Narration: Background `tool_start` events are voiced as short first-person
  progress ("Checking your calendar.") from templates (no model call), at most
  one per tool, only if Front is not already speaking.
- Follow-ups: when Background finishes with something new, Front gets one
  short internal turn containing the new board facts and speaks the result.
  A follow-up waits until Front has finished speaking; it is dropped (still
  shown in the Background tab) if the user started a newer turn that Background
  is now handling.

### Background
- Starts from the final transcript at the same moment as Front, with the board
  and recent turns. Uses the existing planner / thinking agent and tools.
- Prefetch on wake (before the user finishes speaking), cheap and parallel:
  time/date, the caller's device state (battery, location if allowed), active
  timers, open rook tasks for the owner. Results go on the board with a TTL.
- Tool policy is enforced here only (Front has no tools): the guest/owner/
  device-mapped key rules from 2026-10-05 plus mode limits (below).
- Board: per-conversation list of facts `{key, text, source, ts, ttl_s}`,
  capped (e.g. 20 items / 2 KB), newest wins per key.

### Modes (Background tool sets)
| mode / identity | Background tools |
|---|---|
| assistant, owner key | everything below + rook_read/rook_call/agent work |
| assistant, device-mapped key | timers, web_search, weather, own-device reads |
| guest (no key) | timers, web_search, weather |
| conversation (kids), brainstorm, roleplay, active listening | timers, web_search, weather |
| dictate | none (unchanged) |

## Standard tools (Background)
- **Timers** (all modes, guests included): `timer_set(seconds | at, label)`,
  `timer_list()`, `timer_cancel(id | label)`. The server keeps per-conversation
  timer state for list/cancel and tells the client; the **client** schedules
  and rings it (works with the voice connection closed, no band access needed).
  On reconnect the server resends active timers (idempotent by id).
- **Calendar** (owner/device key): phone cap `calendar.list(start, end, limit)`
  reading Android's CalendarContract, which includes the Google Calendar app
  and Outlook (when Outlook's "sync calendars" is on). Needs READ_CALENDAR
  (grant button in Settings).
- **Mail, read-only** (owner/device key): recent Gmail/Outlook notifications via
  the existing `notify.list`, filtered by package. No history; that is route A.
- **Weather**: Open-Meteo (no key) for the device's location or a configured
  home location.
- **Rook tasks** (owner): read the deck / a task via the voice server's rook MCP.
- **Music**: existing pianobar `cmd.pianobar-*` caps (owner).
- **Home Assistant**: after a read-only version/API check; lights, switches,
  scenes, climate; owner only. Separate step.
- Later: Bayb family lists, calendar add, phone media keys.

## Protocol additions (protocol 2, opt-in by hello flags)

Hello may add `"background": true`, `"timers": true`, `"pipeline": "front_background" | "classic"`
(default `classic` until rollout finishes). Servers ignore unknown flags; old
clients never send them.

### `background` event — owner identities only, every Background step
```
{ "type":"background", "turn":<int>, "seq":<int, monotonic per connection>,
  "ts":<epoch ms>, "kind":"start|prefetch|thought|tool_call|tool_result|board|followup|dropped|done|error",
  "text":<short human text>, "tool":<opt>, "args":<opt object, secrets masked>,
  "result":<opt text, ≤2000 chars>, "status":<opt ok|failed|cancelled>,
  "elapsed_ms":<opt> }
```
Never sent to guest or device-mapped identities. The app shows these in the
**Background** tab (formerly "Decisions"), always on.

### `timer` event — any identity, if hello had `timers:true`
```
{ "type":"timer", "action":"set|cancel", "id":<str>, "label":<str>,
  "fires_at":<epoch ms>, "duration_s":<int> }
```
Client: Android schedules an exact alarm (AlarmManager; falls back to inexact
if the exact-alarm permission is missing), shows a notification, speaks
"<label> timer is done" through on-device TTS (SpeakBridge) and adds a chat
line; the browser uses setTimeout + speechSynthesis. Cancel removes it.

### Timing (every turn, server log + `background` kind `done`)
`stt_ms, front_first_token_ms, front_first_audio_ms, background_ms,
followup_ms` measured from end of speech.

## Retired / paused
The decision engine and diffucision are paused: `DECISION_URL` stays empty and
the `decision` event is no longer rendered by the app (the tab now shows
`background`). Code stays for later.

## Phases
0. Reunify kaiju's live voice code with master; deploy that to kaiju with no
   behaviour change; from then on kaiju runs only tagged master builds
   (deploy script + rollback). Decision engine off.
1. Per-stage timing.
2. Front.
3. Background + board + prefetch + follow-ups + `background` events.
4. App: Background tab.
5. Tools above; Home Assistant after its check.
6. `pipeline` flag: classic vs front_background, on bakephone first, then default.
