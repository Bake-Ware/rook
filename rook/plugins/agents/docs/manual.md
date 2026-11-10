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
| `security` | Triages security events, proposes safer ways | yes |

Any plugin's manifest can ship personas and skills (`personas:`,
`skills:`). The runtime reads every installed plugin's (`hub.manifests`, at
most a minute old) and makes an agent of each persona, so the workbench's
team roles come with the workbench: `pm`, `lead` and `qa` (plugin `task`),
`writer` and `archivist` (`knowledge`), `producer` (`concept`). Edits to an
agent win over later persona updates. On a persona name two plugins ship,
the first by plugin name wins (this plugin's own come first).

`agent.skill {name}` reads a skill from the manifest that ships it, else
the knowledge page of kind `skill` with that slug (with the workbench).

Every agent knows the others (a roster in its prompt) and is told that
pulling in another agent is expected, not overstepping. Agents that need you
go through the house agent.

## Talking to an agent

- `agent.chat.send {agent, text}` messages it and waits for the reply
  (`wait: false` answers at once). It wakes the agent if it sleeps.
- `agent.chat.read {agent, after}` reads your session with it; pass `next`
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
sleep on idle. `agent.wake` and `agent.sleep` do it by hand.

## Notepad, memory and dreams

While it works, an agent jots what it decides, learns and is told on its
private notepad (`agent.note`). Its memory is read-only while it works. When
it goes to sleep, a **dream cycle** - a light model (the backend's System
Three: Haiku for Claude Code) - turns the notepad into lasting memory and
prunes and corrects both. Always-on agents dream every `dream_every` (6
hours). `agent.dream` runs one now. Memories are private records owners
curate with `agent.memory.list/get/search/create/update/archive/delete`;
the notepad is the agent's own (it sees it in its prompt) and has no caps
to read it. Your sessions with an agent are streams: `stream.list
kind=session` on worker `rook` lists them and `stream.read` reads them.

## Making and changing agents

Agents are records: `agent.list` (with `awake`), `get`, `search`,
`history`, and for admins `create`, `update`, `archive`, `delete` (for good).
`agent.create {name, persona?, ...}` makes one; what you leave out
comes from the persona's defaults (prompt, grants, skills, backend, models
like `model: {two: opus, three: haiku}`). `agent.update {id,
revision, ...}` changes one; once anyone but the plugin has changed an agent,
later persona updates no longer touch it. `agent.archive` stops one and
keeps everything; `restore: true` brings it back. Skills are markdown
playbooks; `agent.skill {name}` reads one.

## Backends

Turns go only through the `agent.backend` contract. An agent with backend
`default` uses the hub's chosen provider; naming one provider's cap
(`agents-claude-code.turn`, `agents-codex.turn`, or any plugin that provides
the contract) pins it there. Each backend has its own System Two and System
Three models (its `model` and `model_three` settings); an agent's
`model: {two, three}` overrides them. Settings: `warm`, `turn_timeout`,
`dream_every`, `chat_agent`.

## On the hub site

The agents package adds the house agent to the hub site itself (`shell:` in
its manifest, files in `shell/`): the chat dock beside every page with
Rookie, its avatar, the work pane and the guided first run, and the look
pickers on Your account. `agent.site` is what the dock needs: the agent in
it (the `dock` setting, `house`; blank: no dock), whether it can answer yet
(a worker serving `agent.backend` is online), your look and feel and
whether first-run setup is done. `agent.setup {done?}` reads or sets that
(kept in the plugin's data as `setup.json`; none yet: not done, so a new
hub with the agents package opens on the guided first run), with `install`,
the facts the installer left there. Guided setup does not keep the site
open: once the owner sets a password, a new browser gets the sign-in page
even while setup carries on.
`agent.look {sound?, rookie?, theme?, name?}` sets the look and feel: sound,
Rookie's style and theme are yours (the per-person settings `sound`,
`rookie`, `rookie_theme`), the name is the house agent's (`house_name`;
blank: Rookie). Blank ones haven't been chosen: first-run setup asks them.

## Identity

Each agent is `agent:<name>` on the hub, with its own grants plus
`agent.backend` (its own turns). A turn runs as the agent for the person
whose message it answers, so it gets only what both may; with no person
behind the message (a persona job, another agent acting alone) it acts
alone, with its own grants. The backend gets a token for that same pair,
good for that turn only (the `door` arg), so every Rook call the agent makes
from its harness is journaled as the agent and checked the same way. Dreams
run as the agent alone, without a token. Archiving an agent takes its
identity off the hub; a name hub.yaml already uses is refused.
