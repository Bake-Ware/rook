# Settings

The hub plugins' settings, and each person's own.

| Cap | What it does |
|---|---|
| `setting.list` | Every hub-side setting of the hub's plugins with its type, default and value, then your own per-person ones. `plugin` narrows it; `person` (owners) shows someone else's. |
| `setting.set` | `{plugin, key, value, person?}`. Owners set hub settings and anyone's per-person ones; everyone sets their own. It applies to the next call; `null` goes back to the default. |

Values set here are kept in the hub's data folder (`settings.json`,
`person-settings.json`) and win over `hub.yaml`'s `settings:`. The
**Settings** page (under More) shows the hub's status and edits both kinds.
Its Hub section gives the band, the relay (or WebSocket address) machines
join at, and a link to the Workers page where they are let in. Owners also
see the people and agents the hub knows, and add, remove or set the password of a person (`hub.person`).
Worker-side settings are only in each worker's `worker.yaml` for now.
