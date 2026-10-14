---
id: model-providers
title: Model providers and llmanifold
order: 14
features:
  - features/model-providers
  - plugin/hub/llm
  - settings/llm
  - plugin/hub/llmanifold
  - settings/llmanifold
since: 0.1.0
deprecated: false
---
# Model providers and llmanifold

A **model provider** is a language model service your hub can use, saved
once under a short name. Instead of typing the same address and key into
every place that needs a model, you add the service here and pick it by name.
Any service that speaks the OpenAI-style API works: llama.cpp, vLLM,
LM Studio, Ollama, or a paid API.

Today the home agent ([[manual-home-agent]]) can use a provider by name, and
so does everything that uses the home agent's model, such as its memory
([[manual-home-agent-memory]]).

## Adding a provider

Open **Manage > Model providers** (operator account) and choose **Add a
provider**.

1. Give it an id: lowercase letters, digits, `-` or `_` (for example
   `lab-gpu`). This is the name other pages use. A **Label** is optional.
2. Enter its **Address**, the service's OpenAI-style base address, for
   example `http://llm.example:8080/v1`. Do not put a user name or password
   in the address: it is refused.
3. If the service needs a key, store it under Secrets ([[manual-secrets]])
   and choose that secret as its **API key**. The page never shows the key
   itself.
4. Optionally fill in **Models to offer** and a **Default model**. Leave
   the list empty to offer whatever the service lists.
5. **Save**.

**Check** asks the provider whether it answers and how quickly, and **List
models** shows what it offers right now. The page also shows which parts of
Rook use each provider. You can have up to 32.

## Using a provider for the home agent

On **Manage > Home agent**, choose it under **Model provider**. Its address
and key are then used, and the home agent's own Base URL and API key are
ignored. Leave Model provider at "None: use the endpoint below" to keep
using the address on that page. If a chosen provider is removed, the home
agent says so instead of quietly falling back to something else.

## llmanifold

llmanifold is a "front door" for language models: one address in front of
local engines and paid services, with lanes, queueing and fallbacks. If you
run it, set its address under **Settings > llmanifold** (for example
`http://llm.example.com:1234/v1`). The **llmanifold** plugin then loads and
appears in Model providers as the provider `llmanifold`, so the home agent
can pick it like any other. Until the address is set, the Plugins page
([[manual-plugins]]) says that is what it is waiting for.

Its settings:

- **llmanifold address**: its OpenAI-style address (no user name or password
  in it).
- **Token**: a secret, if your flows need one; blank for open flows.
- **Flows to offer** and **Default flow**: which flow names others may pick,
  and the one used when none is named.
- **Admin address** (advanced): its admin page, if the hub can reach it. With
  it, the status also shows lanes and the queue. The hub only reads it; it
  never drains, pauses or changes llmanifold.
- **Status timeout** (advanced): how long a status check waits.

## For agents

Agents can read (not change) the providers on worker `rook`:
`llm.providers` (names, hosts and offered models, never addresses or keys),
`llm.models` and `llm.check`, and `llmanifold.status`. Checks are limited per
caller (20 a minute) so one agent cannot flood your model services.

## Settings

Under **Settings > Model providers**:

- **OpenAI-compatible providers**: the list the Model providers page edits.
  It is easier to change it on that page.
- **Check timeout** (advanced): how long a check waits for an answer.
