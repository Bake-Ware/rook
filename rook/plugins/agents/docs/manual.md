# Agents

Every agent on Rook is the same thing: a **persona** (what it is and how it
works), a **backend** (what it thinks with: Claude Code or Codex), a
**session** with each person or agent it talks to, a private **notepad**
and a **memory**. The house agent is just one agent with a broader persona;
there is no special one.

## Ready-made roles

Installing the agents package makes these, each with its skills:

| Agent | Role | Always on |
|---|---|---|
| `house` | Your personal assistant and guardian of the house; knows how and when to reach you | yes |
| `pm` | Project manager: sweeps the work, organizes, reports, plans | |
| `lead` | Lead developer: design calls, branches, reviews, debt | |
| `qa` | QA engineer: evidence that things work | |
| `writer` | Technical writer: docs true to what shipped | |
| `archivist` | Keeps the wiki honest | |
| `producer` | Gets releases out the door | |
| `security` | Triages security events, proposes safer ways | yes |

Every agent knows the others (a roster in its prompt) and is told that
pulling in another agent is expected, not overstepping. Agents that need you
go through the house agent.

## Talking to an agent

- `agents.chat.send {agent, text}` messages it and waits for the reply
  (`wait: false` answers at once). It wakes the agent if it sleeps.
- `agents.chat.read {agent, after}` reads your session with it; pass `next`
  back as `after`.
- A direct message to the band's Telegram or Discord bot, once you linked
  your account (`telegram.link`, `discord.link`), goes to the `chat_agent`
  (the house agent by default) in your session with it, as you; the reply
  comes back from the bot. Group and channel messages, and people who
  haven't linked, are not answered (they stay events for jobs). Blank
  `chat_agent` turns this off.

Each person (and each agent) has their own session with an agent, kept for
good and resumed next time. A guest never sees your conversation. When an
agent works for you it can do only what both of you may.

## Awake and asleep

An agent wakes when addressed and stays warm for `warm` (10 minutes) after
its last message, then sleeps. Always-on agents (`house`, `security`) never
sleep on idle. `agents.wake` and `agents.sleep` do it by hand.

## Notepad, memory and dreams

While it works, an agent jots what it decides, learns and is told on its
private notepad (`agents.note`). Its memory is read-only while it works. When
it goes to sleep, a **dream cycle** - a light model (the backend's System
Three: Haiku for Claude Code) - turns the notepad into lasting memory and
prunes and corrects both. Always-on agents dream every `dream_every` (6 hours). `agents.dream`
runs one now. `agents.get` shows memory and notepad to the agent itself and
to owners.

## Making and changing agents

`agents.create {name, persona?, ...}` makes one, from a persona's defaults
and/or your own prompt, grants, skills, backend and models
(`model: {two: opus, three: haiku}`). `agents.update` changes one; your edits
win over later persona updates. `agents.archive` stops one and keeps
everything; `restore: true` brings it back. Skills are markdown playbooks;
`agents.skill {name}` reads one.

## Backends

An agent with backend `default` uses the hub's chosen `agent.backend`
provider; `claude-code` or `codex` pins it to that backend plugin
(`agents-claude-code`, `agents-codex`). Each backend has its own System Two
and System Three models (its `model` and `model_three` settings); an agent's
`model: {two, three}` overrides them. Settings: `warm`, `turn_timeout`,
`dream_every`, `chat_agent`.
