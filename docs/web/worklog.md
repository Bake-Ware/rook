# Worklog: live terminals in Work

The worklog is the default Work view (dashboard **Sessions** tab). Each room
groups the sessions of one project directory on one host. Live sessions render
as real terminals (xterm.js). Finished sessions collapse into a log entry
showing title, agent, host, message count, age and status. Every historical
Claude/Codex session found on a host is listed and can be resumed with one click.
**Live now** collects every running terminal across rooms.

The classic view ([work.md](work.md)) remains available during rollout:

- **Classic view** / **Worklog view** buttons switch per browser (stored in
  `localStorage` as `rook.work.view`).
- `ROOK_WORK_V2=0` on the dashboard disables the worklog entirely. Only the
  classic view is served, and `launch` is refused.

## Data path

```
worker PTY ──ring──▶ work.stream.read (long-poll, compressed) ──band──▶ hub TermHub
                                                                        │ replay ring
             browser xterm.js ◀── /account/work/term/<session> (websocket) ┘
```

**Worker** (`rook/worker/plugins/terminals.py`, caps `work.stream.*`,
placement `not is_hub and has('pty')`). A harness runs under a PTY, with that
PTY as its controlling terminal, so Ctrl-C and job control behave as they do
locally (Windows: see Platform support below). Output lands in a ring (256 KB default, 1 MB max) addressed by
absolute byte offset. `work.stream.read(id, cursor, wait)` returns as soon as
bytes exist past `cursor`, or after `wait` seconds (maximum 25). It first waits
12 ms to coalesce a burst. A live terminal therefore costs one outstanding
request, not a polling loop. A lost reply costs one round trip and never data,
because the cursor advances only on a reply that arrived. At most 8 live
terminals run per worker. Finished terminals stay readable for 15 minutes.

### Platform support

| Platform | Terminal backend | `shell` harness | Inbox for sessions started outside Rook |
|---|---|---|---|
| Linux, macOS | POSIX PTY (`pty`, controlling tty) | `$SHELL -l` | Claude: Unix socket. Codex: app-server control socket, or Konsole over D-Bus |
| Windows 10 1809+ / 11 | ConPTY (`rook/worker/conpty.py`, pure ctypes, no extra package in the worker bundle) | `powershell.exe -NoLogo` (`pwsh.exe`, then `cmd.exe`, if PowerShell 5 is missing) | Claude: local named pipe. Codex: none (use a Rook terminal) |
| Windows before 1809 | none: the worker does not advertise `pty`, so `work.stream.*` is absent | | |

On Windows the worker advertises the `pty` fact only when `kernel32` has
`CreatePseudoConsole`. Each terminal's child is created suspended, placed in
a Job Object with kill-on-close, then resumed, so everything it starts ends
with the terminal. Signals map as follows: `INT` writes Ctrl-C (`0x03`) to the
console, which delivers CTRL_C_EVENT to the foreground program the way a
keyboard does. `HUP` closes the pseudoconsole (CTRL_CLOSE_EVENT to every
attached program). `TERM`, `QUIT` and `KILL` terminate the job.
`work.stream.close` sends `HUP`, waits 1.5 s, then terminates the job. Resize
is `ResizePseudoConsole`.

Harness launch on Windows uses the same environment (`TERM=xterm-256color`,
`ROOK_MCP_URL`/`ROOK_MCP_TOKEN`, `ROOK_WORK_SESSION`, persona) and the same
per-session files. The terminal folder and the Claude `--mcp-config` file get
an owner-only ACL, the Windows equivalent of modes 0700 and 0600. The ACL is
applied before the token is written. npm installs CLIs such as `codex` as
`.cmd` shims. The worker runs the shim's target (`node <script>` or the
`.exe`) directly, so cmd.exe never re-parses arguments such as persona text.
A `.cmd`/`.bat` launcher it cannot resolve runs through `cmd.exe /d /s /c`
only when no argument contains a cmd.exe metacharacter. Otherwise the launch
is refused.

Claude Code on Windows exposes its peer inbox as a local named pipe
(`\\.\pipe\LOCAL\cc-msg-<hex>`). Its marker
`%USERPROFILE%\.claude\sessions\<pid>.json` records `procStart` as the
process creation FILETIME, and the token file is named after the lower-cased
pipe path. Before sending, the worker checks all of the following:

- the marker and token files are owned by the worker's user and grant access
  to no one but that user, SYSTEM and Administrators,
- the process is a live `claude.exe` of the same user with the recorded
  creation time,
- the pipe path is a single local pipe name,
- the token file matches that process start (`procStartFt`, `pidDomain`),
- after connecting, `GetNamedPipeServerProcessId` is that process.

The pipe is opened at SECURITY_IDENTIFICATION level, so the server cannot act
as the worker's user.

