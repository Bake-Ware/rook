# Settings

The hub plugins' settings, and each person's own.

| Cap | What it does |
|---|---|
| `setting.list` | Every hub-side setting of the hub's plugins with its type, doc, default, value and where the value came from (`source`: set, config or default), then your own per-person ones. Each row also carries its plugin's `segment`, `title` and `plugin_doc`, a fixed list of values as `enum`, and a list's or map's `items` type. `plugin` narrows it; `person` (owners) shows someone else's. |
| `setting.set` | `{plugin, key, value, person?}`. Owners set hub settings and anyone's per-person ones; everyone sets their own. It applies to the next call; `null` goes back to the default. |

Values set here are kept in the hub's data folder (`settings.json`,
`person-settings.json`) and win over `hub.yaml`'s `settings:`. The
**Settings** page (under Hub) edits both kinds, then shows the hub's status.
Its settings are grouped by segment, then by plugin; a segment is open when
it holds a value somebody set. A secret setting offers the vault's names
(`secret.list`), a `worker` one the band's workers (`hub.workers`). A change
is saved when it is made (on Enter or leaving a box); a blank box goes back
to the default.
Its Hub section gives the band, the relay (or WebSocket address) machines
join at, and a link to the Workers page where they are let in. Owners also
see the people and agents the hub knows, and add, remove or set the password of a person (`hub.person`).
Worker-side settings are only in each worker's `worker.yaml` for now.
