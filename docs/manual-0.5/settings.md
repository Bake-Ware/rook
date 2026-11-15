# Settings and secrets

## Where a setting lives

Each plugin setting says in the plugin's manifest where it lives:

| `where` | Set in | Example |
|---|---|---|
| `worker` (the default for a plugin that runs on a worker) | the worker's `worker.yaml`, under `settings:` | whether the browser on that machine shows its window |
| `hub` (the default for a plugin that runs on the hub) | the hub: `hub.yaml` under `settings:`, or the Settings page | which agent answers the dashboard's chat dock |
| `person` | each person's own, on the Settings page | which channel `notify` reaches you on |

```yaml
# worker.yaml
settings:
  browser: { headless: false }
# hub.yaml
settings:
  agent: { dock: house }
```

Values set on the Settings page (or with the cap `setting.set`) are kept in
the hub's data folder and win over `hub.yaml`; setting `null` goes back to
the default. Owners change hub settings and anyone's per-person settings;
everyone changes their own. `setting.list` shows every hub-side setting
with its type, default and value. Worker settings are only in `worker.yaml`
for now: the Settings page doesn't edit them yet.

The Settings page is a map of what the system can do. It groups every
setting by its plugin's segment (Agents, Machines, Work, Hub, then any
other), and inside it by plugin. Each row is the setting's description,
its `plugin.key` name, and a control: a switch, a dropdown where the
values are known (a fixed list, the vault's secret names, the band's
workers), a number box, a text box, or one item per line for a list. A
change is saved as it is made. Your own settings are marked "yours". The
filter box narrows the rows by any word.

A plugin that needs a setting before some caps can work (`requires:
{settings: [...]}`) shows those caps as unavailable, with the reason, until
it has a value.

## Choosing a provider

Some caps are shared contracts that several plugins can provide:
`llm.complete` (Claude API, an OpenAI-compatible server, a Claude Code or
Codex subscription), `notify.send`, `voice.tts`. With none picked, the
first installed answers. For `llm.complete`, the **Model providers** page
(under Agents) has **Use this one** on each provider (`llm.choose`, which
an agent can call too); it takes effect on the next call and is kept in
`hub.yaml`. The others are picked in `hub.yaml`:

```yaml
contracts:
  llm.complete: llm-cli.complete
```

## Secrets

A secret setting (`secret: true` in the manifest) holds a **name**, never a
value. Its value is fetched when a call needs it, and redacted from errors
and the journal. In call arguments and manifests a secret is written as a
reference: `{{secret:NAME}}`.

The values live in the hub's vault, `<data>/vault.db` (owners only):

- store one with `secret.set {name, value}`, or on the hub's machine with
  `python -m rook.core2.hub --config hub.yaml secret set NAME` (the value on
  stdin); `secret.list` shows names, never values;
- a hub plugin's secret setting reads its value from the vault when the
  plugin needs it: with `telegram: {token: telegram-bot}` in `hub.yaml`'s
  settings, the bot token is the vault's `telegram-bot`;
- in the args of a call to a worker, `{{secret:NAME}}` (alone or inside a
  string) becomes the value for that call only; the journal keeps the
  reference, and the value comes back as `[redacted]`. Calls to the hub's
  own caps keep the reference as text. A command gets a secret in its
  environment this way: `shell.exec` with
  `env: {GH_TOKEN: "{{secret:gh-token}}"}`;
- an agent turn's door token is a reference too: `ctx.turn_token` returns a
  name (`turn-...`), and `{{secret:<that name>}}` in a call to a worker is
  the token until the turn ends, for whoever the turn acts for. The token
  stays in the hub's memory; no script or journal entry holds it.

Worker-side secret settings (a worker plugin's `secret: true` setting) have
no path from the vault yet: pass the secret as a reference in the call.

If the hub dies, copy
`vault.db` to the new hub and run `recover-vault` with the recovery key.