**Framing** (`rook/worker/termwire.py`). Band messages are JSON, and every
packet is fragmented at about 1 KB with no retransmit. Each chunk travels in
the smallest encoding the reader accepts: `t` (UTF-8 text, only when the
chunk is valid on its own), `b` (base64), or `z` (base64 of zlib). TUI redraws
typically shrink 3-6x under `z`, which means fewer fragments that all have to
arrive. Decompression is bounded (4 MB per chunk).

**Hub** (`rook/remote/term_hub.py`). One `TermStream` per (worker, terminal)
follows the worker while at least one viewer is attached. It keeps following
for 60 s after the last viewer leaves and keeps the replay ring for 5 more
minutes. Memory is bounded for a hub host with about 1 GB of RAM:

| Budget | Value |
|---|---|
| replay ring per stream | 256 KB |
| queued output per viewer | 512 KB; a slower viewer is resynced (reset + ring replay) instead of buffered |
| pending input per stream | 64 KB |
| streams / viewers per stream | 32 / 16 |

Worst case is about 8 MB of rings plus viewer queues. `TermHub.memory()`
reports the current total.

**Browser** (`rook/web/worklog.js`, vendored xterm.js 6.0.0 in
`rook/web/vendor/xterm/`, served same-origin at `/account/work/assets/vendor/`).
The socket requires an operator login, the dashboard Origin, and the CSRF
token on every control message. Frames:

- server → browser **binary**: an 8-byte big-endian stream offset, then raw
  bytes. The browser tracks its cursor and drops any overlap.
