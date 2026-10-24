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

## Waiting quietly, reminded out loud

Decision cards aren't emergencies, so they don't pop up one by one. They wait
quietly in the app's Attention tab (and in the notification shade, without a
sound), and the phone tells you out loud when something is waiting:

- one card: "Need your input: Deploy hub update?";
- several: "Hey boss, you have 6 decisions pending."

It speaks only when the room is quiet: before each reminder the app listens
for a few seconds to check nobody is talking. The sound stays on the phone:
nothing is recorded, kept or sent. If someone is talking, you're on a call,
music is playing or you're talking to the voice, it tries again a few minutes
later. It stays quiet while the phone is on Do Not Disturb, and it repeats a
reminder only after you have used the phone since the last one. A card you
answer quickly somewhere else is never announced. If the phone can't remind you (its switch is
off, or it can't use the microphone), cards pop up the usual way again.

You can tune it in Settings > Plugins > Attention: **Queue decisions** (off
brings back a pop-up for every card), **Spoken reminders**, and under
advanced the wait before a new card is announced (2 minutes), the time
between reminders (15 minutes), how often to remind again while cards wait
(every hour), and **No reminders between** (for example 22:00-07:00; empty
means any time). The app's Attention tab has its own **Spoken reminders**
switch and the microphone permission it needs. Spoken reminders need app
0.4.22-beta.3 or later; with an older app cards still wait quietly.

Urgent alerts are different: they still ring at once.

## Clearing your cards by voice

Say "clear my cards" (or "what do you need from me") to the voice. It reads
the waiting cards one at a time, the ones blocking work first, then the
oldest, and you answer each in a few words: "yes, approve it", "no, not now",
"skip", "explain" (tell me more), "repeat" or "stop". Each answer is recorded
exactly like a tap ("approved by Bake via voice"), and the card disappears
from the phone, the Approvals page and its huddle. A card answered somewhere
else meanwhile is passed over.

- **Only you, recognised for sure.** A spoken answer counts only when the
  voice is sure it is your voice (a strong voiceprint match, not "probably").
  Otherwise it says it couldn't confirm it was you and leaves the card. A
  guest, a child or a voice it doesn't know can't start this or answer.
  Answer in a few words: a bare "yes" can be too short to recognise.
- **Turn it on once:** on the **Tokens** page, tick **May relay my approvals**
  on the row "<assistant>'s voice (your voiceprint)". Until then the voice
  says it's off.
- **Loosening the rules is read back.** For a trust-map change that loosens
  the rules (accepting an exception, lowering a level, sacred entries
  included) and for a held call on something sacred, after your yes the
  voice reads the exact change back in one sentence and asks you to say "yes"
  and a check word ("yes, mango"). Only your very next words count, and only
  in your recognised voice: an old recording of a yes doesn't know the word.
  Your words, the voiceprint score and the read-back go into the trust map's
  history. A change it can't read back word for word stays on its card.
  Making the rules stricter, and any "no", happen at once.
- **Some things stay on the dashboard.** Cards about secrets, tokens,
  settings or who may do what are named and left for the dashboard; the voice
  says so.
- Nothing is decided from active listening, dictation or the scribe: only in
  the normal conversation.

## The log

Every card, reminder and decision (on the phone, by voice) is written as one plain line (what
happened, why, and how it was decided), kept for 30 days. Agents read it with
`attention.log`. It keeps the reason, never the content: no message text and
no command.

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
