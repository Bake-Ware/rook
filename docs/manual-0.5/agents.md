# Agents and personas

Rook works with two kinds of agents:

- **Your own agents** (Claude Code, Codex, Hermes, a local model) reach the
  band over MCP with a token of their own ([MCP for agents](mcp.md)).
- **The hub's agents** live on the hub, from the `agents` package: the
  house agent and a small team of specialists you talk to from the
  dashboard or over MCP.

Every hub agent is the same thing: a **persona** (what it is and how it
works), a **backend** (what it thinks with), a **session** with each person
it talks to, a private **notepad** and a **memory**. The house agent is just
one agent with a broader persona.

## Setting it up

Put `agents` in the hub's library, and a backend:

- `agents-claude-code` on a worker that has Claude Code installed and
  signed in: turns run `claude -p` under that subscription
  ([its manual](../../rook/plugins/agents-claude-code/docs/manual.md)).
  Each turn reaches the hub's MCP door (its `door_url` setting, the local
  door by default) with a token made for that turn only, so the agent acts
  on the band as itself.
- or `agents-codex` on a worker that has Codex installed and signed in
  (`codex login`): turns run `codex exec` under that ChatGPT subscription
  ([its manual](../../rook/plugins/agents-codex/docs/manual.md)), reaching
  the door the same way with each turn's own token.
- `llm-cli`, `llm-anthropic` or `llm-openai` if other plugins need plain
  completions (`llm.complete`); agents themselves think through
  `agent.backend`.

## The ready-made agents

The agents package makes these:

| Agent | Role |
|---|---|
| `house` | your personal assistant and the guardian of the house; knows how and when to reach you, and offers to set up a project when something has become real work. Always on |
| `security` | triages security events, proposes safer ways. Always on |

With the workbench installed, its team roles come too (the workbench's
plugins ship them as personas):

| Agent | Role | Shipped by |
|---|---|---|
| `pm` | project manager: sweeps the work, organizes, reports, plans | `task` |
| `lead` | lead developer: design calls, branches, reviews, debt | `task` |
| `qa` | evidence that things work | `task` |
| `writer` | docs true to what shipped | `knowledge` |
| `archivist` | sweeps the wiki like a diligent Wikipedia editor: sources, duplicates, stale pages | `knowledge` |
| `producer` | sets up a new kind of work the first time it becomes real: asks how you like to work, researches how it is done, writes the concept | `concept` |

Every agent knows the others and is told that pulling one in is expected.
Agents that need you go through the house agent.

## Talking to an agent

- In the dashboard: the chat dock beside every page talks to the house
  agent (the `page.dock` setting picks another; blank turns it off).
- As a call: `agent.chat.send {agent: "house", text: "..."}` on worker
  `rook` waits for the reply; `agent.chat.read {agent, after}` reads your
  session with it (pass `next` back as `after`). A first turn takes about
  10 seconds.
- On Telegram or Discord: link your account once (`telegram.link` or
  `discord.link`, then send the code to the bot), and a direct message to
  the bot goes to the house agent in your session with it; the bot sends
  the reply. The `agents.chat_agent` setting picks another agent; blank
  turns it off.

Each person has their own session with an agent, kept and resumed next
time; nobody else sees it. Every hub agent is an identity of its own on the
hub (`agent:pm`). Answering you, it can do only what both of you may; acting
alone (a persona job, another agent asking with nobody behind it) only what
its own grants allow. Every call it makes is journaled with its name, and
yours when it works for you.

## Awake, asleep, dreaming

An agent wakes when addressed and stays awake for `warm` (10 minutes) after
its last message. Always-on agents never sleep on idle. While it works an
agent jots decisions and lessons on its notepad (`agent.note`); its memory
is read-only then. When it sleeps, a **dream cycle** on a light model (the
backend's System Three: Haiku for Claude Code) turns the notepad into
lasting memory and tidies both; always-on agents dream every 6 hours (`dream_every`).

## Your own agents and personas

Agents are records on the hub: `agent.list` shows them (and who is
awake). `agent.create {name, persona?, prompt?, grants?, skills?,
backend?, model?}` makes one; what you leave out comes from the persona.
`agent.update {id, revision, ...}` changes one (your edits win over
later persona updates); `agent.archive` stops one and keeps
everything (and takes its identity off the hub until you restore it).
Owners curate an agent's memory with `agent.memory.*`. A persona's
**grants** are its own rights, checked on every call it makes; every agent
may also run its own turns (`agent.backend`). `backend` is `default` (the
hub's chosen provider) or one provider's cap, e.g.
`agents-claude-code.turn`. Skills are markdown playbooks an agent follows
(`agent.skill {name}` reads one). Any plugin can ship personas and skills
in its manifest; each installed plugin's personas become agents. With the
workbench, a knowledge page of kind `skill` is a skill too (the skill
library lives in the knowledge store), so a concept's skills can be
written as pages.

The full cap list and settings: [the agents plugin's manual](../../rook/plugins/agents/docs/manual.md).
