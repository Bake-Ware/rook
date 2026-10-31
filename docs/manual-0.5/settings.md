# Settings and secrets

## Where a setting lives

Each plugin setting says in the plugin's manifest where it lives:

| `where` | Set in | Example |
|---|---|---|
| `worker` (the default for worker plugins) | the worker's `worker.yaml`, under `settings:` | the path to `deluge-console` on that machine |
| `hub` | the hub: `hub.yaml` under `settings:`, or the Settings page | which agent answers the dashboard's chat dock |
| `person` | each person's own, on the Settings page | which channel `notify` reaches you on |

```yaml
# worker.yaml
settings:
  torrent: { console: /usr/bin/deluge-console }
# hub.yaml
settings:
  pages: { dock: house }
```

Values set on the Settings page (or with the cap `setting.set`) are kept in
the hub's data folder and win over `hub.yaml`; setting `null` goes back to
the default. Owners change hub settings and anyone's per-person settings;
everyone changes their own. `setting.list` shows every hub-side setting
with its type, default and value. Worker settings are only in `worker.yaml`
for now: the Settings page doesn't edit them yet.

A plugin that needs a setting before some caps can work (`requires:
{settings: [...]}`) shows those caps as unavailable, with the reason, until
it has a value.

## Choosing a provider

Some caps are shared contracts that several plugins can provide:
`llm.complete` (Claude API, an OpenAI-compatible server, a Claude Code or
Codex subscription), `notify.send`, `agent.backend`. Pick the provider in
`hub.yaml`:

```yaml
contracts:
  llm.complete: llm-cli.complete
```

## Secrets

A secret setting (`secret: true` in the manifest) holds a **name**, never a
value. Its value is fetched when a call needs it, and redacted from errors
and the journal. In call arguments and manifests a secret is written as a
reference: `{{secret:NAME}}`.

0.5 has no vault yet. Until it does:

- a hub plugin's secret comes from the hub process's environment, as
  `ROOK_SECRET_<NAME>` (upper case, anything not a letter or digit as `_`),
  so it is never in a config file: with `telegram: {token: telegram-bot}`
  in `hub.yaml`'s settings, the bot token is read from
  `ROOK_SECRET_TELEGRAM_BOT`;
- in call arguments, the hub resolves only the band keys it keeps for other
  bands (`{{secret:band-psk-<band>}}`, owners' calls only). Any other
  reference reaches the worker as written.

`python -m rook.core2.migrate` carries a 0.4 vault over into
`<data>/vault.db` for when the vault lands ([Moving from 0.4](migrating.md)).
