# Attention: the phone as the one app allowed to interrupt

The Rook app on the person's phone is the one place Rook may interrupt them.
The hub puts **decision cards** (everything waiting on the person) and
**alerts** on it, and reads back what they tapped and whether they saw it.

Code: `rook/hub/plugins/attention/` (hub side). The phone side
(`attention.post`, `attention.update`, `attention.poll`,
`attention.rules_set`) lives in the Android app; this page is the contract
between them.

## Pull only

The phone never calls the hub. Every `attention.poll_secs` (10 s) the hub's
`attention` plugin (placed `is_hub`, one per band) goes over the phones named
in `attention.devices` that are online and announce `attention.post`, and:

1. **polls** `attention.poll {since: <last seq>}` and handles each event in
   order (below). The last seq per phone is saved after every event, so a hub
   restart never replays a tap;
2. **syncs cards**: every item `rook/hub/relay.py` `Relay.items()` lists (held
   calls, held job calls, huddle cards, pinned questions) that this phone does
   not have yet is posted once; an item that is no longer waiting gets
   `attention.update {state: "gone"}`. What was posted where is saved, so a
   restart does not ring everything again;
3. **delivers alerts** queued by `attention.send` that the phone has not had.
   A phone that was offline gets them when it is back (an alert no phone took
   within 24 hours is dropped); the delivery time is recorded per phone.

With `attention.devices` empty the plugin makes no calls at all.

All calls to the phone are the hub's own (`system:rook-hub-attention`).

## The contract (phone caps)

`attention.post {id, kind, line, explain, urgency, channel, actions, source, expires_at}` returns `{ok, id}`.

- `id`: string, at most 64 characters. Posting the same id again updates it.
- `kind`: `hold`, `job`, `card`, `ask` (decision cards) or `alert`.
- `line`: one line, at most 140 characters.
- `explain`: two or three plain sentences, at most 600 characters.
- `urgency`: `urgent` or `normal`. `channel`: `urgent`, `family`,
  `approvals`, `digest` or `jobs`.
