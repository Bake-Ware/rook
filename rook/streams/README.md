# Streams

One append-only, typed event log for every live thing: chat rooms, huddles,
terminals, processes, agent sessions, inboxes and voice. Design: knowledge
page `castling-streams` (Bake, Oct 28 2026). This package is the store and
the API; the `stream.*` caps and the hub relay sit on top of it.

## Model

A **stream** has an id (`s_…`), a **kind** (`terminal`, `process`, `room`,
`huddle`, `session`, `voice`, `inbox`, or any other a plugin names), a
**host** (the worker that owns its producer, or `hub`), participants, free
`attrs` (cwd, pid, exit code, …) and a **spec**, the same three fields a cap
declares in `plugin.yaml`:

```yaml
stream: { types: [bytes, status], input: true, durable: false }
```

**Events** are numbered 1, 2, 3… per stream. A reader's cursor is the last
`seq` it saw; `read` returns `{events, next, dropped, closed}` and you pass
`next` back. Five types:

| Type | Body | Used by |
|---|---|---|
| `bytes` | raw bytes | PTY and process output |
| `text` | `{text, …}` (mentions, reply_to) | rooms, huddles, inboxes |
| `turn` | `{role: prompt\|reply\|tool, …}` | agent sessions, voice |
| `status` | any object | lifecycle (`closed`, exit code), huddle status |
| `input` | exactly one of `{text}`, `{signal}`, `{resize: [cols, rows]}` | keystrokes, messages into an agent, Ctrl-C, window size |

**Durability** (decided Oct 28): rooms, huddles and sessions are durable
(`SqliteStore`, every event kept until `delete`). Terminals and processes
are bounded (`MemoryStore`, newest events up to a byte budget, 256 KiB by
default like today's scrollback); a reader that fell behind gets `dropped`
and a jump in `seq`. `export` copies a bounded buffer into a new durable
stream. The defaults per kind are in `events.KINDS`.

A **producer** writes a stream's output (a PTY, a process, an agent harness,
a voice link). It `attach`es, appends through its handle and takes input
from `Producer.inputs()`. Input written while nothing is attached waits in
the stream; the next producer gets it. That is how a message reaches a
sleeping agent, and why **wake = attach**. Input is at-least-once: an event
counts as taken when the producer asks for the next one.

## API

| Cap (castling-caps) | Here |
|---|---|
| `stream.open` | `Streams.open(kind, spec=…, title, producer, participants, attrs)`; a cap with a `stream:` block uses `cap_stream(...)` and returns `{"stream": id}` |
| `stream.read` | `await Streams.read(sid, after, limit, types, wait)` (long-poll); in process, `Streams.follow(...)` |
| `stream.write` | `Streams.append(sid, type, body, by)`: `input` on an interactive stream, `text` in a room |
| `stream.attach` | `Streams.attach(sid, producer)` → `Producer` |
| `stream.list` | `Streams.list(kind, participant, open_only)` |
| `stream.close` | `Streams.close(sid, **status)`: kept and readable, takes no more events |
| `stream.search` | `Streams.search(query, kind)` across both stores |
| `stream.delete` | `Streams.delete(sid)`: erase for good (its own cap, a hard stop) |

## How today's plugins sit on it (no migration yet)

| Today (master d15e79bf) | Becomes |
|---|---|
| `work.stream.*` terminals (`terminals.py`, `ring.py`) | kind `terminal`, producer `pty`/`conpty`. Output → `bytes`; keystrokes, `resize`, `signal` → `input` events the PTY producer consumes. `ByteRing` and byte cursors give way to the bounded store and `seq` cursors. PTY spawning stays as the producer (through core's launch path). |
| `proc.*` | kind `process`: a terminal without a PTY (stdout and stderr merged, as today). |
| Console rooms + console pump | a `terminal` stream with a title and participants; search is `stream.search`; the close summary is the closing `status`. The pump goes. |
| Session shim (your own terminal mirrored) | a `terminal` stream whose producer is the shim link. |
| `term_hub` (browser replay) | the hub relay below. |
| Hub chat rooms (`band_mcp/chat_rooms.py`, `chat.*` on `rook`) | kind `room`, `text` events `{text, mentions}`. Per-reader read positions (unread counts) stay with the rooms plugin as cursors; presence and avatars aren't streams. |
| Huddles | kind `huddle`; the huddle plugin follows the stream and runs rounds on top. Agent status → `status` events (or the whiteboard), questions → `approval.ask` cards. |
| Worker `chat` (JSONL) and `msg` | a `room` with the local human, or a person's `inbox` stream. No JSONL files. |
| `session_messages`, the house agent's inbox | `input` events into the agent's `session` stream. |
| `sessions` catalog, `claude_history`, `codex_history`, `session_mirror` | importers that write `turn` events into `session` streams; `sessions.list` → `list(kind="session")`. |
| `agenthost` turns, persistent lanes, `role.session_*` | the harness is the producer of a `session` stream; a turn is an `input`, the reply `turn` events; cancel is `input {signal: INT}`. |
| `wake` | `attach` an agent producer to the stream. |
| Voice | kind `voice`; the voice link is the producer and writes `turn` events. |

## Not built here

- **`stream.*` caps.** Thin wrappers once the plugin kit (`ctx`, real caps)
  lands. `ctx.stream(...)` calls `cap_stream` (TODO castling:kit).
- **Hub index and relay.** The design has one hub index of all streams and
  one fan-out relay with one replay buffer, replacing `term_hub`, the console
  pump and room polling. It needs the core v2 wire messages (spec agent);
  `Meta.wire()` / `Event.wire()` are the shapes it would carry.
- **Access rules per kind** follow castling-security, to settle with the SDK.

## Choices made where the design was silent

- `status` is always allowed (core writes `closed`); other types must be in the spec.
- `input` bodies carry text, not raw bytes; binary input can come later as a minor.
- `voice` and `inbox` default to durable, like sessions and rooms.
- One producer per stream at a time.
- Closed bounded streams are kept for late readers, the newest 32.
- One event is at most 1 MiB; producers split bigger output.
