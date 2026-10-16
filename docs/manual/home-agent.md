---
id: home-agent
title: The home agent
order: 14
features:
  - plugin/hub/home
  - settings/home
  - plugin/hub/decide
  - settings/decide
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

## Running it on Claude Code

The home agent can also run on Claude Code, using the Claude subscription of a
machine you choose (the **agent host**). The hub keeps its memory, persona and
tools; each answer is one Claude Code run on that machine.

1. On the machine, install Claude Code and sign in (`claude`, then `/login`).
   Then allow it to host: **Settings > Workers**, that machine, **House agent
   host** on (off by default, so a machine with Claude Code installed is never
   used unless you say so). Its Rook worker needs a build with the
   **agenthost** plugin.
2. On **Home agent**, set **Backend** to *Claude Code on an agent host*.
3. Pick the **Agent host** and a **Model** (`haiku` is quick and cheap; `sonnet`
   and `opus` think harder). **Test** shows the answer, the host and the time.
4. Optionally list **Fallback hosts**, and choose what happens when no host is
   online: answer with the OpenAI-style endpoint below, or say it is
   unavailable.

To move the agent, pick another host. Nothing is lost: memory stays on the
hub. Usage counts against the subscription signed in on the host, or against
a token from `claude setup-token` saved in Secrets (**Sign-in**).

Claude Code runs with its own tools, settings and MCP servers switched off;
the only tools it gets are the home agent's, served by the hub for that one
answer.

## Decide (preview)

The **Decide** plugin connects Rook to a fast decision model: a model that
answers a batch of multiple-choice questions about one screen in a single
step. With it, an agent can hand over fiddly screen work ("click through this
installer") and Decide drives the mouse and keyboard, stopping to ask when it
is unsure or when an action looks irreversible. It is a preview, off by
default, and its **Kill switch** and **Dry run** settings let you stop it or
watch without acting. Its settings are on the Settings page under
**Decide (decision model)**.