- `actions`: a subset of `yes`, `no`, `explain` (alerts: `ack`).
- `source`: a short label (the item's speakable name, or who sent the alert).
- `expires_at`: epoch seconds; the phone drops the card after it; 0 is never.
  Decision cards are sent with 0 (no expiry of any kind): the hub withdraws
  them with `gone` once decided elsewhere or their huddle is archived.

`attention.update {id, state, message}` returns `{ok}`. `state` is `done`,
`failed` or `gone`: the outcome after a tap, or that the item was resolved
elsewhere.

`attention.poll {since}` returns `{events, last}`. Each event has `seq` (int,
increasing), `ts` (epoch seconds) and `type`:

- `decision {id, decision: approve|deny, reason?}`: Yes or No tapped;
- `seen {id}`, `opened {id}`, `dismissed {id}`, `acked {id}`;
- `unlocked {}`: the person unlocked the phone;
- `notice {app, category, priority: urgent|family|normal, summary}`: a short
  summary (at most 140 characters) of an important notification from another
  app.

- `reminded {id, outcome, via?, level?}` (0.4.22-beta.3): what became of an
  `attention.remind`; `outcome` is `spoken`, `busy`, `call`, `voice_session`,
  `mic_in_use`, `media`, `speaking`, `unchecked`, `off`, `dnd` (the phone is
  on Do Not Disturb) or `error`; `via`
  `server` (the house voice) or `device`; `level` only two rounded numbers.

`last` is the newest seq on the phone; if it is below what the hub saw, the
app's counter restarted and the hub polls from 0 again.

`attention.remind {id, text, sample_ms=3000, speak_unchecked=false}` (admin,
physical; app 0.4.22-beta.3): returns `{ok, id, accepted}` at once (`accepted`
false with `busy` or `reason` when another reminder runs or the app's switch is
off). The phone then checks the room on the device (calls, its own voice
session, another app recording, media playing, then a few seconds of the
microphone level; nothing is recorded, kept or sent), speaks `text` in the
app's selected voice only if nobody is talking, and reports a `reminded`
event.

`attention.rules_set {rules}`: classification preferences for the phone's
notice filter. The hub calls it only with `attention.push_rules` on, and
derives no rules yet (a stub for the trust map).

Tiers (hub floor, `rook/core/authz.py`): `attention.post` admin (physical),
`attention.update` admin, `attention.poll` admin (sensitive),
`attention.rules_set` admin. Only the hub should post cards: a card's Yes is
relayed as the person's decision, so whoever words the card words the
question. Agents' admin calls are held for the person.

## Cards

A waiting item becomes a card with `id` = the item id, `kind` = the item
kind, `line` = its plain title, `body` = For / Who / I want to / Why / Risk
(rook/hub/plain.py) and `explain` = the technical detail. `urgency` is
`normal`, `actions` `yes`, `no`, `explain`.

**The queue (Bake, 2026-10-23: "stack up these non emergency items").** A
decision card is not an emergency. With `attention.queue` on (the default) it
is posted on the `digest` channel: it shows in the app's list and quietly in
the notification shade, with no heads-up, no sound and no takeover on unlock.
Every app since 0.4.20 treats `digest` that way, so no app update is needed
for the queue itself. Off: the `approvals` channel, which pops up and takes
over on the next unlock, as before. Emergencies are alerts with
`urgency=urgent` (`attention.send`, the phone's own urgent notices): they
still ring at once on the `urgent` channel. Nothing expires.

## Spoken reminders

Instead of each card interrupting, the phone says one short line when it is
due and the room is quiet:

- one card waiting: "Need your input: <its title>" (a held call's title comes
  from its cap and machine only, never its arguments, the same rule as the
  voice summary; so the spoken line can be more general than the card's own
  title, which may say what the command does);
- several: "Hey boss, you have 6 decisions pending."

What counts: everything the approvals list holds (held calls, job holds,
huddle cards and questions, house asks) plus pending trust-map proposals.
Pacing (hub settings, all live): a new card waits `remind_wait_s` (120 s)
first, so one answered quickly elsewhere is never announced and cards that
arrive together are announced together; at least `remind_gap_min` (15) between
reminders; again every `remind_again_min` (60; 0 = only for new cards) while
cards wait, but a repeat only once the phone was unlocked since the last
reminder (never the same line to an empty or sleeping room); `remind_retry_min` (5) after a busy room; nothing during
`quiet_hours` (local time, like `22:00-07:00`; empty by default, Bake will tune
it). Only one phone speaks: the first configured phone that is online and has
`attention.remind`. A reminder the phone doesn't answer within 3 minutes
counts as lost and is tried again.

If the phone can't remind at all (its Spoken reminders switch is off, it
can't check the room, it fails, or it never answers) two times in a row,
cards pop up again: queued cards are posted again on the `approvals` channel
(same id: no new ring, they take over on the next unlock) and new ones arrive
that way, until a reminder is spoken. Repeat pacing uses the time the hub
heard the phone's unlock, never the phone's clock.

The voice is the app's selected voice: the house voice (Chatterbox on the
voice server) when the app has a voice server, else Android's own speech.
The quiet check needs the microphone; if the phone can't check (no
permission, Android silenced background capture), it stays quiet unless
`remind_unchecked` is on.

## Clearing cards by voice

`rook/hub/plugins/home/cardpass.py`. The voice (Haiku, tool-free) routes
`cards` ("clear my cards") and `card {answer: yes|no|skip|explain|repeat|stop|
choose, choice?}`; the hub judges everything:

- **Start:** the routed turn must be the owner's key with a *sure* speaker
  match (`Sessions._owner_sure`, cosine >= 0.45: any match the voice names), and the person must have ticked "May
  relay my approvals" for `voice:house` (Tokens page, a row beside the
  phones). The pass lists the approvals items plus pending trust proposals:
  held calls first (they block someone), then cards, questions, house asks,
  trust proposals; oldest first in each. Dashboard-only items (`relay.FLOOR`,
  `approvals.relay_dashboard_only`) are named and left.
- **Asking:** each card goes to the voice as a `[back end] (cards) ...` push
  whose `for_turn` is a fresh marker; only the voice's reply to that marker
  (not talked over) marks the card as asked.
