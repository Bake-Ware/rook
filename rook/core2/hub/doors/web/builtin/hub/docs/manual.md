# Hub

What the hub knows about itself and its workers.

| Cap | What it does |
|---|---|
| `hub.status` | The hub's id, band, core version, doors and plugins; for owners, its people too. |
| `hub.workers` | Every worker the hub knows, online or not: name, id, plugins, when it was last seen, and the caps it can't serve with the reason (`missing bin: ...`). |
| `hub.manifests` | The manifest of every plugin in use: the hub's own and those its workers announce. `section` (`jobs`, `personas`, ...) narrows it to the plugins that have that section, and only that section. |

The **Workers** page (in the sidebar) shows `hub.workers` and refreshes
when a worker comes online, goes offline or changes its plugins. The
`hub/workers` component puts the same list on any page (`only:` a few
worker names).

A worker is offline after 90 seconds without a heartbeat. The hub still
knows its caps and still tries a call to it: an offline worker fails with
the real error, never a guess.
