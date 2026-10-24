# Attention on the phone: decision cards, read/ack signals, the quiet feed

Status: built overnight 2026-10-22/23 (Track C, APK side). The hub side
polls and posts against the contract below.

## What it does

1. **Decision cards.** The hub puts a card on the phone (`attention.post`):
   one line of context, big Yes / No / Explain buttons, the source and age in
   small print, "1 of N" when several wait (oldest first). Urgent cards
   (`urgency=urgent` or `channel=urgent`) ring on the `rook_urgent` channel
   with a full-screen intent: they turn the screen on and show over the lock
   screen. Ordinary cards (channel `approvals`, also `family`) are a heads-up
   only; if one is still waiting the next time the phone is unlocked, the card
   opens then. Yes/No on a locked phone asks for the unlock first. A decision
   shows "Sent…" until `attention.update` says done (a brief confirmation, then
   the next card) or failed (the message stays on the card).
2. **Read/ack signals.** Everything the person does with a card is an event
   the hub reads with `attention.poll`. Events are persisted on the phone
   (last 1000) and wait there while the phone is offline.
3. **The quiet feed.** The notification listener sorts every other app's
   notification on the phone into urgent / family / normal / ignore with a
   rules file. Non-ignored items go to a local feed (Attention tab, 300
   entries, 7 days). Urgent items also ring a local alert card ("Got it");
   family items get a heads-up on `rook_family`; normal items are feed only.

## The contract (caps on the phone worker)

| Cap | Args | Returns | Tier |
|---|---|---|---|
| `attention.post` | id (≤64), kind hold\|job\|card\|ask\|alert, line (≤140), explain="" (≤600), urgency normal\|urgent, channel approvals\|urgent\|family\|digest\|jobs, actions ⊆ yes/no/explain/ack (default yes,no,explain; alerts ack), source="", expires_at=0 | `{ok, id, new}` | admin |
| `attention.update` | id, state done\|failed\|gone, message="" | `{ok}` | admin |
| `attention.poll` | since=0 | `{ok, events, last}` (+ `more`, `reset`, `gap`) | admin, sensitive |
| `attention.feed` | limit=50 | `{ok, entries, ignored_7d}` (summaries only) | exec, sensitive |
| `attention.rules_get` | — | `{ok, source, extend_default, rules}` | exec, sensitive |
| `attention.rules_set` | rules (list, document, or empty to reset) | `{ok, source}` | admin |
| `attention.remind` | id (≤64), text (≤300, whitespace collapsed), sample_ms=3000 (clamped 1000-8000), speak_unchecked=false | `{ok, id, accepted}` (+ `busy` or `reason: "off"` when not accepted) | admin, physical |

Tiers are the hub's floor (`rook/core/authz.py`): only the hub's attention
plugin should post cards or read taps, because a card's Yes is relayed as the
person's decision; agents' admin calls are held for the person. `feed` and
`rules_get` stay exec (sensitive), like `notify.list`.

Re-posting an id updates the card in place and never re-rings. Over-long
text is clipped, unknown enum values are refused.

Events: `{seq, ts, type, ...}`, seq monotonic across restarts.

- `decision` {id, decision approve\|deny, reason?} (reason only on No, optional)
- `seen` {id, via card\|heads_up}: the card screen had focus, or the heads-up
  was posted while the phone was awake and unlocked (an approximation:
  Android does not report when a notification is actually looked at)
- `opened` {id}: the notification or the app's row was tapped
- `dismissed` {id}: swiped away, or Later/Back on the card; it stays in the
  app's list but no longer takes over on unlock
- `acked` {id}: "Got it" on an alert
- `unlocked` {}: ACTION_USER_PRESENT (heard while the worker service runs)
- `notice` {id, app, category, priority, summary}: an urgent or family
  notification from another app
- `reminded` {id, outcome, via?, level?, checked?}: an `attention.remind`
  finished (see Spoken reminders below)

`last` is the seq to pass as `since` next time. `reset: true` means the
phone's log is behind the caller's `since` (app data cleared) and everything
was returned; `gap: true` means events were dropped by the cap before they were
read; `more: true` means call again.

