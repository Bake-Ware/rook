---
id: home-agent
title: The home agent
order: 14
features:
  - plugin/hub/home
  - settings/home
  - plugin/hub/decide
  - settings/decide
  - plugin/worker/agenthost
  - settings/agenthost
  - plugin/hub/roles
  - settings/role
since: 0.1.0
deprecated: false
---
# The home agent

The **home agent** is an AI assistant that lives on the hub itself. It answers
when you mention it in a chat room and when other parts of Rook ask it a
question. It uses a language model you choose: any service that speaks the
OpenAI-style API, including one you run at home.

## Setting it up

Open **Home agent** (under Manage).

1. Switch it on (**Enabled**).
2. Give it a **Name**. People write `@name` in chat to talk to it.
3. Fill in the model: the service's **Base URL**, the **Model** name and, if the
   service needs one, an **API key**. Store the key under Secrets and enter its
   name; the page never shows the key itself. Or pick a saved **Model
   provider** instead, and its address and key are used
   ([[manual-model-providers]]).
4. Optionally choose a **Persona** ([[manual-agents-and-mcp]]) and add your own
   instructions.

The page also shows its recent activity, so you can see what it was asked and
what it said. With **Read-only tools** on, it can search the knowledge wiki
before answering. Jobs can use it as a step too ([[manual-jobs]]).

It remembers people, facts, earlier conversations and skills between
conversations; see [[manual-home-agent-memory]].

## Running it on Claude Code, Codex or OpenCode

The home agent can also run on an agent harness you already use: Claude Code,
Codex or OpenCode, on a machine you choose (the **agent host**), with that
machine's own sign-in (your Claude or ChatGPT subscription, or OpenCode's
model providers). The hub keeps its memory, persona and tools; each answer is
one run of the harness on that machine.

1. On the machine, install the harness and sign in (`claude` then `/login`;
   `codex login`; or set up a provider in OpenCode). A worker with a recent
   build offers itself as a host as soon as it finds one installed; nothing
   runs there until you pick it. To keep a machine out, switch **House agent
   host** off for it under **Settings > Workers**.
2. On **Home agent**, set **Backend** to *Claude Code*, *Codex* or *OpenCode on
   an agent host*.
3. Pick the **Agent host** (the list shows each machine's harnesses, their
   versions and whether they are signed in) and a **Model**: **List models**
   asks that machine. For Claude Code, `haiku` is quick and cheap; `sonnet`
   and `opus` think harder. Blank uses the harness's own default.
   **Test** shows the answer, the host and the time.
4. Optionally list **Fallback hosts**, and choose what happens when no host is
   available: answer with the OpenAI-style endpoint below, or say it is
   unavailable.

To move the agent, pick another host. Nothing is lost: memory stays on the
hub. Usage counts against the sign-in on the host (for Claude Code, optionally
a token from `claude setup-token` saved in Secrets: **Sign-in**). OpenCode's
free models refuse to run with a custom agent, so use one of your own
providers there.

The harness runs with its own tools, settings, plugins and MCP servers
switched off; the only tools it gets are the home agent's, served by the hub
for that one answer.

## Roles (preview)

Roles are team members the home agent will chair in huddles: a project
manager, a QA engineer, a lead developer, or any role you add. Each role has
a charter (what it is for), the things it watches, the tools it may use, and
its own memory. Roles run on an agent harness (Claude Code, Codex or
OpenCode), on the home agent's host unless you give the role its own.

Each role gets its own folder on its host, like a home folder: its memory
(one small text file per thing it remembers) and the harness settings that
say exactly which tools it may use. Every answer the role gives runs inside
that folder. To move a role to another machine, Rook moves the folder; the old
copy is renamed and kept, never deleted.

There is no Roles page yet. Until there is, agents and scripts manage roles
through worker `rook`: `role.presets` shows the starting roster, `role.set`
saves a role (admin), `role.ask` asks one a question, `role.memory` shows what
it remembers and `role.move` moves it (admin). **Tool calls per role turn**
(Settings, `role.max_tool_calls`) bounds one answer; **Role homes directory**
(per worker, `agenthost.roles_dir`) says where a host keeps the folders. A role that only proposes (the
default) can read with its tools but not change anything; it tells you what
it would do instead.

## Decide (preview)

The **Decide** plugin connects Rook to a fast decision model: a model that
answers a batch of multiple-choice questions about one screen in a single
step. With it, an agent can hand over fiddly screen work ("click through this
installer") and Decide drives the mouse and keyboard, stopping to ask when it
is unsure or when an action looks irreversible. It is a preview, off by
default, and its **Kill switch** and **Dry run** settings let you stop it or
watch without acting. Its settings are on the Settings page under
**Decide (decision model)**.
