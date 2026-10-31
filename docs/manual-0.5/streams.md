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

A stream belongs to the people and agents it was opened for; others can't
read it. Closing keeps it readable; it just takes no more events.

## What is and isn't there yet

Not on `castling` yet: a worker has no stream store, so `shell.open`
answers `streams are not available here`, and no worker serves the
`stream.read` and `stream.write` caps the `stream` tool calls. Both arrive
with the worker's `stream.*` caps in their own pull request (#289); this
page describes how they work then. A hub index of every stream and a
dashboard view of terminals and sessions come later. The model, the event
types and how 0.4's terminals, consoles, chat and agent sessions map onto
streams are in `rook/streams/README.md`.

## For plugin authors

A cap declares that it opens a stream in its manifest:

```yaml
stream: { types: [bytes, status], input: true, durable: false }
```

and its `run:` code returns `ctx.stream(...)`, then keeps writing through
the handle.
