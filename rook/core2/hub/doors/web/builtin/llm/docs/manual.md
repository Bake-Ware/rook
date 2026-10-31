# Models

Which language model providers this hub can use. `llm.complete` itself is a
shared contract (core's): plugins call it, and it goes to the chosen
provider (`llm-anthropic`, `llm-openai`, `llm-cli`, ...).

| Cap | What it does |
|---|---|
| `llm.providers` | The installed `llm.complete` providers, by plugin, and which one is chosen. |
| `llm.models` | The models one provider offers (blank: the chosen one), with its System One, Two and Three defaults. |
| `llm.status` | Whether a provider (blank: every one) answers, and how long it took. |

Choose the provider in `hub.yaml`:

```yaml
contracts:
  llm.complete: llm-cli.complete
```

With one provider installed, it is the one. Agents think through their own
backend (`agent.backend`, for example `agents-claude-code`), not through
`llm.complete`.
