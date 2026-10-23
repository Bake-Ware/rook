---
id: persistent-agents
title: Always-on house agent and its voice
order: 12
features:
  - settings/home
since: 0.3.1
deprecated: false
preview: true
---
# Always-on house agent and its voice

Normally the house agent starts fresh for every request: the hub sends the
context, the agent answers, and the run ends. With **Always-on sessions** on,
it is one Claude Code conversation that keeps going, plus a fast voice that
talks for it:

- **The agent** runs on Opus in its own directory on the machine with your
  Claude login. Its persona and rules are in `CLAUDE.md` there, its model and
  permissions in `.claude/settings.json`, and its tools in `.mcp.json`, the
  same files Claude Code reads anywhere.
- **The voice** runs on Haiku 5.5 at low effort, in `voice/` inside that
  directory. It inherits the persona. It answers you at once and hands real
  work to the agent; it never claims something is done until the agent says so.
  It has no tools at all (tools make Haiku 5.5 think before it speaks): it ends
  what it says with a short hidden line that tells the hub what to do, and that
  line is never spoken or shown.

To you they are one assistant, under the name in **Manage > Voice > Speech >
Assistant name** (for example Sojourn; **Whose assistant** there is your name). It
doesn't talk about how it works inside, and it doesn't say it can't find
something out: it says it's checking and gets the answer. The voice also gets a
short brief kept fresh by the hub (what the agent is working on, what waits on
you, recent decisions, how much of the band is online) and the time, so common
questions need no lookup.

Both keep their identity: if the machine or the hub restarts, they resume the
same conversation. If a conversation can't be resumed, it starts fresh and the
**Home agent** page says so. When the conversation gets long, the agent first
saves what is worth keeping to its memory, then compacts it.

## The inbox

Everything for the agent queues up in its inbox: what you say to the phone,
the web chat, Telegram, other agents, huddles, and reports from sessions and
jobs. You come first (by voice, ahead of anything else), then huddles, then
background items. Reports that arrive while it is busy are passed on together
on its next turn ("While you were busy: ...").

The **Home agent** page shows one line per agent, for example
`house agent: busy, 2 waiting, 1 fork`.

## What the voice can do while the agent works

You can keep talking while the agent is busy. The voice can:

- **add to what it's doing**: "only ones near downtown" (or "stop, change of
  plan");
- **ask where things stand**: "where are we at on that?" gets an answer from
  the agent's own context without interrupting it;
- **start a side thread** for something separate, which comes back and is
  folded into the agent's conversation when it is done;
- **queue, pause, resume or cancel** work;
- **change how it listens**: "switch to dictation" (it only writes down what
  you say until you say "I'm done", then offers to save or send it), "active
  listening" (it listens and only speaks when you say its name or something
  important comes up), "brainstorm" (mostly listens, asks a short question at
  a pause and keeps notes), and "back to normal". The phone shows the mode it
  switched to. The voice never switches into or out of the children's
  conversation mode or roleplay.

## Voice models

What the voice and the agent run on is yours to change, live, without a
restart:

- **On the phone:** Settings > Voice > **Voice models** (with the fast voice
  on): the voice (Haiku 5.5, Haiku 4.5, Sonnet, or **Local** on kaiju's own
  models) and the agent (Opus, Sonnet, Haiku), each with an effort and a
  one-line note. **Apply** changes it for every device.
- **By voice:** "switch your back end to Sonnet", "use the local model for
  the voice". It asks first ("Switch the agent to Sonnet?") and changes it
  after you say yes. Only the owner can.
- **Local** hands the voice to the voice server's own path (voice-front, with
  voice-back behind it): no Claude, no web search, nothing routed to the agent.
  It is the hub setting **Voice engine** (`home.voice_engine`), which the voice
  server reads for each connection, so nothing changes on kaiju.

Every change is recorded (`home.voice_models_set`, who and how).

## Your phone

With **Owner's phone for the voice's brief** set (e.g. `bakephone`), the
voice knows your battery, the next things on your calendar and your important
notifications without looking them up (read every few minutes; nothing is
changed). The agent can also act on that phone for you:

- **Just does it:** reads, opening an app or a link, adding or changing your
  own calendar events, dismissing a notification.
- **Does it, then tells you** (in the decisions it made for you): dismissing
  notifications, reminders.
- **Asks first** (one line, Yes / No / Explain): anything that speaks for you
  to someone else (a text, an email, replying to a notification) and deleting
  something, such as a calendar event.

Family devices are never offered. The app needs Calendar access (Settings >
Permissions) to add or change events.

**Expressive voice** (Settings > Voice > Behaviour, on by default): with a
Chatterbox voice, the voice may now and then laugh, chuckle or sigh. The
screen shows it as "(laughs)". Off, or with another voice, it never does.

## Web search

The agent can search the web and read pages (**Web search** in **Manage > Home
agent > Persistent sessions**, on by default). Ask the voice anything current
("are any coffee shops open at midnight near me?", "what's the weather this
weekend"): it says it's looking, the agent searches, and the voice tells you
the answer. The voice itself never searches. Another role gets web search only
when you grant it, like an MCP server: "give our researcher web search".

## Giving a role more tools

Ask the house agent, for example "give our reverse engineering technician the
REA MCP". It adds that MCP server to the role's `.mcp.json` and reloads its
session. Granting a tool goes on the [[manual-trust]] as a tool grant: the
house agent does it only when you ask for it yourself. If another agent asks,
it waits for you on the Trust page.

## Turning it on and off

Turn on **Always-on sessions** in **Manage > Home agent > Persistent
sessions**. It needs the session host running on the machine with the Claude
login. Turn it off and every request is a one-off run again. If the session
host is not connected, requests use one-off runs automatically. The voice
server keeps its old voice path as a fallback, a single setting on its host.
**Voice model** picks the voice's model: `claude-haiku-5-5` (default) or
`claude-haiku-4-5`, about 0.3 s quicker to the first word and never thinking,
a little less capable.
See [[manual-voice]] and [[manual-front-door]].
