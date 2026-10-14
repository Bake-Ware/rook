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
   name; the page never shows the key itself.
4. Optionally choose a **Persona** ([[manual-agents-and-mcp]]) and add your own
   instructions.

The page also shows its recent activity, so you can see what it was asked and
what it said. With **Read-only tools** on, it can search the knowledge wiki
before answering. Jobs can use it as a step too ([[manual-jobs]]).

## Decide (preview)

The **Decide** plugin connects Rook to a fast decision model: a model that
answers a batch of multiple-choice questions about one screen in a single
step. With it, an agent can hand over fiddly screen work ("click through this
installer") and Decide drives the mouse and keyboard, stopping to ask when it
is unsure or when an action looks irreversible. It is a preview, off by
default, and its **Kill switch** and **Dry run** settings let you stop it or
watch without acting. Its settings are on the Settings page under
**Decide (decision model)**.
