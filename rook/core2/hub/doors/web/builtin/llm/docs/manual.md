# Models

Which language model providers this hub can use. `llm.complete` itself is a
shared contract (core's): plugins call it, and it goes to the chosen
provider (`llm-anthropic`, `llm-openai`, `llm-cli`, ...).

| Cap | What it does |
|---|---|
| `llm.providers` | The installed `llm.complete` providers, by plugin, and which one is chosen. |
| `llm.choose` | Send `llm.complete` to this provider from the next call on; kept in `hub.yaml`'s `contracts`. |
| `llm.models` | The models one provider offers (blank: the chosen one), with its System One, Two and Three defaults. |
| `llm.status` | Whether a provider (blank: every one) answers, and how long it took. |

`llm.models` and `llm.status` ask each provider's own `models` cap as this
plugin (`uses: ["*.models"]`), as `agent.models` asks the engines.

With one provider installed, it is the one. `llm.choose` writes the
choice into `hub.yaml`:

```yaml
contracts:
  llm.complete: llm-cli.complete
```

## The Model providers page

Under Agents. One card per installed provider, the one in use first:
whether it answers now (the error when it doesn't; **Check again**),
**Use this one**, and its key (a vault name: add one on the Vault page),
address, worker and System One/Two/Three models, each saved as it is
changed. **More settings** opens the Settings page for the rest.
