# Streams

A **stream** is one live thing you can read and write: a terminal, a
running process, an agent session, a chat room. Every stream is the same
shape: numbered events (output bytes, text, agent turns, status changes,
input), read from a cursor. Terminals and processes keep a bounded buffer
(the newest 256 KiB); rooms and sessions keep everything.

## Opening one

A cap that opens a stream answers `{"stream": "s_…"}` instead of a result.
`shell.open` is the everyday one: a live terminal (a PTY) or a process on a
worker.

```
call worker=kaiju cap=shell.open args={"pty": true} why="Look around kaiju's disks."
→ {"stream": "s_7c1e…"}
```

Without `argv` it starts a login shell; `pty: false` runs a process on
pipes instead of a terminal.

## Reading and writing

Agents use the MCP `stream` tool ([MCP for agents](mcp.md)):

- **read**: `stream worker=kaiju stream=s_7c1e… after=0 wait=5 why="..."`
  returns `{events, next, dropped, closed}`; pass `next` back as `after`.
  `wait` long-polls for new output. `dropped` says a reader fell behind a
  bounded buffer and missed some output.
- **write**: `input={"text": "ls -l\n"}` types; `{"signal": "INT"}` is
  Ctrl-C; `{"resize": [120, 40]}` sets the window size.

A stream belongs to the people and agents it was opened for, and to the
hub's owners; others can't read it. Closing keeps it readable; it just
takes no more events.

## Finding one

The hub keeps an index of every stream on the band: terminals and
processes on every worker, agent and voice sessions on the hub. Ask the
hub (`worker=rook`):

```
call worker=rook cap=stream.list args={"kind": "terminal", "open": true} why="Which terminals are open?"
→ {"streams": [{"id": "s_7c1e…", "worker": "kaiju", "kind": "terminal", "title": "/bin/bash -l",
                "person": "person:bake", "last_at": …, "size": 18234, "online": true, …}], "next": null}
```

- `stream.list` filters by `kind`, `worker`, `participant` and `open`, latest
  activity first; `limit` and `offset` page it (pass `next` back as `offset`).
- `stream.search` with `query` matches titles, commands, working folders and
  who takes part. What a stream *says* isn't indexed: read the stream.
- `stream.get` with `stream` gives one row.

Then read it where it lives: `stream.read` on the row's `worker` (a row with
`plugin`, such as an agent session, is read through that plugin's caps).
You see what you could read: owners every stream, a person the ones they
take part in, an agent acting alone the ones it takes part in that have no
person. A worker's terminals are gone when it restarts; the index forgets
them then too.

## What is and isn't there yet

Not on `castling` yet: the hub relay (one fan-out replay for every reader,
replacing 0.4's terminal hub and console pump) and a dashboard view of
terminals and sessions. The model, the event types and how 0.4's terminals,
consoles, chat and agent sessions map onto streams are in
`rook/streams/README.md`.

## For plugin authors

A cap declares that it opens a stream in its manifest:

```yaml
stream: { types: [bytes, status], input: true, durable: false }
```

and its `run:` code returns `ctx.stream(...)`, then keeps writing through
the handle. A hub plugin that keeps streams of its own (`rook.streams.Streams`)
hands them to `ctx.index_streams(streams)` once, so `stream.list` finds them.