- server → browser **JSON**: `hello` (the viewer's id plus state), `state`
  (`holder`, `viewers`, `cols`, `rows`, `running`, `exit_code`, `lost`),
  `reset` (clear the terminal; a ring replay follows), and `error`.
- browser → server **JSON**: `input {data, enc: t|b}`, `resize {cols, rows}`,
  `take`, `release`, `handoff {to}`, `signal {sig}`.

Reconnecting with `?since=<cursor>` replays only the missing tail when the
hub's ring still covers it; otherwise the browser gets `reset` and the ring.

**Input control.** Exactly one viewer holds input. The first viewer to type
while nobody holds takes control. **Take control** steals it, **Release** gives
it up, and **Hand off** passes it to another viewer. Keystrokes are coalesced
at the hub, with one `work.stream.write` in flight at a time, so they stay in
order. Only the holder's size reaches the PTY. Other viewers render the PTY's
grid at its size.

**Teardown.** **End session** calls `work.stream.close` (SIGHUP, then SIGKILL
to the process group), marks the session closed, and revokes its MCP token.
The dashboard's collector also calls `work.stream.list` every ~6 s for
sessions marked running that nobody is watching. This catches exits and hosts
that restarted ("Terminal is gone from its host").

## Launch templates

**+ Launch** (or **Launch again** on a finished entry) starts a harness on a
chosen host and directory:

| Harness | Command | Model | Rook MCP connection |
|---|---|---|---|
| `claude` | `claude [--resume ID]` | `--model M` | `--mcp-config <0600 file>` (removed when the terminal ends) |
| `codex` | `codex [resume ID]` | `-m M` | `-c mcp_servers.rook.url=… -c mcp_servers.rook.bearer_token_env_var="ROOK_MCP_TOKEN"` |
| `hermes` | `hermes` | `--model M` | environment only |
| `shell` | `$SHELL -l` | — | environment only |

Every harness also gets `TERM=xterm-256color`, `ROOK_MCP_URL`,
`ROOK_MCP_TOKEN` (when requested), `ROOK_WORK_SESSION` (hub session id),
`ROOK_WORK_TERMINAL`, `ROOK_PERSONA` and, when a persona applies,
`ROOK_PERSONA_FILE`. The worker strips its own band secret
from the environment. Workers advertise installed harnesses in their heartbeat
(`hb.work.harnesses`), and the form offers only those.

**Persona.** Before spawning, the worker fetches `persona.render` from the
hub (the session's persona id names a profile; empty means the persona
assigned to the harness family) and `persona_args(harness, text)` in
`terminals.py` passes it on: `--append-system-prompt` for Claude Code,
`-c developer_instructions=…` for Codex, nothing for Hermes (its persona
lives in SOUL.md, see `persona.apply`). See docs/design/persona.md.

**MCP URL.** Set `ROOK_WORK_MCP_URL` on the dashboard. If it is unset,
`<dashboard origin>/mcp` is used.

### Scoped MCP token (until permissions land)

With **Give it a Rook MCP token** checked, the dashboard mints a token through
the existing token store, acting as the signed-in operator (the same path as
the Tokens page):

- name `work:<harness>:<first 8 of session id>`, so journal and audit entries
  attribute the agent's calls to that session;
- scopes `["rook", "work-session:<session id>"]`. The token route accepts no
  other scope tags;
- lifetime of 1 day. The token is revoked when the session is ended from the
  worklog, or on the next operator socket after the terminal is seen to have
  exited.

**The scope tag is attribution only today.** Until the permissions layer
(docs/design/permissions.md) enforces it, this token can call everything any
operator API token can. The secret travels to the worker once, inside the
PSK-encrypted band call, and is redacted from the worker audit log (arg names
ending in `_token`). The web database stores only the token id.

## Caps

All of these are ordinary worker caps, visible to agents through `rook_caps`
and callable with `rook_call`. No new MCP tools were added.

| Cap | Risk | Purpose |
|---|---|---|
| `work.stream.open` | exec | start a harness under a PTY; returns `id` |
| `work.stream.read` | read | long-poll output from `cursor` (`wait` up to 25 s) |
| `work.stream.write` | exec | raw input (`\r` for Enter, `\x03` for Ctrl-C) |
| `work.stream.resize` | write | set cols/rows |
| `work.stream.signal` | exec | INT/TERM/HUP/QUIT/KILL to the process group (Windows mapping under Platform support) |
| `work.stream.close` | exec | stop and drop the terminal |
| `work.stream.list` | read | live and recently finished terminals, installed harnesses |
| `work.sessions` | read | live terminals plus Claude/Codex history as one resumable catalog (`limit`, `offset`, `query`) |
| `work.export` | read | one page of a historical transcript in `rook.transcript/1` |
| `claude-history.transcript`, `codex-history.transcript` | read | the same export, per agent |

Successful `work.stream.read` calls are not written to the worker audit log,
because a live terminal long-polls continuously. Failures still are.

## Transcript export format: `rook.transcript/1`

This is the stable interface for the memory plugin to ingest historical
sessions. Page with `work.export(agent, session_id, offset)` until
`next_offset` is `null`. Transcripts stay on their host until asked for.

```jsonc
{
  "ok": true,
  "format": "rook.transcript/1",
  "session": {                      // first page only (offset 0)
    "agent": "claude",              // claude | codex
    "session_id": "…", "title": "…", "cwd": "/srv/app", "git_branch": "main",
    "started": "2026-08-30T10:00:00Z", "updated": "…", "message_count": 42
  },
  "messages": [
    {"index": 0, "role": "user", "ts": "2026-08-30T10:00:00Z", "text": "…"},
    {"index": 1, "role": "assistant", "ts": "…", "text": "…"},
    {"index": 2, "role": "tool", "ts": null, "text": "[tool_use: …]",
     "clipped": 1200}               // present only when text was cut at 50,000 chars
  ],
  "next_offset": 3                  // null on the last page
}
```

- `index` is stable for a given transcript file. Ingest idempotently on
  `(host, agent, session_id, index)`.
- `role` is `user`, `assistant` or `tool`.
- `max_chars` (500-50,000, default 6,000) bounds a page. A page always holds
  at least one record.
- Discover sessions with `work.sessions` (fields `agent`, `session_id`,
  `title`, `cwd`, `updated`, `messages`, `active`, `resumable`).

Additive fields may appear under the same format name. A breaking change will
be published as `rook.transcript/2`.

## Fleet compatibility

- Build-167 workers do not announce `work.stream.*`. Their history still
  appears, and **Resume on host** uses the existing `*-history.resume` +
  `proc.*` path. Follow its output in the classic view.
- No wire format changed. The new caps, the `hb.work` heartbeat key and the
  new token-route `scopes` field are all optional additions.
- The classic view keeps working against the same session records.

## Verification

- `pytest tests/test_work_terminals.py`: framing, launch templates, real PTY
  round trips (resize, Ctrl-C, exit code, ring bounds, long-poll timing), token
  injection and cleanup, transcript export, hub fan-out (holder rules, replay,
  slow-viewer resync, lost worker), and web launch, stream, close and revoke,
  PTY vs `proc.*` resume, the flag, and the token-scope validation.
- `pytest tests/test_windows_terminals.py`: the ConPTY backend and the
  Windows Claude inbox on Linux, with kernel32 faked at the ctypes boundary.
  It covers the call sequence, suspended start and job, handle cleanup,
  streaming, resize, signal mapping, exit drain, owner-only files, npm shim
  resolution, and the inbox's match and refusal rules.
- `py tests\integration\windows_conpty_check.py [--claude] [--inbox]` on a
  Windows machine (manual): real ConPTY output, input, resize, Ctrl-C, exit
  codes, job teardown of grandchildren, PowerShell through the plugin, ACLs,
  and, optionally, Claude Code in a terminal and one inbox message (`--send`).
- `ROOK_IT=1 pytest tests/integration/test_work_stream.py`: 20,000 lines
  through a real band, over MCP long-polls and through `TermHub` with two
  viewers, checked byte for byte.