`notify.post` gained an optional `channel` (the same five names);
`device.update` an optional `channel` stable\|beta (see below).

## Spoken reminders (attention.remind, app 0.4.22-beta.3+)

The hub decides when to remind and what to say ("Need your input: deploy hub
update?", "Hey boss, you have 6 decisions pending."); the phone decides
whether the room is quiet enough and speaks. The cap returns at once
(`accepted: true`); one reminder runs at a time (`accepted: false, busy`
otherwise); the app's **Spoken reminders** switch off gives `accepted: false,
reason: "off"` and an `off` event. Then, on its own thread:

1. **Pre-checks**, in this order, each ending it unspoken: `dnd` (Do Not
   Disturb or bedtime: the interruption filter is not "all"; the hub's quiet
   hours are off by default, so this keeps reminders out of a sleeping room),
   `call` (audio mode
   in call, ringing or in communication that is not Rook's own conversation;
   telephony call state below Android 12), `voice_session` (Rook's voice
   conversation is live or a session is open, or the voice is thinking or
   speaking), `mic_in_use` (another app records: recording configurations
   without Rook's own and the system hotword, API 24+; or wake standby gave
   the mic away), `media` (the music stream is active and Rook is not the one
   speaking), `speaking` (Rook's voice.speak queue or reply window is busy).
2. **Quiet sample** of `sample_ms`. With wake-word standby capturing, the
   levels come from the frames standby already reads (an in-memory tap on
   VoiceService); no second recorder. Otherwise a short AudioRecord of its own
   (16 kHz mono, VOICE_RECOGNITION, not privacy-sensitive) that is released on
   every path. The pre-checks are re-read every 100 ms meanwhile; another
   recorder, a call or Rook's voice ends the sample at once. All-zero audio,
   a silenced client (API 29+), a recorder that won't open or start, no
   microphone permission, or standby letting go before 60% of the sample:
   `unchecked`, spoken only with `speak_unchecked`.
3. **Decision** (`QuietCheck`, pure Kotlin, unit-tested): 30 ms frames, RMS in
   dBFS. Active frame: above -50 dBFS and 10 dB above the sample's 20th
   percentile. Active frames count only in runs of at least 240 ms (one quiet
   frame bridged); 300 ms counted is `busy` (a clap, a door or a cough is not).
   Also `busy`: median at least -40 dBFS with p90 - p10 at least 6 dB (a TV or
   a conversation that raised the floor itself), or median at least -25 dBFS
   (too loud to be heard). A steady hum stays quiet. Under 0.9 s measured, or
   90% of frames all-zero: `unchecked`.
4. **Speak** through voice.speak's path with the app's selected voice (the
   voice server's house voice when configured, Android TTS as fallback), no
   interrupt, no reply window, no chat line, after one more pre-check.
   `spoken` with `via` server or device; a failure or no end within 120 s is
   `error`.

`level` is `{active_frac: 0.0-1.0 (two decimals), peak_dbfs: int}` whenever a
sample was measured; `checked: false` marks a reminder spoken without a quiet
check (`speak_unchecked`). The audio is read into one 30 ms buffer, reduced to
a level and overwritten; it is never written, kept, logged or sent.

An app before 0.4.22-beta.3 has no `attention.remind`; the hub has to fall
back (a card or nothing). Nothing about `post`/`poll` changed for older hubs:
they see `reminded` events only if they call `remind`.

## What leaves the phone

Notification content stays on the phone. The classifier, the rules and the
feed all live in the app's private storage (`files/attention/`).

What leaves, and only when a band caller polls:

- `notice` events for **urgent and family** items: the app's label, the
  category, the priority, and a summary of at most 140 characters:
  `"<App>: <category> — <title, clipped to 60>"` plus at most the first five
  words of the text. For a messaging app the title is usually the sender's
  name, so the sender's name leaves the phone.
- `attention.feed` returns the same summaries for urgent, family and normal
  items (never the full text), and a count of ignored ones.
- `attention.rules_get` returns the rules, which may contain family names.
- Card events carry only the card id, the decision, an optional typed
  reason for No, and timestamps.
- `reminded` events carry the outcome, the speech route and two rounded
  numbers about the room's loudness; never audio or anything heard.

Normal and ignored notifications produce no event. Nothing is pushed: the
hub has to poll.

**Pre-existing, unchanged:** `notify.list` (exec tier, sensitive) returns the
full title, text and big text of recent notifications from every app to any
band caller allowed to run exec caps on the phone. Attention does not widen
that, but it does not narrow it either; anyone who can call `notify.list` can
already read everything this design keeps on the phone.

## Rules

Shipped as `android/app/src/main/assets/attention_rules.json`; the hub may
replace them (`attention.rules_set`), stored as `files/attention/rules.json`.

```json
{"family_names": ["Mom"], "family_starred": true, "default": "normal", "extend_default": false,
 "rules": [{"priority": "urgent", "category": "trusted", "note": "who set it",
            "match": {"packages": [], "app_labels": [], "words": [], "senders": []},
            "scope": {...}, "except": {...}}]}
```

First match wins. A rule matches when any list in `match` hits, `scope` (if
present) also hits and `except` does not. `packages` are substrings of the
package name; `app_labels` and `words` are phrases at a word start
(`"evacuat"` finds "evacuate"); `senders` are whole names in the title, and
`"@family"` means `family_names` plus starred contacts (when
`family_starred` and Contacts is granted). A bare list given to `rules_set`
goes in front of the defaults; a document replaces them unless
`extend_default: true`. A trust map from elsewhere fits as `{priority, match:
{senders}, note}` entries.

Default order: system/clock noise, shops and shipping apps, social and news
apps → ignore; the urgent words ("fire", "emergency", "evacuat", "lockdown",
"911", "urgent", "ambulance", "injured", "hurt", "police", "alarm", "pick up
now", "call me") → urgent, **but only from messaging, phone, school, health
and home-safety apps or from family**, and not with promo words; family names
→ family; school apps or "school" → family (category school); health,
pharmacy, doctors → family (category health, not promos); delivery, promo and
newsletter words → ignore; anything else → normal.

The urgent scope is narrower than "any notification with an urgent word": news
and shopping apps say "fire" and "police" all day, and an urgent item rings
like an alarm through Do Not Disturb. Widen it with `rules_set` if needed.

Noise skipped before classifying: Rook's own notifications, ongoing and
foreground-service notifications, group summaries, the alarm/call/progress/
service/system/transport/navigation/stopwatch categories, empty ones, and an
update that changes nothing.

## Android channels and grants

Channels (stable ids): `rook_urgent` (high, alarm sound via USAGE_ALARM,
vibration, bypass Do Not Disturb, public on the lock screen),
`rook_family` and `rook_approvals` (high, heads-up), `rook_digest` (low),
`rook_jobs` (min, silent).

Grants, listed with a button each in the app's **Attention** tab (shown only
while one is missing; a one-line link above the chat points there until the
tab has been opened once):

- Notification access (the listener; also used by `notify.list`)
- Post notifications (Android 13+)
- Do Not Disturb access (Notification Policy). Without it Android drops the
  urgent channel's bypass; the app re-submits the channel on every resume so
  a later grant takes effect. The user can also allow it per channel.
- Full-screen alerts (USE_FULL_SCREEN_INTENT; Android 14+ special access).
  Without it an urgent card is a heads-up.
- Display over other apps: Android 10+ blocks starting the card from the
  background on unlock without it (the HID accessibility service, when
  enabled, also allows it).
- Battery optimisation off.

## APK update channel

`SharedPreferences("rook")["apk_channel"]`: `stable` (default, never changed
for anyone) or `beta`. On beta the updater fetches `apk.json?channel=beta`
and `apk?channel=beta`; the hub answers with an approved, newer beta or the
stable build, so the version, hash and signature checks are unchanged.
Settings → App shows Stable / Beta. `device.update {channel}` sets it before
checking; `device.update_status` reports it. Going back to stable does not
downgrade: the updater only installs higher version codes.
