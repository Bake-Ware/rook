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

## Who's talking

With your voice enrolled ("enroll my voice"), the voice knows when it is you
and when it is someone else: each turn says "Bake (sure)", "Robin
(probably)" or "unknown guest". It is recognition, not a password: anything
risky still asks you. Someone it doesn't recognise on your phone is treated
as a guest; a very short "yes" that is too short to recognise counts as you.

**Learning someone else's voice:** say "I want to enroll a new voice" or
"learn Robin's voice". It asks their name and who they are to you. An adult
then says "I agree to have my voice remembered" in their own voice. Only a
voice it doesn't know yet (or already knows as that person) counts: not yours,
not someone else it knows, and not a question or a "no". One yes enrolls one
person. For a child (a kid, son or daughter, as the voice or you put it) it asks
you to confirm and needs your yes out loud; the child's own yes isn't enough. A
bare "yes" can be
too short for it to know it's you: say a few words. Then they
read a few sentences. Only you, recognised by voice, can start this. It keeps
a record of them on the hub (who they are, their consent in their words, their
devices) and a note in its memory; the voiceprint stays on the voice server.
"Forget Robin's voice" deletes the voiceprint (it asks whether to keep the
record). **Settings > Voice > People** in the app lists the voices it knows,
with Forget.

**Ending its own listening:** "that's all", "thanks, bye", "stop listening",
said to it as a goodbye, or "go to sleep for 10 minutes" (it then ignores the
wake word that long: only when you ask for minutes or hours).
Sign-off: **Sign-off when it stops listening** (short or none).

**Taking notes (scribe):** when people talk among themselves and not to it,
it stops replying and writes down who said what (enrolled names, else
"Speaker A", "Speaker B"), only while you are one of them: if you aren't, or
once it hasn't heard you for 10 minutes, it stops recording, just stops
replying and waits for its name. The phone shows
"taking notes" while it lasts. Now and then the agent reads the new lines and
files tasks it implies (proposed as usual, never sent to anyone), takeaways
and decisions with the time they were said, on a knowledge page "Conversation
<date time>". Saying its name, or five quiet minutes, ends it. Transcripts are
deleted after **Keep scribed transcripts** (30 days); the takeaways stay;
audio is never kept. **Always ask before taking notes** makes it ask first.

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

This only ever happens on the phone set as **Owner's phone for the voice's
brief**, never a family device, and only after you tick **Let the assistant act
for me on this phone** in that phone's app (Settings > App; off by default).
A notification's own buttons count as "asks first" unless the button is a
harmless one named outright (Mark as read, Dismiss, Snooze). The app needs
Calendar access (Settings > Permissions) to add or change events.

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
