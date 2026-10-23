---
id: front-door
title: The house agent as your front door
order: 11
features:
  - settings/home
since: 0.2.0
deprecated: false
preview: true
---
# The house agent as your front door

With the front door on, every request that reaches the house agent goes
through one routine, whether you speak to the phone, type in the web chat
panel, write in a chat room, or a Claude session asks it with
`rook_home_ask`. It works out what you want and takes exactly one path:

- **It answers** questions, chat and status ("what's waiting?", "what did
  you decide for me?").
- **It does it**, when the action is quick, easy to undo and the
  [[manual-trust]] lets agents go there. The reply starts with **Done:** and
  what it did. It only says "Done" when the action really ran.
- **It starts a coding session** for one clear piece of code work (the lead
  developer by default) and tracks it: "I've got the team on it:
  brave-otter-lamp." The session reports back on its own.
- **It brings in the team**: fuzzy or multi-discipline work opens a huddle
  ([[manual-huddles]]) with the roles that fit: "I've got the team on it:
  <huddle name>."

Turn it on with **Front door** on **Manage > Home agent** (next to Enabled).

## When it asks you

It protects your attention: it only asks when

- agents disagree and neither the trust map nor its playbook settles it,
- something is sacred, or guarded (the trust map says ask first),
- an action can't easily be undone (deleting, wiping, powering off), or
- nothing covers it.

An ask is one line with **Yes**, **No** and **Explain**. Explain gives two or
three plain sentences; **More** gives the detail. Asks appear on your
phone's decision card, in `rook_approvals`, and on the dashboard's
**Decisions** page. After you say yes it acts, then asks once whether to
remember it ("Next time just restart pianobar without asking?").

When agents disagree and an entry does settle it (for example, Pianobar is
ship-it), the house agent rules, cites the entry, and logs it without
asking you.

## What it decided for you

**Decisions** lists what the house agent decided without asking: what it
did, the sessions and huddles it started, the disputes it ruled, and the
playbook rules it tightened. Each one shows the entry it relied on.

**Overturn** only makes things stricter: the trust entry it cited goes one
level up (ship-it to guarded, guarded to sacred), or the playbook gets an
"Ask me first" rule. The action itself is not undone; ask the house agent
to undo it if it should be.

By voice or from any client: "what did you decide for me?"

## The playbook

The playbook is the house agent's routing rules, a plain text file you can
read and edit at the bottom of **Decisions**. Its sections are **Just do
it**, **Ask me first**, **Send to a coding session**, **Bring in the team**,
**Just answer** and **Settling disagreements**.

Correct it in conversation:

- "ask me about X" makes it stricter. That applies at once, and you see it
  under Decisions.
- "next time just do that" makes it looser. That comes back to you as a
  one-line ask first.

The trust map always wins: a playbook rule can make the house agent ask
more than the map does, never act where the map says to ask.
