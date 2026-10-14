# Model providers and llmanifold

## Why

Each hub feature that talks to a language model used to carry its own
endpoint, model and key: the home agent (`home.base_url`, `home.api_key`),
its memory (through the home agent's endpoint), the voice service
(`voice.llm_url`). A household usually has one or two model servers, often
behind one front door ([llmanifold](https://github.com/Bake-Ware/llmanifold)).
Model providers name them once.

## The pieces

**`llm` hub plugin** (`rook/hub/plugins/llm`, Manage > Model providers).
A registry of `Provider`s: `id`, `base_url` (an OpenAI-compatible `/v1`),
`api_key` (a vault reference `{{secret:name}}`, never the key), `models`
offered (empty: whatever `/v1/models` lists), `default_model`, `kind` and
`source`. Providers come from:

* its setting `llm.providers` (a list, edited on the page): any
  OpenAI-compatible server;
* other loaded hub plugins that define `llm_providers() -> [Provider]`. The
  registry asks them on every lookup, so a plugin that reloads or is
  switched off simply appears or disappears; nothing is registered or left
  behind. A plugin's provider owns its id: the page refuses a hand-made
  provider with the same id.

API for hub code: `get(id)`, `require(id)`, `client(id, model=None, actor=…)`
(a `ChatClient` with the key resolved from the vault, attributed to `actor`),
`models(id, live=False)`, `check(id)`. A contributing plugin may also define
`provider_detail()`; `check` adds it (llmanifold: flows, lanes, queue).

Caps on worker `rook`, all `read`: `llm.providers` (ids, hosts, offered
models, last check; never URLs or key references), `llm.models`,
`llm.check`. `llm.models` and `llm.check` read the key as the calling
identity (the vault's audit names the caller), and allow 20 calls a minute
per caller with at most 4 running at once. A `base_url` with a user name or
password in it is refused (hosts are shown as `host:port` only).

**`llmanifold` hub plugin** (`rook/hub/plugins/llmanifold.py`). Loads once
`llmanifold.base_url` is set (until then the Plugins page says to set it; the
host retries the plugin when one of its settings changes). Contributes the
provider `llmanifold` (kind `llmanifold`; flows to offer and a default flow
are its settings) and the cap `llmanifold.status`: the API's flows and, with
`llmanifold.admin_url`, a summary of the admin `/api/status` (version, reload
error, per-endpoint name/state/in-flight/lanes, queue length), answered from
the last check for 10 s. Neither address may carry a user name or password.
It only reads:
draining, pausing and editing stay with llmanifold's own admin site and its
worker plugin.

This is the hub side. The llmanifold repo's beta worker plugin
(`llmanifold_rook`, entry point `rook.plugins`) runs on the machine llmanifold
runs on and carries the admin caps (drain, undrain, reload); the two do not
overlap.

**Home agent.** `home.llm_provider` (Model provider on its page) names a
provider. `HomeAgent.config()` then takes `base_url`, `api_key` and, when no
model is set, the provider's default model from it, so everything that reads
the home agent's configuration (the chat client, its memory's chat and
`/v1/embeddings`) follows the provider without knowing about it. A provider
that disappears makes the agent report "needs its model provider" rather than
fall back to the old endpoint. A model probe from the page uses the
provider's own key, which only ever goes to the provider's own address.

## Not done here

* Voice still has its own `voice.llm_url` / `voice.llm_model` (the voice
  service reads them as `VLLM_URL` / `VLLM_MODEL`). Pointing it at a provider
  means resolving the provider's key into the service's environment, which
  is a later step.
* No `llm.chat` cap: the band can see providers but not spend tokens through
  the hub. Adding one needs a policy decision (risk tier, metering).
* Knowledge embeddings (`knowledge.embedder`) keep their own `{texts} ->
  {model, vectors}` service.
