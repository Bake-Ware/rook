# Caps

A **cap** is one thing a worker (or the hub) can do: `shell.exec`,
`torrent.pause`, `task.create`. Its name is `<plugin>.<what it does>`.
Every cap is declared in its plugin's manifest with a description, its
arguments and result, and three facts the hub uses to protect you:
**risk**, **scope** and **effects**. Because they are declared, the hub
knows them even while the worker is offline.

## Finding caps

- The dashboard's Workers page lists each worker's plugins and what it
  can't serve.
- Agents use the MCP `caps` tool: `caps match="shell."` for a family,
  `caps match=shell.exec` for one cap's arguments ([MCP for agents](mcp.md)).
- Over HTTP: `GET /caps?worker=kaiju`.

A cap renamed between 0.5 releases keeps its old name as an alias for a
release or two. 0.4 names are not aliases: 0.5 is a clean cut.

## Risk

How much a cap can change. Lowest first:

| Risk | Means |
|---|---|
| `read` | looks; changes nothing |
| `write` | changes something through the cap's own narrow purpose |
| `exec` | runs arbitrary code or commands (shell, scripts) |
| `admin` | changes Rook's administration, security or system configuration; every elevated call counts as `admin` |

Presets and grants can cap the highest risk a person or agent may use
([People and presets](people.md)).

## Scope

How far a cap reaches: `self` (Rook's own state, or the caller's own
data), `local` (this machine or device), `multi-machine`, `multi-user`
(other people's data or accounts), `external` (outside the band: the
internet, a third-party service).

## Effects

Flags for what a cap does that needs care. A cap lists any that apply:

| Effect | Means | What the hub does |
|---|---|---|
| `destructive` | erases or overwrites data for good | always waits for you; never retried automatically |
| `sensitive` | reveals secrets or private content (screen, camera) | waits for you when it reveals a secret's value |
| `personal` | reads or writes someone's personal data (messages, location) | kept from children and guests |
| `physical` | acts on the physical world (power, locks, speakers) | kept from children and guests; never retried automatically |
| `sends_as` | sends as you to someone else | always waits for you |
| `spends` | spends money | always waits for you |
| `persists` | creates persistence (services, keys, users, scheduled tasks) | the security gate looks harder |
| `taints` | brings outside content in (web pages, email, inbound messages) | marks the session tainted |

A flag the hub doesn't know counts as `destructive`.

## Arguments, results and timeouts

Arguments are checked against the cap's declared types before anything
runs; a mismatch fails with `bad args: …`. A call waits for the cap's
declared `timeout` (else 15 seconds). A call that timed out **may still be
running**: the hub retries by itself only caps declared `idempotent`, and
never a `destructive` or `physical` one. Check the journal before trying
again by hand.

Some caps answer `{"stream": id}` instead of a result: a live terminal or
process you read and write ([Streams](streams.md)). A cap with `undo`
names the cap that reverses it (`torrent.pause` → `torrent.resume`).

## Shared contracts

Some cap names are contracts many plugins can answer: `screen.capture`,
`hid.type`, `notification.post`, `power.off` on devices, and services such
as `notify.send` and `llm.complete` on the hub. A call to `screen.capture`
on a desktop worker reaches `screen-desktop.screen.capture`. Which provider answers a hub
service is a setting ([Settings](settings.md#choosing-a-provider)).
