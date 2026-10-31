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
  Give it an `mcp_config` setting pointing at the hub's MCP door if the
  agents should reach the band themselves; without it their turns have no
  Rook tools.
- or `agents-codex` on a worker that has Codex installed and signed in
  (`codex login`): turns run `codex exec` under that ChatGPT subscription
  ([its manual](../../rook/plugins/agents-codex/docs/manual.md)). Its
  `config` setting points Codex at the MCP door the same way.
- `llm-cli`, `llm-anthropic` or `llm-openai` if other plugins need plain
  completions (`llm.complete`); agents themselves think through
  `agent.backend`.

## The ready-made agents

| Agent | Role |
|---|---|
| `house` | your personal assistant and the guardian of the house; knows how and when to reach you. Always on |
| `pm` | project manager: sweeps the work, organizes, reports, plans |
| `lead` | lead developer: design calls, branches, reviews, debt |
| `qa` | evidence that things work |
| `writer` | docs true to what shipped |
| `archivist` | keeps the wiki honest |
| `producer` | gets releases out the door |
| `security` | triages security events, proposes safer ways. Always on |

Every agent knows the others and is told that pulling one in is expected.
Agents that need you go through the house agent.

## Talking to an agent

- In the dashboard: the chat dock beside every page talks to the house
  agent (the `pages.dock` setting picks another; blank turns it off).
- As a call: `agents.chat.send {agent: "house", text: "..."}` on worker
  `rook` waits for the reply; `agents.chat.read {agent, after}` reads your
  session with it (pass `next` back as `after`). A first turn takes about
  10 seconds.
- On Telegram or Discord: link your account once (`telegram.link` or
  `discord.link`, then send the code to the bot), and a direct message to
  the bot goes to the house agent in your session with it; the bot sends
  the reply. The `agents.chat_agent` setting picks another agent; blank
  turns it off.

Each person has their own session with an agent, kept and resumed next
time; nobody else sees it. An agent working for you can do only what both
of you may, and every call it makes is journaled with its name and yours.

## Awake, asleep, dreaming

An agent wakes when addressed and stays awake for `warm` (10 minutes) after
its last message. Always-on agents never sleep on idle. While it works an
agent jots decisions and lessons on its notepad (`agents.note`); its memory
is read-only then. When it sleeps, a **dream cycle** on a light model (the
backend's System Three: Haiku for Claude Code) turns the notepad into
lasting memory and tidies both; always-on agents dream every 6 hours (`dream_every`).

## Your own agents and personas

`agents.create {name, persona?, prompt?, grants?, skills?, backend?, model?}`
makes an agent from a persona's defaults and your changes;
`agents.update` changes one (your edits win over later persona updates);
`agents.archive` stops one and keeps everything. A persona's **grants** are
its own rights: unattended, an agent can do only those. Skills are markdown
playbooks an agent follows (`agents.skill {name}` reads one); plugins ship
personas and skills in their manifest.

The full cap list and settings: [the agents plugin's manual](../../rook/plugins/agents/docs/manual.md).
