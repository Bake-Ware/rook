---
id: home-agent-memory
title: What the home agent remembers
order: 14
features:
  - features/home-agent-memory
  - plugin/hub/memory
  - settings/memory
since: 0.1.0
deprecated: false
---
# What the home agent remembers

The home agent ([[manual-home-agent]]) remembers between conversations. Tell
it your name once, or that the bins go out on Tuesday, and it still knows next
week. Memory is on by default; you can see and change everything it
remembers under **Manage > Memory**.

Memory is not the knowledge wiki ([[manual-knowledge]]). The wiki holds pages
your agents write with evidence and you verify. Memory holds short notes the
home agent jots down as it goes, and notes it doesn't use fade over time.

## What it keeps

- **About people**: names and preferences people tell it, such as "call me
  Sam" or "never wake me before 7". Each person's memories are their own.
- **Facts** about your home and setup, shared by everyone.
- **Conversations**: short summaries of earlier chats, combined into one
  summary per week once they are two weeks old.
- **Skills**: ways of doing things that worked, learned from job steps the
  home agent finished with tools ([[manual-jobs]]). It uses a learned skill
  only after you approve it (see below).

## How it learns and recalls

Before it answers, the home agent reads what it knows about the person it is
talking to and whatever seems relevant to the message, within a size limit.
After it answers, it keeps what it learned:

- Say "remember that ..." and it remembers.
- It notices "my name is ...", "call me ...", "I live in ...", "I prefer
  ..." and "from now on ...", and keeps them in plain words ("The user
  prefers metric units.").
- Say "forget that ..." and it forgets and erases the closest matching memory
  about you. Shared facts and skills are only forgotten this way when the
  operator says it.
- When something changes ("actually, I live in Leeds now"), the new memory
  replaces the old one. The old wording is kept in its history, never
  overwritten.

With the home agent's tools on, the model can also save and search memory
itself while it answers. To switch memory off for the home agent, turn off
**Use memory** in its settings (`home.memory`); to stop it picking things up
from chat, turn off **Remember what people tell it** (`memory.auto_remember`).

Every 30 minutes Rook tidies up: it summarises finished conversations, rolls
old ones up by week, merges duplicates, lets unused memories fade and keeps
each kind within a size budget. **Tidy up now** (in the **⋯** menu on the Memory
page) runs it at once.

## Privacy

- **Nobody's private memory goes to someone else.** When an agent talks to
  the home agent, when another part of Rook asks it a question, and in job
  steps, it sees shared memory only: no one's profile and no conversations.
  It keeps nothing from those conversations, except that a job step it
  finished with tools can become a skill waiting for your approval.
- **Another person's memory is the operator's to see.** Anyone allowed to use
  memory reads and changes shared memories and their own. Reading or changing
  someone else's, seeing forgotten memories, adding skills, keeping memories
  from fading and approving skills are for the operator.
- **No secrets.** A value stored under Secrets ([[manual-secrets]]) becomes a
  reference such as `{{secret:guest-wifi}}`. Anything that looks like a
  password, key, token, door or alarm code, wifi password or card number is
  refused, or replaced by `[redacted]` in summaries. Add your own patterns in
  the Memory setting **Never remember text matching** (`memory.deny_patterns`).

## The Memory page

Open **Manage > Memory** (operator account). Filter by kind (about a person,
fact, conversation, skill), search, and pick a memory to see where it came
from, the versions it replaced and its history. You can:

- **Add a memory** (**+ Add**): write one plain statement and choose its kind.
- **Correct** it: change the text and **Save**. The old version is kept.
- **Keep** it: tick **Keep** so it never fades.
- **Approve** a learned skill, or **Withdraw approval**. A skill marked
  "needs your approval" is never read into any prompt until you approve it,
  because skills steer which tools the agent uses. A learned skill keeps the
  job's instructions and the tools that worked, never what the tools
  returned.
- **Forget** it: it is no longer used, and is deleted for good later.
- **Forget and erase**: it is erased at once, with every earlier version of
  it. Only a note that something was erased, and when, is left.

## How long it keeps things

- Replaced, faded and forgotten memories are deleted, with their history,
  after 90 days (setting **Delete replaced and forgotten memories after
  (days)**, `memory.retention_days`; 0 keeps them).
- Conversation turns waiting to be summarised are dropped after a week, and
  at once when automatic remembering (`memory.auto_remember`) is off.
- **Forget and erase** leaves no copy of the text behind in the memory
  database, its search index or its write-ahead log file.

## Search by meaning

Memory always searches by keywords. It can also search by meaning
("beverage" finding a memory about tea) when an embedding model answers: set
`memory.embed_model` to a model on the home agent's own model service, point
`memory.embedder` at an embedding service, or run a worker that offers
`embed.text`. If none answers, memory carries on with keywords.

## For agents

Agents reach memory with `memory.recall`, `memory.remember` and
`memory.forget` on worker `rook`, or with the `rook_memory` tool when **Let
connected agents use memory** (`memory.agent_tool`, off by default) is on. All memory settings are on the
Settings page under **Memory** ([[manual-settings]]).
