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
- **The voice** runs on Haiku with thinking held off, in `voice/` inside that
  directory. It inherits the persona. It answers you at once and hands real
  work to the agent; it never claims something is done until the agent says so.

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
- **queue, pause, resume or cancel** work.

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
See [[manual-voice]] and [[manual-front-door]].
