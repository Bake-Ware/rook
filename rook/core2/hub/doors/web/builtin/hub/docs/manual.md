# Hub

What the hub knows about itself and its workers.

| Cap | What it does |
|---|---|
| `hub.status` | The hub's id, band, core version, doors (and which are closed), plugins and `security` (`off` or `on`); for owners, its people too. |
| `hub.workers` | Every worker the hub knows, online or not: name, id, plugins, when it was last seen, and the caps it can't serve with the reason (`missing bin: ...`). |
| `hub.door` | `{door, open}`: the kill switch. Closing a door (`mcp`, `http`, `web`) makes it refuse everything (calls, sessions, reads, work already started through it); closing `mcp` also refuses agents' tokens on every door. Owners, or a hub component closing one (the security plugin). Kept across restarts. Over HTTP: `POST /doors/<name> {"open": false}`. |
| `hub.manifests` | The manifest of every plugin in use: the hub's own and those its workers announce. `section` (`jobs`, `personas`, ...) narrows it to the plugins that have that section, and only that section. |

## Security: off or on

One hub setting, `hub.security`, default `off` (Bake, Oct 31: "I just want
no approvals for right now"). An owner flips it on **Settings**
(`hub.security`, Save) or with `setting.set {plugin: hub, key: security,
value: on}`; the call is journaled like any other.

- `off`: nothing waits on a person. No gate runs and no call is held
  (each journal entry says `security: off`, and `would_hold` lists a
  hard stop it would have waited on); `core.elevate` checks rights and the
  ceiling but no gate; a device code is approved at once as the first
  owner (the `enroll` journal entry names the machine); a sacred project
  needs no owner sign-off (reviewers still sign).
- `on`: gates, holds, device-code approval and sacred sign-off apply.

Either way the vault stays encrypted and secrets stay masked in the journal.

The **Workers** page (in the sidebar) shows `hub.workers` and refreshes
when a worker comes online, goes offline or changes its plugins. The
`hub/workers` component puts the same list on any page (`only:` a few
worker names).

A worker is offline after 90 seconds without a heartbeat. The hub still
knows its caps and still tries a call to it: an offline worker fails with
the real error, never a guess.
