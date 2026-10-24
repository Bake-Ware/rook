---
id: phone-attention
title: Decisions and alerts on your phone
order: 17
features:
  - plugin/hub/attention
  - settings/attention
since: 0.2.0
deprecated: false
---
# Decisions and alerts on your phone

The Rook app on your phone is the one app Rook may interrupt you with. When
something waits on you (a held command, a held job step, a huddle card, a
question an agent pinned), the hub puts a **decision card** on your phone
with Yes, No and Explain. When an agent has to tell you something now, it
sends an **alert**. The hub also notices whether you saw it, so "no answer"
is not taken for "not seen".

## Turning it on

1. Install the Rook app on your phone ([[manual-phones-and-tablets]]).
2. In Settings > Plugins > Attention, set **Phones** (`attention.devices`)
   to your phone's worker name, and **Your name** (`attention.person`) to
   your name. Nothing is sent while Phones is empty.
3. On the **Tokens** page, your phone now has a row named "<phone> (phone)".
   Tick **May relay my approvals** for it. Until you do, cards still show
   up, but tapping Yes or No does nothing except tell you to tick that box.

## Decision cards

Each card reads the same way, in plain words:

- **Title**: a few words, such as "Read approvals database" or "Restart nginx".
- **For**: the task, project, huddle or job it serves ("no task" when the agent
  did not say).
- **Who**: the agent, role or job asking.
- **I want to**: one sentence of what will happen, such as "run a read-only
  query on the approvals database on bakenetcanada".
- **Why**: the agent's own reason, when it gave one.
- **Risk**: Low, Medium or High and why, such as "Low: looks read-only, but
  rook-hub is guarded, so changes there ask you".

**Explain** shows the technical detail: the capability, the command or
arguments, the trust map entry that held it, and the item's id and its
three-word name. The dashboard's Approvals page shows the same. (An app
older than 0.4.22 shows the title and what it is for, with the rest under
Explain.) Tapping Yes or No decides it, exactly as if you clicked it on the
Approvals page, and the item shows "approved by <you> via <phone> (phone)"
with the time you tapped.

Some things are never decided from a phone: secrets, tokens, policy,
settings, job locks and anything on the dashboard-only list (Settings >
Approvals). Their cards only have Explain, which says to decide them on the
dashboard. When something is decided elsewhere, its card disappears.

## Alerts

Agents send alerts with `attention.send` (on the worker `rook`). An urgent
alert uses the phone's urgent channel. If your phone is off or out of
reach, the alert waits and arrives when it is back (for up to a day).

## What the hub learns from your phone

Only your taps and when you saw, opened, dismissed or acknowledged a card or
unlocked the phone, plus a short summary (at most 140 characters) of urgent
and family notifications from other apps. The full text of a notification
never leaves the phone. Agents can read these with `attention.status` (did
you see it?) and `attention.notices` (recent summaries).
