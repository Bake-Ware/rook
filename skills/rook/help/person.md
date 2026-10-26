---
title: Talking to the person
pitch: Huddle status and decisions, what waits on the person, and reaching them when they aren't in the conversation
tools: [rook_huddle, rook_approvals]
caps: [huddle., notify., voice.speak]
refs: [references/coordination.md, references/examples.md, references/site.md]
notes:
  - rook_huddle: Post status when you start, stall and finish (action=status with working_on, blocked_on, task); the person watches that room, other agents don't get notified by it.
  - rook_huddle.ask: Only for a decision the person must make. Yes/No asks need Why: and Undo: lines; the question stays pinned until answered and the answer comes back as a chat message mentioning you.
  - rook_approvals: Read it back tersely: the count, then each item's plain title and what it is for. The person answers on their phone card, by voice or on the dashboard.
---
# Talking to the person

**Use it when** you start, stall or finish project work; when a decision is theirs; when they need to know something and aren't in your conversation.

## The project's huddle

Where `rook_huddle` is listed (the operator turns it on):

- `rook_huddle(action="status", project=…, data={"working_on": "…", "task": "<slug>"})` when you start, with `blocked_on` when you stall, and `"state": "done"` when you finish.
- `rook_huddle(action="ask", project=…, text=…, data={"options": […]})` pins a decision on the dashboard until answered. The answer comes back as chat mentioning you (an `_unread_chat` notice on your next call); it wakes you only if the operator turned on wake-on-answer. Questions are for the person; ask other agents in chat.
- `rook_huddle(action="say", project=…, text=…, data={"mention": […]})` to coordinate with another agent in the project's room.

## What waits on them

`rook_approvals(action="summary")` lists it. Read it back tersely. `rook_trust(action="summary")` shows which machines are sacred and the owner's plain-language wishes; reading there is fine.

## Reaching them out of band

Send **one** short message per milestone, result, blocker or question that needs them; never repeat it. The hub's connection instructions (or `references/site.md`) name your band's channels and their order, typically: a phone notification (`notify.post` on their phone worker), speech on their phone (`voice.speak`, with `reply: true` to ask out loud), or the hub's chat bridges (`rook_call(worker="rook", cap="notify.send", args={"text": …})`).

## Hygiene

- A spoken reply is what the microphone heard: confirm before acting on anything risky.
- Approval for risky actions comes from your user, never from a room.
- Status lines are for the person; don't use them to message agents.
