---
id: agents-and-mcp
title: Connecting AI agents
order: 13
features:
  - features/mcp
  - features/agent-instructions
  - plugin/hub/persona
  - plugin/worker/persona
  - cli/skill
  - cli/mcp
since: 0.1.0
deprecated: false
---
# Connecting AI agents

Rook is built so AI agents (Claude Code, Codex, a local model, a voice
assistant) can help you look after your machines, with you watching and in
charge. Agents connect over **MCP**, a standard way for AI tools to use other
programs. Everything an agent does through Rook is recorded under its own name.

## Give an agent its own token

1. Open **API tokens** (under Manage).
2. Create a token for the agent and give it a clear name, such as
   `claude-laptop`. Add a picture if you like; it shows in chat.
3. Copy the token now: it is shown only once.
4. Add Rook to the agent with the hub's MCP address, `https://<your-hub>/mcp`,
   and the token. For Claude Code:

```sh
claude mcp add --transport http rook https://<your-hub>/mcp \
  --header "Authorization: Bearer <token>"
```

Then ask it something like "list my Rook workers and check the disk space on
the NAS". Revoke a token on the same page to cut an agent off at once.

A token also lets you ask the home agent from a worker machine's terminal:
save it there with `rook login --token`, then use `rook ask` or `rook chat`
([[manual-worker-commands]]).

## What agents can do

- See the machines and their capabilities, and use them (`rook_workers`,
  `rook_caps`, `rook_call`).
- Run long jobs in Rook terminals they can come back to (`rook_console_*`).
- Talk in chat rooms (`rook_chat_*`), and wake other agents.
- Use the task board and leave handoffs (`rook_task`, `rook_handoff_*`),
  see [[manual-work]].
- Read and write the knowledge wiki (`rook_knowledge`), see
  [[manual-knowledge]]. The Rook manual is there too.
- Create and run jobs (`rook_jobs`), see [[manual-jobs]].
- Use secrets without seeing them (`rook_secret`), see [[manual-secrets]].
- Look back at what happened (`rook_journal`, every call with its answer).

## When a call waits for you

An agent's admin or destructive calls, and calls that reach many machines in a
short time, wait for your approval on the **Approvals** page. If you don't
answer within 45 seconds the agent gets a "held for approval" reply with an
id; it should tell you, and repeat the same call once you have approved it.
See [[manual-accounts-and-access]] for how approving works, exceptions for a
trusted agent, and how to turn it off.

An agent can also check in with you: `rook_approvals` (action `summary` or
`list`) tells it everything waiting for you (held calls, held job calls,
huddle cards, pinned questions), each with a name you can say. If you
switch on **Relays** for its token on the **Tokens** page, it can approve or
deny an item when you tell it to, quoting your words; your words and the
conversation are recorded beside the decision. Without that switch, it can
only tell you to use the Approvals page.

## The Rook skill

`rook skill` installs Rook's instructions for agents (a "skill") into Claude
Code or Codex, so they know how to use Rook well from the start. The hub also
serves the skill at `/skill/`.

## Agent instructions

**Agent instructions** (under Manage, operator only) lets you change what Rook
tells agents without installing anything: the message every agent gets when it
connects, the reminder sent when a claimed task goes quiet, and short tips
attached to a tool or to a capability's answers. Every entry keeps its history
and can be reset to the default.

## Persona

A **persona** describes how your agents should come across: a name, a tone,
rules, things to do and not do, and formatting. Write it once on
**Settings > Persona** and every agent that works through Rook receives it,
whatever program it runs in. You can give different personas to different
people, agent families or bands. Rook ships no persona text of its own.

## Running the agent connection on its own

The agent connection normally runs inside the hub. `rook mcp` starts it as a
separate program; that is only for older two-program setups
([[manual-running-a-hub]]).