- **A decision** comes from the *heard transcript* of the person's turn (yes
  and not no; the named choice), never from the voice's account; that turn
  must be the sure owner, in the normal mode (not listen, brainstorm,
  dictation or scribe: those don't decide), after the card was asked, within
  2 minutes. Relayed with `Relay.decide` as `voice:house`, label `voice`,
  `said = {who: owner, quote: their words, session: "voice:house turn <t>
  <time> voiceprint <score> (sure)"}`: recorded like any relay.
- **Strict items** (a trust proposal that isn't strictly stricter, per
  `trust.describe_change`; a held call of kind `trust`): after the yes, the hub
  writes the read-back ("To confirm: <one sentence>. If that's right, say yes,
  <word>.") with a random word from a list of 40; the voice's spoken reply
  must contain the sentence and the word (else asked once more, then the card
  is left); the confirming turn must be the **first** heard turn after the
  read-back, the sure owner, a yes with the word, and not an echo of the
  read-back itself. A trust proposal is then decided with
  `Actor("person", owner, via="voice", evidence=(voiceprint, readback,
  challenge, turn, ...))`, so the trust history keeps the evidence (entries
  set by voice show as unconfirmed on the Trust page until confirmed there).
  A no and a tightening proposal apply on a plain sure answer.
- Steps of one pass are serialized (two racing answers decide once). A hub
  restart ends a pass; the cards are still waiting.
- Residuals: a skip, stop, repeat or explain needs no voiceprint (they decide
  nothing); a recording of the owner saying yes, played right after an
  ordinary (non-strict) card, would count; card text written by agents (a
  question, a proposal's reason) is read by the voice and could try to steer
  how it describes the card, which is why strict items use a hub-written,
  verified read-back.

## The log

Every card queued, reminder spoken or skipped (once per reason until one is
spoken), tap decided, card gone (answered elsewhere or withdrawn) and, with
the voice clearing session, every spoken decision is one row in
`event_log.db` (rook/hub/eventlog.py, in the plugin's data dir): what (a plain
line), why (the summarized reason, never a message's or command's content),
how (who decided, on what), and an internal pointer to the item. Kept 30 days
(Bake: "30 days? Summarize justification for action from event not
details"). `attention.log {limit, since}` reads it. This is stage 1's log of
the events design (Rook knowledge events-consolidation-model-20261023), so the
later event store can take it over.

An item the person must decide on the dashboard (`relay.refusal`: secrets,
tokens, policy, settings, job locks, and `approvals.relay_dashboard_only`) is
posted with `explain` only, and the explanation says to decide it on the
dashboard's Approvals page.

## The consent gate

A tap is relayed with `Relay.decide` exactly like an agent relaying the
person's words:

- `relayer = "device:<worker_id>"`, `relayer_label = "<name> (phone)"`;
- `said = {who: attention.person, quote: "Tapped Yes on the decision card on
  <name>" (or No), session: "device:<worker_id> card <id> <ISO time>"}`.

It counts only when the person ticked **May relay my approvals** for the
phone on the dashboard's Tokens page; the configured phones are listed there,
off by default. Nothing an agent can reach turns it on. The hub only decides
items it posted to that phone and that are still open; a tap on anything else
gets `gone`. Dashboard-only items are refused by `Relay.decide` as for any
relayer.

Outcome: `attention.update done` with the recorded line ("approved by Bake via
pixel (phone): ..."), or `failed` with the reason. A phone that may not relay
yet is told to turn on "May relay my approvals" for it on the Tokens page;
once the person does, the card is posted again so the next tap counts.

## Hub caps (worker `rook`)

- `attention.send {text, title?, urgency=normal, channel=digest, explain?}`
  (write): an alert for the configured phones. `urgency=urgent` uses channel
  `urgent`. Returns the id, `delivered_now`, the phones it reached and the
  ones it waits for.
- `attention.status` (read): per phone: online, last poll, last event, queued
  alerts, open cards, and when the person last saw, opened, dismissed, acked
  or unlocked it, with the recent signals. No answer does not mean unseen.
- `attention.notices {limit}` (read, sensitive): recent notice summaries,
  newest first.
- `attention.log {limit=30, since}` (read): the 30-day log, newest first,
  as readable lines and rows (no internal pointers).

`attention.status` also reports `reminders`: queue and reminders on, the
speaking phone, the last spoken reminder and try, the last reason one wasn't
spoken, and whether quiet hours apply now.

## What leaves the phone

Only:

- tap and read events: `decision` (approve or deny, an optional reason),
  `seen`, `opened`, `dismissed`, `acked`, `unlocked`, each with an id and a
  time;
- `notice` summaries for urgent and family notifications: the app's name, a
  category, the priority and a summary of at most 140 characters.

Never the full text of any notification. The hub keeps the last 500 signals
and the last 500 notices in the plugin's `state.json`.

## Settings

- `attention.devices` (list, empty): phones (worker names or ids). Empty: off.
- `attention.person` ("the person"): the name recorded with each decision.
- `attention.poll_secs` (10): how often the hub checks the phones.
- `attention.push_rules` (off): send `attention.rules_set` (no rules yet).
- `attention.queue` (on): decision cards wait quietly; off pops them up.
- `attention.remind` (on): spoken reminders (needs app 0.4.22-beta.3).
- `attention.remind_wait_s` (120), `remind_gap_min` (15), `remind_again_min`
  (60), `remind_retry_min` (5), `quiet_hours` (""), `remind_unchecked` (off):
  pacing, as above.
