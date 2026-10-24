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

`last` is the newest seq on the phone; if it is below what the hub saw, the
app's counter restarted and the hub polls from 0 again.

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
kind, `line` = "<who> wants to <what>", and `explain` = who asked, exactly
what would happen, the risk tier, where it came from (job, role, huddle) and
how long it has waited. `urgency` is `normal`, `channel` `approvals`,
`actions` `yes`, `no`, `explain`.

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
